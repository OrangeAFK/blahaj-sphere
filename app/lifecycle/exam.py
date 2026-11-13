"""Tier exams (SPEC §8.4) and the Competency screen's data (SPEC §11).

A domain's exam unlocks at mastery >= 65 AND >= 3 distinct subskills evidenced at the current tier (the engine's `exam_unlocked`; no time
conditions). Starting it creates a `kind=tier_exam` node of the active track at stage `oral`: no prelab, lab or proof, because the exam is
only the 6-question oral. The questions and their hidden key (anchors, probes, key_terms) are written by one heavy call (`build_exam`), validated
in code with one repair attempt, and stored like any other oral key, so the oral flow, the examiner and the pinned grader are unchanged.
Grading and the pass rule (mean >= 2.3, no 0) live in app/lifecycle/oral.py; the tier-up is derived from the six `tier_exam_item` events by the
engine, so it is rebuildable. Everything is scoped to the track of the NodeStore.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

import yaml

from app.builder.context import load_framework
from app.builder.service import alloc_lock, next_node_id, slugify
from app.competency.engine import EXAM_ITEMS, EXAM_PASS_MEAN, MAX_TIER, UNLOCK_MASTERY, UNLOCK_SUBSKILLS, DomainScore, score_domain, tier_history
from app.datastore.cache import rebuild_db
from app.datastore.events import read_events
from app.datastore.misconceptions import fold, read_records
from app.lifecycle.nodes import LifecycleError, NodeRef, NodeStore, autocommit
from app.llm.types import LLMError

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
TASK = "build_exam"
SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "brief": {"type": "string"}, "items": {"type": "array", "minItems": EXAM_ITEMS, "maxItems": EXAM_ITEMS}},
          "required": ["title", "brief", "items"]}  # fmt: skip
IN_PROGRESS = ("oral", "debrief")  # an exam node that is not finished (it starts at oral)
SIZE_EST_MIN = 20
MIN_KEY_TERMS, MAX_KEY_TERMS = 3, 8
_locks: dict[tuple[str, str], threading.Lock] = {}
_guard = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _lock(track: str, domain: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault((track, domain), threading.Lock())


def missing_for_unlock(score: DomainScore) -> list[str]:
    """What stands between the learner and the exam, in words ([] = unlocked)."""
    out = []
    if score.tier >= MAX_TIER:
        return [f"T{MAX_TIER} is the top tier: there is no exam above it"]
    if score.mastery < UNLOCK_MASTERY - 1e-9:
        out.append(f"mastery {score.mastery:.0f}% of {UNLOCK_MASTERY:.0f}% needed")
    if len(score.subskills_evidenced) < UNLOCK_SUBSKILLS:
        out.append(f"{len(score.subskills_evidenced)} of {UNLOCK_SUBSKILLS} subskills evidenced at T{score.tier}")
    return out


def validate_exam(raw, allowed: set[str]) -> tuple[dict | None, list[str]]:
    """The model's exam -> ({title, brief, items}, []) or (None, errors phrased so the model can repair them)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["the output must be a JSON object"]
    for k in ("title", "brief"):
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            errors.append(f"`{k}` must be a non-empty string")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) != EXAM_ITEMS:
        return None, errors + [f"`items` must be a list of exactly {EXAM_ITEMS} objects"]
    clean = []
    for i, it in enumerate(items, 1):
        w = f"item {i}"
        if not isinstance(it, dict):
            errors.append(f"{w}: must be an object")
            continue
        if it.get("id") != f"e{i}":
            errors.append(f"{w}: id must be 'e{i}'")
        if it.get("subskill") not in allowed:
            errors.append(f"{w}: subskill {it.get('subskill')!r} is not one of {sorted(allowed)}")
        if not isinstance(it.get("question"), str) or not it["question"].strip():
            errors.append(f"{w}: `question` must be a non-empty string")
        anchors = it.get("anchors")
        if not isinstance(anchors, dict) or sorted(str(k) for k in anchors) != ["0", "1", "2", "3"] or not all(isinstance(v, str) and v.strip() for v in anchors.values()):
            errors.append(f"{w}: `anchors` needs the keys '0','1','2','3', each a non-empty sentence")
        probes = it.get("probes")
        if not isinstance(probes, list) or not 1 <= len(probes) <= 3 or not all(isinstance(p, str) and p.strip() for p in probes):
            errors.append(f"{w}: `probes` must be 1-3 non-empty strings")
        terms = it.get("key_terms")
        distinct = {t.strip().lower() for t in terms if isinstance(t, str) and t.strip()} if isinstance(terms, list) else set()
        if not isinstance(terms, list) or len(distinct) != len(terms) or not MIN_KEY_TERMS <= len(distinct) <= MAX_KEY_TERMS:
            errors.append(f"{w}: `key_terms` must be {MIN_KEY_TERMS}-{MAX_KEY_TERMS} distinct non-empty strings")
        if not errors:
            clean.append({"id": it["id"], "subskill": it["subskill"], "question": it["question"].strip(),
                          "anchors": {str(k): anchors[k].strip() for k in sorted(anchors)}, "probes": [p.strip() for p in probes],
                          "interleaved": False, "key_terms": [t.strip() for t in terms]})  # fmt: skip
    if not errors and len({c["subskill"] for c in clean}) < UNLOCK_SUBSKILLS:
        errors.append(f"the items must cover at least {UNLOCK_SUBSKILLS} distinct subskills")
    return ({"title": raw["title"].strip(), "brief": raw["brief"].strip(), "items": clean}, []) if not errors else (None, errors)


class TierExams:
    def __init__(self, store: NodeStore, router, domain_pack: str):
        self.store, self.router, self.domain_pack = store, router, domain_pack

    @property
    def track(self) -> str:
        return self.store.track

    # --- reads ------------------------------------------------------------------------------------------
    def _events(self):
        return read_events(self.store.layout.events_path, self.track)[0]

    def _framework(self):
        fw = load_framework(self.store.root, self.track)
        if fw is None:
            raise LifecycleError(409, "approve the competency framework first (onboarding)")
        return fw

    def exam_nodes(self, domain: str) -> list[dict]:
        """Every tier-exam node of the domain, oldest first: {node_id, dir, tier, status, passed, mean, tier_up}."""
        out = []
        for ref in self.store.all():
            fm = self.store.frontmatter(ref)
            if fm.get("kind") != "tier_exam" or fm.get("domain") != domain:
                continue
            g = self.store.session(ref, "oral", {}).get("grading") or {}
            out.append({"node_id": ref.id, "dir": ref.name, "tier": fm.get("tier"), "status": str(fm.get("status")),
                        "passed": g.get("passed") if g.get("status") == "done" else None, "mean": g.get("mean"),
                        "tier_up": bool((g.get("exam") or {}).get("tier_up"))})  # fmt: skip
        return out

    def competency(self) -> dict:
        """The Competency screen (§11): per domain the tier descriptors, mastery, the unlock condition, subskills and the exam state."""
        fw = self._framework()
        events = self._events()
        doms = []
        for d in fw.domains:
            s = score_domain(events, d.id)
            exams = self.exam_nodes(d.id)
            active = next((e for e in reversed(exams) if e["status"] in IN_PROGRESS), None)
            missing = missing_for_unlock(s)
            state = "max_tier" if s.tier >= MAX_TIER else "in_progress" if active else "available" if s.unlocked else "locked"
            evidenced = set(s.subskills_evidenced)
            names = list(d.subskills) + sorted(evidenced - set(d.subskills))  # emergent subskills (§8.1) show up once they have evidence
            doms.append({
                "domain": d.id, "name": d.name, "scope": d.scope, "tier": s.tier, "mastery": round(s.mastery, 1), "n_window": s.n_window,
                "tiers": [{"tier": t, "descriptor": d.tiers[t], "state": "passed" if t < s.tier else "current" if t == s.tier else "locked"} for t in sorted(d.tiers)],
                "unlock": {"mastery_needed": UNLOCK_MASTERY, "subskills_needed": UNLOCK_SUBSKILLS, "subskills_evidenced": len(evidenced),
                           "mastery_ok": s.mastery >= UNLOCK_MASTERY - 1e-9, "subskills_ok": len(evidenced) >= UNLOCK_SUBSKILLS},
                "subskills": [{"name": n, "evidenced": n in evidenced} for n in names],
                "exam": {"state": state, "missing": missing if state in ("locked", "max_tier") else [], "node_id": active["node_id"] if active else None,
                         "pass_mean": EXAM_PASS_MEAN, "questions": EXAM_ITEMS, "attempts": exams,
                         "tier_ups": [{"tier": st.tier, "via": st.via} for st in tier_history(events, d.id)[1:]]},  # fmt: skip
            })
        return {"track": self.track, "domains": doms}

    def offers(self) -> list[dict]:
        """Planner options for the unlocked, not-yet-started exams of the track (code-made: the model never writes them)."""
        out = []
        for d in self.competency()["domains"]:
            if d["exam"]["state"] != "available":
                continue
            u = d["unlock"]
            out.append({"title": f"Tier {d['tier']} exam: {d['name']}", "kind": "tier_exam", "domain": d["domain"], "tier": d["tier"],
                        "subskills": [s["name"] for s in d["subskills"] if s["evidenced"]][:5], "size_est_min": SIZE_EST_MIN,
                        "why_now": f"Mastery {d['mastery']:.0f}% with {u['subskills_evidenced']} subskills evidenced at T{d['tier']}: the exam is unlocked. "
                                   f"Pass it (6 oral questions, mean >= {EXAM_PASS_MEAN}, no 0) to move up to T{d['tier'] + 1}."})  # fmt: skip
        return out

    # --- start ------------------------------------------------------------------------------------------
    def start(self, domain_id: str) -> dict:
        """Create the exam node for an unlocked domain (or return the one already in progress). -> {node_id, dir, reused}."""
        fw = self._framework()
        dom = next((d for d in fw.domains if d.id == domain_id), None)
        if dom is None:
            raise LifecycleError(404, f"no domain {domain_id!r} in this track's framework")
        with _lock(self.track, domain_id):
            active = next((e for e in reversed(self.exam_nodes(domain_id)) if e["status"] in IN_PROGRESS), None)
            if active:
                return {"node_id": active["node_id"], "dir": active["dir"], "reused": True}
            events = self._events()
            score = score_domain(events, domain_id)
            miss = missing_for_unlock(score)
            if miss:
                raise LifecycleError(409, f"the {dom.name} exam is not unlocked: " + "; ".join(miss))
            exam = self._write(dom, score, events)
            ref = self._create(dom, score, exam)
        return {"node_id": ref.id, "dir": ref.name, "reused": False}

    def _inputs(self, dom, score: DomainScore) -> dict:
        goals = self.store.layout.goals_path
        recent = [self.store.frontmatter(r) for r in self.store.all()]
        mis = [{"text": m["text"], "subskill": m["subskill"]} for m in fold(read_records(self.store.layout.misconceptions_path, self.track))
               if not m["resolved"] and str(m["subskill"] or "").startswith(dom.id + ".")]  # fmt: skip
        return {
            "domain": {"id": dom.id, "name": dom.name}, "tier": score.tier, "tier_descriptor": dom.tiers[score.tier],
            "next_tier_descriptor": dom.tiers.get(score.tier + 1), "subskills": list(dom.subskills), "evidenced_subskills": list(score.subskills_evidenced),
            "recent_nodes": [str(f.get("title")) for f in recent if f.get("domain") == dom.id and f.get("kind") != "tier_exam"][-8:],
            "open_misconceptions": mis, "goals": goals.read_text(encoding="utf-8") if goals.is_file() else "",
        }  # fmt: skip

    def _write(self, dom, score: DomainScore, events) -> dict:
        """Heavy call -> a validated exam, one repair attempt (like the Planner). LLM outage -> 502; nothing is created."""
        allowed = set(dom.subskills) | set(score.subskills_evidenced)
        system = {"role": "system", "content": (PROMPTS / "exam_writer.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain_pack)}
        user = {"role": "user", "content": json.dumps(self._inputs(dom, score), ensure_ascii=False)}
        msgs, errors = [system, user], []
        for _ in range(2):
            try:
                res = self.router.complete(TASK, msgs, schema=SCHEMA, track=self.track)
            except LLMError as e:
                raise LifecycleError(502, f"could not write the exam: {e}") from None
            exam, errors = validate_exam(res.json, allowed)
            if exam is not None:
                return exam
            msgs = [system, user, {"role": "assistant", "content": res.text},
                    {"role": "user", "content": "That exam is invalid:\n- " + "\n- ".join(errors) + "\nReturn the corrected JSON only."}]
        raise LifecycleError(502, "exam invalid after retry: " + "; ".join(errors))

    def _create(self, dom, score: DomainScore, exam: dict) -> NodeRef:
        lay = self.store.layout
        subs = sorted({it["subskill"] for it in exam["items"]})
        vocab = list(dict.fromkeys(t for it in exam["items"] for t in it["key_terms"]))[:30]
        with alloc_lock:
            nid = next_node_id(lay)
            d = lay.node_dir(nid, slugify(f"tier-exam-{dom.id}-t{score.tier}"))
            (d / "_hidden").mkdir(parents=True)
        fm = {"id": nid, "track": self.track, "slug": d.name.split("-", 1)[1], "title": exam["title"], "parents": [], "kind": "tier_exam", "domain": dom.id,
              "subskills": subs, "tier": score.tier, "size_est_min": SIZE_EST_MIN, "pdk": "none", "run": [], "proof_artifacts": [], "vocab": vocab,
              "status": "oral", "provisional": False, "created": _now(), "references": []}  # fmt: skip
        (d / "node.md").write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n# {exam['title']}\n\n{exam['brief']}\n", encoding="utf-8", newline="\n")
        (d / "_hidden" / "oral_key.yaml").write_text(yaml.safe_dump({"items": exam["items"]}, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
        rebuild_db(self.store.root)
        autocommit(self.store.root, f"node {nid}: tier exam created ({dom.id} T{score.tier})")
        return NodeRef(nid, d, self.track)

