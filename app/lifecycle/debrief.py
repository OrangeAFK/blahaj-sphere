"""Evidence, grading and the Debrief view (SPEC §7.7, §3.1, §8.2).

Evidence is written to events.jsonl only: `proof_pass` when the checker passes, `prelab_numeric` (code) and `prelab_explain`
(heavy pinned grader) when the node enters Debrief. If grading is unavailable the node enters Debrief as **provisional**: proof
and calibration are shown, grades read "pending", and the router's grading queue retries; `apply_queued` writes the events then.
"""

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.builder.context import load_framework
from app.competency.engine import Event, base_value_weight, score_domain
from app.datastore.cache import DataStore
from app.datastore.misconceptions import RESOLVE_MIN_VALUE, add_misconception, fold, read_records, resolve_misconception
from app.datastore.events import new_event, read_events
from app.lifecycle.nodes import LifecycleError, NodeRef, NodeStore, autocommit, node_lock
from app.lifecycle.tutor import Tutor, public_checker
from app.llm.types import LLMError

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
GRADE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array"}, "misconceptions": {"type": "array"}},
    "required": ["items", "misconceptions"],
}
GRADE_TASK = "grade_prelab_explain"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def tolerance_abs(it: dict) -> float:
    """A numeric prelab key item's tolerance as an absolute amount in its unit (rel is a fraction of |value|)."""
    tol_spec = it.get("tolerance") or {}
    return float(tol_spec["rel"]) * abs(float(it["value"])) if "rel" in tol_spec else float(tol_spec.get("abs", 0.0))


def refresh_provisional(store: NodeStore, ref: NodeRef) -> None:
    """provisional = any grading (prelab explanations, oral) is still pending (SPEC §3.1)."""
    pending = any((store.session(ref, name, {}).get("grading") or {}).get("status") == "pending" for name in ("debrief", "oral"))
    store.set_scalar(ref, "provisional", pending)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Evidence:
    """Writes events for one node: domain/tier derivation, interleaved and assist_reveal flags, misconception resolution."""

    def __init__(self, store: NodeStore, tutor: Tutor):
        self.store = store
        self.tutor = tutor
        self.ds = DataStore(store.root, store.track)

    def domain_of(self, ref: NodeRef, subskill: str) -> str:
        fw = load_framework(self.store.root, self.store.track)
        if fw is not None:
            for d in fw.domains:
                if subskill in d.subskills:
                    return d.id
        return subskill.split(".")[0] if "." in subskill else str(self.store.frontmatter(ref).get("domain"))

    def tier_for(self, ref: NodeRef, domain: str) -> int:
        """Node tier, capped at the learner's current tier in that domain (an interleaved item from a lower-tier domain must
        not sit above it: §8.3 only counts events with tier <= T)."""
        node_tier = int(self.store.frontmatter(ref).get("tier") or 1)
        cur = self.ds.scores().get(domain)
        return min(node_tier, int(cur["tier"])) if cur else node_tier

    def emit(self, ref: NodeRef, type_: str, subskill: str, *, score: float | None = None, err: float | None = None, tol: float | None = None,
             interleaved: bool = False, grader_model: str | None = None) -> Event:  # fmt: skip
        v, w = base_value_weight(type_, score=score, err=err, tol=tol)
        domain = self.domain_of(ref, subskill)
        ev = new_event(track=ref.track, node_id=ref.id, domain=domain, subskill=subskill, tier=self.tier_for(ref, domain), type=type_, value=v, weight=w,
                       interleaved=interleaved, assist_reveal=subskill in self.tutor.reveals(ref), grader_model=grader_model,
                       framework_version=1)  # fmt: skip
        self.ds.append_event(ev)
        if interleaved and v >= RESOLVE_MIN_VALUE - 1e-9:  # §7.7: an interleaved item scoring >= 2 resolves misconceptions on it
            path = self.store.layout.misconceptions_path
            for m in fold(read_records(path, ref.track)):
                if not m["resolved"] and m["subskill"] == subskill and m["node_id"] != ref.id:
                    resolve_misconception(path, m["id"], track=ref.track, node_id=ref.id, event_id=ev.id)
        return ev

    def node_events(self, ref: NodeRef) -> list[Event]:
        return [e for e in self.ds.events() if e.node_id == ref.id]

    def log_misconceptions(self, ref: NodeRef, items, default_subskill: str | None) -> int:
        n = 0
        for m in items or []:
            if isinstance(m, dict) and isinstance(m.get("text"), str) and m["text"].strip():
                sub = m.get("subskill") if isinstance(m.get("subskill"), str) else default_subskill
                if add_misconception(self.store.layout.misconceptions_path, track=ref.track, node_id=ref.id, subskill=sub, text=m["text"]) is not None:
                    n += 1
        return n


class Debrief:
    def __init__(self, store: NodeStore, router, domain_pack: str, tutor: Tutor):
        self.store = store
        self.router = router
        self.domain_pack = domain_pack
        self.ev = Evidence(store, tutor)
        self.oral_section = None  # callable(ref) -> dict, installed by Lifecycle

    # --- state ------------------------------------------------------------------------------------------
    def _state(self, ref: NodeRef) -> dict:
        s = self.store.session(ref, "debrief", {})
        s.setdefault("numeric_done", False)
        s.setdefault("grading", {"status": "none", "items": [], "results": [], "model": None})
        return s

    def _answers(self, ref: NodeRef) -> dict:
        import yaml

        p = ref.path / "prelab_answers.yaml"
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("answers", {}) if p.is_file() else {}

    # --- entering the debrief ------------------------------------------------------------------------------
    def finalize(self, ref: NodeRef) -> None:
        """Numeric prelab events (code), explain grading (heavy, queued if unavailable), debrief.md. Idempotent."""
        with node_lock(ref.track, ref.id):
            st = self._state(ref)
            if not st["numeric_done"]:
                self._numeric_events(ref, st)
                st["numeric_done"] = True
                self.store.save_session(ref, "debrief", st)
            if st["grading"]["status"] == "none":
                self._grade_explain(ref, st)
            self.write_md(ref)

    def _numeric_events(self, ref: NodeRef, st: dict) -> None:
        answers = self._answers(ref)
        cal = []
        for it in self.store.prelab_key(ref):
            if it.get("kind") != "numeric" or it["id"] not in answers:
                continue
            value, pred = float(it["value"]), float(answers[it["id"]])
            tol = tolerance_abs(it)
            err = pred - value
            self.ev.emit(ref, "prelab_numeric", it["subskill"], err=err, tol=tol)
            cal.append({"id": it["id"], "subskill": it["subskill"], "unit": it.get("unit"), "predicted": pred, "actual": value,
                        "error": err, "rel_error": (err / value) if value else None, "tolerance": tol, "within": abs(err) <= tol})  # fmt: skip
        st["calibration"] = cal

    def _grade_inputs(self, ref: NodeRef) -> tuple[list[dict], dict]:
        prompts = self.store.prelab_prompts(ref)
        answers = self._answers(ref)
        items = [{"id": it["id"], "subskill": it["subskill"], "prompt": prompts.get(it["id"], ""), "anchors": {str(k): v for k, v in (it.get("anchors") or {}).items()}}
                 for it in self.store.prelab_key(ref) if it.get("kind") == "explain"]  # fmt: skip
        return items, {i["id"]: str(answers.get(i["id"], "")) for i in items}

    def _grade_explain(self, ref: NodeRef, st: dict) -> None:
        items, answers = self._grade_inputs(ref)
        g = st["grading"]
        if not items:
            g["status"] = "done"
            self.store.save_session(ref, "debrief", st)
            return
        g["items"] = [{"id": i["id"], "subskill": i["subskill"], "prompt": i["prompt"]} for i in items]
        g["answers"] = answers
        system = (PROMPTS / "prelab_explain_grader.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain_pack)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"items": items, "answers": answers}, ensure_ascii=False)}]
        try:
            res = self.router.complete(GRADE_TASK, msgs, schema=GRADE_SCHEMA, node_id=ref.id, track=ref.track)  # a failure is queued by the router (§3.1)
        except LLMError as e:
            g["status"], g["error"] = "pending", str(e)[:300]
            self.store.save_session(ref, "debrief", st)
            refresh_provisional(self.store, ref)
        else:
            err = self._apply_grades(ref, st, res.json, res.model)
            if err:  # unusable grader output: queue it like an outage, the retry asks again
                g["status"], g["error"] = "pending", err
                self.store.save_session(ref, "debrief", st)
                refresh_provisional(self.store, ref)
                self.router.store.enqueue_grade(GRADE_TASK, asdict(_request(msgs, ref.id, ref.track)), ref.id, err, track=ref.track)
        self.store.save_session(ref, "debrief", st)

    def _apply_grades(self, ref: NodeRef, st: dict, out, model: str) -> str | None:
        """Validate the grader's JSON and write the events. -> an error string if unusable (nothing written)."""
        g = st["grading"]
        if not isinstance(out, dict) or not isinstance(out.get("items"), list):
            return "grader output has no items list"
        by_id = {i.get("id"): i for i in out["items"] if isinstance(i, dict)}
        results = []
        for it in g["items"]:
            got = by_id.get(it["id"])
            score = got.get("score") if got else None
            if got is None or isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 3:
                return f"grader gave no valid 0-3 score for item {it['id']}"
            ans = g["answers"].get(it["id"], "")
            quote = got.get("quote") if isinstance(got.get("quote"), str) else ""
            verified = quote != "" and _norm(quote) in _norm(ans)
            results.append({"id": it["id"], "subskill": it["subskill"], "score": score, "quote": quote if verified else "",
                            "quote_verified": verified or quote == "", "rationale": str(got.get("rationale", "")), "prompt": it["prompt"]})  # fmt: skip
        for r in results:
            self.ev.emit(ref, "prelab_explain", r["subskill"], score=r["score"], grader_model=model)
        self.ev.log_misconceptions(ref, out.get("misconceptions"), None)
        g.update(status="done", results=results, model=model)
        g.pop("error", None)
        self.store.save_session(ref, "debrief", st)
        refresh_provisional(self.store, ref)
        return None

    def apply_queued(self, row: dict, result) -> None:
        """Router.retry_pending_grades handler: a queued prelab grading finally succeeded."""
        if row.get("task_type") != GRADE_TASK or row.get("node_id") is None:
            return
        try:
            ref = self.store.find(int(row["node_id"]))
        except LifecycleError:
            return
        with node_lock(ref.track, ref.id):
            st = self._state(ref)
            if st["grading"]["status"] != "pending":
                return  # already applied
            err = self._apply_grades(ref, st, result.json, result.model)
            if err:
                self.router.store.enqueue_grade(GRADE_TASK, json.loads(row["request"]), ref.id, err, track=ref.track)
            else:
                self.write_md(ref)
                autocommit(self.store.root, f"node {ref.id}: grades applied")
            self.store.save_session(ref, "debrief", st)

    # --- view --------------------------------------------------------------------------------------------
    def view(self, ref: NodeRef) -> dict:
        st = self._state(ref)
        fm = self.store.frontmatter(ref)
        g = st["grading"]
        cal = st.get("calibration") or []
        mis = fold(read_records(self.store.layout.misconceptions_path, ref.track))
        proof = self.store.session(ref, "proof", {"submissions": []})
        subs = proof.get("submissions", [])
        return {
            "node_id": ref.id, "title": fm.get("title"), "status": self.store.status(ref), "provisional": bool(fm.get("provisional")),
            "proof": {"passed": any(s.get("verdict") == "pass" for s in subs), "submissions": len(subs),
                      "failed_submissions": sum(1 for s in subs if s.get("verdict") == "fail")},  # fmt: skip
            "checker": public_checker(self.store.session(ref, "checker", None)),  # the latest run (the passing proof run after proof)
            "calibration": {"numeric": cal, "within": sum(1 for c in cal if c["within"]), "total": len(cal)},
            "grades": {"status": "pending" if g["status"] == "pending" else "graded" if g["items"] else "none",
                       "items": [{k: r[k] for k in ("id", "subskill", "prompt", "score", "quote", "rationale")} for r in g.get("results", [])],
                       "pending_items": [{"id": i["id"], "subskill": i["subskill"], "prompt": i["prompt"]} for i in g["items"]] if g["status"] == "pending" else [],
                       "model": g.get("model")},  # fmt: skip
            "oral": self.oral_view(ref),
            "misconceptions": {
                "logged": [{k: m[k] for k in ("id", "text", "subskill")} for m in mis if m["node_id"] == ref.id],
                "resolved": [{k: m[k] for k in ("id", "text", "subskill")} for m in mis if m["resolved"] and m["resolved_by_node"] == ref.id],
            },
            "mastery": self.mastery_deltas(ref),
            "next_action": "finish_debrief" if self.store.status(ref) == "debrief" else "plan_next",
        }  # fmt: skip

    def oral_view(self, ref: NodeRef) -> dict:
        """The oral section of the debrief; the exam itself lives in app/lifecycle/oral.py (set by Lifecycle to avoid an import cycle)."""
        return self.oral_section(ref) if self.oral_section else {"status": "not_graded", "note": ""}

    def mastery_deltas(self, ref: NodeRef) -> list[dict]:
        """Per domain this node produced evidence in: tier and mastery just before its first event vs after its last."""
        events, _ = read_events(self.store.layout.events_path, ref.track)
        idx = [i for i, e in enumerate(events) if e.node_id == ref.id]
        if not idx:
            return []
        before, after = events[: idx[0]], events[: idx[-1] + 1]
        out = []
        for d in sorted({events[i].domain for i in idx}):
            b, a = score_domain(before, d), score_domain(after, d)
            out.append({"domain": d, "tier": a.tier, "mastery_before": round(b.mastery, 1), "mastery_after": round(a.mastery, 1),
                        "delta": round(a.mastery - b.mastery, 1), "exam_unlocked": a.unlocked})  # fmt: skip
        return out

    def write_md(self, ref: NodeRef) -> None:
        """debrief.md (§4.1): a readable snapshot of the view at this moment."""
        v = self.view(ref)
        L = [f"# Debrief: {v['title']}", "", f"Provisional: {'yes (grades pending)' if v['provisional'] else 'no'}", "",
             f"Proof: {'passed' if v['proof']['passed'] else 'not passed'} after {v['proof']['submissions']} submission(s)", "", "## Prediction vs actual", ""]  # fmt: skip
        for c in v["calibration"]["numeric"]:
            L.append(f"- {c['id']} ({c['subskill']}): predicted {c['predicted']:g} {c['unit']}, actual {c['actual']:g} {c['unit']} "
                     f"({'within' if c['within'] else 'outside'} tolerance)")  # fmt: skip
        L += ["", "## Written explanations", ""]
        if v["grades"]["status"] == "pending":
            L.append("Grades pending: they will appear here when grading succeeds.")
        for r in v["grades"]["items"]:
            L.append(f"- {r['id']} ({r['subskill']}): {r['score']}/3" + (f' "{r["quote"]}"' if r["quote"] else "") + f" {r['rationale']}")
        o = v["oral"]
        L += ["", "## Oral", ""]
        if o.get("items"):
            L.append(f"{'Passed' if o['passed'] else 'Not passed'} (mean {'n/a' if o['mean'] is None else format(o['mean'], '.2f')} over non-interleaved items; pass = mean >= 2.0 and no 0).")
            L += [f"- {r['id']} ({r['subskill']}{', interleaved' if r['interleaved'] else ''}): {r['score']}/3" + (f' "{r["quote"]}"' if r["quote"] else "") + f" {r['rationale']}" for r in o["items"]]
            if o.get("remediation"):
                L.append(f"- remediation: {o['remediation'].get('status')}" + (f" ({o['remediation']['node_dir']})" if o["remediation"].get("node_dir") else ""))
        else:
            L.append(o.get("note") or "")
        L += ["", "## Misconceptions", ""]
        L += [f"- logged: {m['text']} ({m['subskill']})" for m in v["misconceptions"]["logged"]]
        L += [f"- resolved: {m['text']} ({m['subskill']})" for m in v["misconceptions"]["resolved"]]
        L += ["", "## Mastery", ""]
        L += [f"- {m['domain']} (T{m['tier']}): {m['mastery_before']} -> {m['mastery_after']} ({m['delta']:+})" for m in v["mastery"]]
        (ref.path / "debrief.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")


def _request(messages: list[dict], node_id: int, track: str):
    from app.llm.types import Request

    return Request(GRADE_TASK, messages, GRADE_SCHEMA, None, None, node_id, None, light=False, track=track)

