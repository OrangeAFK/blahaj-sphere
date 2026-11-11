"""Node lifecycle (SPEC §7.2-7.5, §7.7): prelab lock, lab checker runs, proof, oral stub, debrief, rescope.

All LLM access goes through the Router; the checker runs through the domain (`Domain.run_checker`). Handlers are sync: FastAPI
runs them in its threadpool, and long work (checker, rescope builds) goes to the job manager.
"""

import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from app.builder.context import domain_scores, load_framework
from app.builder.gate import CHECKER_TIMEOUT_S, LONG_RUN_S
from app.builder.service import BuildError, BuildService
from app.datastore.cache import rebuild_db
from app.domains import Domain
from app.lifecycle.debrief import Debrief
from app.lifecycle.nodes import NEXT_ACTION, RESCOPABLE, WORKING, LifecycleError, NodeRef, NodeStore, append_jsonl, autocommit, node_lock
from app.lifecycle.tutor import Tutor, public_checker
from app.llm.types import LLMError
from app.planner.service import validate_option

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
REMEDIATION_AFTER = 3  # §7.5: after 3 failed submissions, offer a remediation node
REASONS = ("too_hard", "wrong_direction", "objection", "broken_env")
RESCOPE_SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}, "reason_class": {"type": "string"}, "changes": {"type": "string"},
                                                   "option": {"type": ["object", "null"]}}, "required": ["action", "changes"]}  # fmt: skip
_running: set[tuple[int, str]] = set()
_running_lock = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Lifecycle:
    def __init__(self, data_root: Path, router, domain: Domain | None):
        if domain is None:
            raise LifecycleError(501, "no active domain")
        self.store = NodeStore(data_root)
        self.router = router
        self.domain = domain
        self.tutor = Tutor(self.store, router, domain.name)
        self.debrief = Debrief(self.store, router, domain.name, self.tutor)

    @property
    def root(self) -> Path:
        return self.store.root

    # --- views --------------------------------------------------------------------------------------------
    def summary(self, ref: NodeRef) -> dict:
        fm = self.store.frontmatter(ref)
        st = str(fm.get("status"))
        return {"id": ref.id, "dir": ref.name, "title": fm.get("title"), "kind": fm.get("kind"), "domain": fm.get("domain"), "tier": fm.get("tier"),
                "status": st, "provisional": bool(fm.get("provisional")), "next_action": NEXT_ACTION.get(st, "plan_next")}  # fmt: skip

    def list_nodes(self) -> list[dict]:
        return [self.summary(r) for r in self.store.all()]

    def view(self, node_id: int) -> dict:
        ref = self.store.find(node_id)
        fm = self.store.frontmatter(ref)
        st = str(fm.get("status"))
        prompts = self.store.prelab_prompts(ref)
        ans_path = ref.path / "prelab_answers.yaml"
        answers = (yaml.safe_load(ans_path.read_text(encoding="utf-8")) or {}) if ans_path.is_file() else None
        sim = fm.get("sim_runtime_s")
        return {
            **self.summary(ref),
            "subskills": [str(s) for s in fm.get("subskills") or []], "size_est_min": fm.get("size_est_min"), "sim_runtime_s": sim,
            "long_run_warning": isinstance(sim, int) and sim > LONG_RUN_S, "brief": self.store.brief(ref),
            "prelab": {
                "markdown": (ref.path / "prelab.md").read_text(encoding="utf-8") if (ref.path / "prelab.md").is_file() else "",
                "items": [{"id": it["id"], "kind": it["kind"], "unit": it.get("unit"), "prompt": prompts.get(it["id"], "")} for it in self.store.prelab_key(ref)],
                "locked": answers is not None or bool(self.store.session(ref, "rescope", {}).get("editing")), "submitted": answers.get("submitted") if answers else None,
                "answers": answers.get("answers") if answers else None,
            },
            "proof_artifacts": [str(a) for a in fm.get("proof_artifacts") or []],
            "workspace_unlocked": st != "prelab", "workspace": str(ref.path / "workspace") if st != "prelab" else None,
            "rescopable": st in RESCOPABLE,
        }  # fmt: skip

    # --- prelab (§7.2) -------------------------------------------------------------------------------------
    def prelab_submit(self, node_id: int, answers: dict) -> dict:
        ref = self.store.find(node_id)
        with node_lock(ref.id):
            self.store.require(ref, "prelab")
            path = ref.path / "prelab_answers.yaml"
            if path.exists():
                raise LifecycleError(409, "prelab answers are locked")
            key = {it["id"]: it for it in self.store.prelab_key(ref)}
            extra, missing = sorted(set(answers) - set(key)), sorted(set(key) - set(answers))
            if extra or missing:
                raise LifecycleError(422, f"answers must cover exactly the prelab items {sorted(key)} (missing {missing}, unknown {extra})")
            clean: dict = {}
            for iid, it in key.items():
                v = answers[iid]
                if it["kind"] == "numeric":
                    try:
                        if isinstance(v, bool):
                            raise ValueError
                        f = float(v)
                    except (TypeError, ValueError):
                        raise LifecycleError(422, f"{iid}: a number in {it.get('unit')} is required") from None
                    if not math.isfinite(f):
                        raise LifecycleError(422, f"{iid}: a finite number in {it.get('unit')} is required")
                    clean[iid] = f
                else:
                    if not isinstance(v, str) or not v.strip():
                        raise LifecycleError(422, f"{iid}: an answer is required (write 'I don't know' to skip)")
                    clean[iid] = v.strip()
            doc = {"submitted": _now(), "answers": clean, "units": {i: key[i].get("unit") for i in key if key[i]["kind"] == "numeric"}}
            with open(path, "x", encoding="utf-8", newline="\n") as f:  # "x": a second write can never replace the lock
                f.write(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
            starter = self.store.session_dir(ref) / "starter"  # the pristine workspace, for a rescope edit (§7.3)
            if not starter.exists():
                shutil.copytree(ref.path / "workspace", starter)
            self.store.transition(ref, "lab")
        return self.view(node_id)

    # --- lab (§7.3) ----------------------------------------------------------------------------------------
    def open_folder(self, node_id: int) -> dict:
        ref = self.store.find(node_id)
        self.store.require(ref, *WORKING)
        ws = ref.path / "workspace"
        if sys.platform == "win32":
            os.startfile(ws)  # noqa: S606 - opens the learner's own workspace folder in Explorer
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ws)])
        else:
            subprocess.Popen(["xdg-open", str(ws)])
        return {"path": str(ws)}

    def _check(self, ref: NodeRef, kind: str) -> dict:
        """Run the checker on a throwaway copy of the workspace (like the gate), so runs never litter it with outputs."""
        assert self.domain.run_checker is not None
        copy_rel, out_rel = f".checker/{kind}_run", f".checker/{kind}_result.json"
        shutil.rmtree(ref.path / copy_rel, ignore_errors=True)
        shutil.copytree(ref.path / "workspace", ref.path / copy_rel)
        t0 = time.monotonic()
        try:
            run = self.domain.run_checker(ref.path, copy_rel, out_rel, CHECKER_TIMEOUT_S)
        finally:
            shutil.rmtree(ref.path / copy_rel, ignore_errors=True)
            (ref.path / out_rel).unlink(missing_ok=True)
            d = ref.path / ".checker"
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        verdict = run.get("verdict")
        return {
            "ts": _now(), "kind": kind, "verdict": verdict, "passed": verdict == "pass", "tests": result.get("tests") or [],
            "metrics": result.get("metrics") or {}, "seconds": round(time.monotonic() - t0, 1),
            "detail": ((run.get("problem") or run.get("stderr_tail") or "")[-1500:] or None) if verdict == "error" else None,
        }  # fmt: skip

    def _claim(self, ref: NodeRef, what: str) -> None:
        with _running_lock:
            if (ref.id, what) in _running:
                raise LifecycleError(409, f"a {what} run is already in progress for node {ref.id}")
            _running.add((ref.id, what))

    @staticmethod
    def _release(ref: NodeRef, what: str) -> None:
        with _running_lock:
            _running.discard((ref.id, what))

    def start_check(self, jobs, node_id: int):
        """Lab "Run checker": unlimited, as a job; the result is the latest entry of GET .../checker."""
        ref = self.store.find(node_id)
        self.store.require(ref, *WORKING)
        self._claim(ref, "checker")

        def run(handle) -> None:
            try:
                handle.log("running the checker on a copy of your workspace")
                rec = self._check(ref, "lab")
                self.store.save_session(ref, "checker", rec)
                handle.log(f"checker: {rec['verdict']}")
            finally:
                self._release(ref, "checker")

        try:
            return jobs.submit("checker", run, node_id=ref.name)
        except Exception:
            self._release(ref, "checker")
            raise

    def latest_check(self, node_id: int) -> dict | None:
        return public_checker(self.store.session(self.store.find(node_id), "checker", None))

    # --- proof (§7.5) --------------------------------------------------------------------------------------
    def _proof_state(self, ref: NodeRef) -> dict:
        s = self.store.session(ref, "proof", {})
        return {"submissions": s.get("submissions", []), "remediation_offer": s.get("remediation_offer", False),
                "remediation_decision": s.get("remediation_decision")}  # fmt: skip

    def proof_submit(self, jobs, node_id: int):
        """Copy the declared proof artifacts to proof/, then run the checker as a job. No retry penalty."""
        ref = self.store.find(node_id)
        with node_lock(ref.id):
            st = self.store.require(ref, *WORKING)
            fm = self.store.frontmatter(ref)
            arts = [str(a) for a in fm.get("proof_artifacts") or []]
            ws = (ref.path / "workspace").resolve()
            missing = []
            for a in arts:
                src = (ws / a).resolve()
                if ".." in Path(a).parts or Path(a).is_absolute() or ws not in src.parents or not src.is_file():
                    missing.append(a)
            if not arts or missing:
                raise LifecycleError(422, f"proof artifacts missing from the workspace: {missing or 'none declared'}")
            ps = self._proof_state(ref)
            if any(s.get("status") == "running" for s in ps["submissions"]):
                raise LifecycleError(409, "a proof check is already running")
            shutil.rmtree(ref.path / "proof", ignore_errors=True)
            for a in arts:
                dst = ref.path / "proof" / a
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ws / a, dst)
            n = len(ps["submissions"]) + 1
            ps["submissions"].append({"n": n, "ts": _now(), "status": "running", "verdict": None, "tests": [], "seconds": None, "detail": None})
            self.store.save_session(ref, "proof", ps)
            if st == "lab":
                self.store.transition(ref, "proof")
        job = jobs.submit("proof_check", lambda h: self.run_proof_check(node_id, n, h), node_id=ref.name)
        return job, n

    def run_proof_check(self, node_id: int, n: int, handle=None) -> dict:
        """The proof checker job. Pass -> proof_pass events + Oral; fail -> stay in Proof (no penalty); error -> not counted."""
        ref = self.store.find(node_id)
        log = handle.log if handle else (lambda _s: None)
        try:
            rec = self._check(ref, "proof")
        except Exception as e:  # noqa: BLE001 - the submission must not stay "running" forever
            rec = {"verdict": "error", "passed": False, "tests": [], "seconds": None, "detail": f"{e.__class__.__name__}: {e}"[:1500], "metrics": {}}
            self._finish_submission(ref, n, rec)
            raise
        log(f"proof check: {rec['verdict']}")
        self.store.save_session(ref, "checker", rec)
        self._finish_submission(ref, n, rec)
        return rec

    def _finish_submission(self, ref: NodeRef, n: int, rec: dict) -> None:
        with node_lock(ref.id):
            ps = self._proof_state(ref)
            sub = next(s for s in ps["submissions"] if s["n"] == n)
            sub.update(status="done", verdict=rec["verdict"], tests=public_checker(rec)["tests"] if rec.get("tests") else [], seconds=rec.get("seconds"),
                       detail=rec.get("detail"))  # fmt: skip
            if rec["verdict"] == "pass":
                self._record_proof_pass(ref)
                self.store.save_session(ref, "proof", ps)
                self.store.transition(ref, "oral")
                return
            if rec["verdict"] == "fail" and sum(1 for s in ps["submissions"] if s.get("verdict") == "fail") >= REMEDIATION_AFTER:
                ps["remediation_offer"] = True
            self.store.save_session(ref, "proof", ps)

    def _record_proof_pass(self, ref: NodeRef) -> None:
        """One proof_pass event per subskill of the node (DECISIONS M6a), once per node."""
        if any(e.type == "proof_pass" for e in self.debrief.ev.node_events(ref)):
            return
        for sub in self.store.frontmatter(ref).get("subskills") or []:
            self.debrief.ev.emit(ref, "proof_pass", str(sub))

    def proof_view(self, node_id: int) -> dict:
        ref = self.store.find(node_id)
        ps = self._proof_state(ref)
        fm = self.store.frontmatter(ref)
        return {"stage": str(fm.get("status")), "artifacts": [str(a) for a in fm.get("proof_artifacts") or []], "submissions": ps["submissions"],
                "failed": sum(1 for s in ps["submissions"] if s.get("verdict") == "fail"), "remediation_offer": ps["remediation_offer"],
                "remediation_decision": ps["remediation_decision"]}  # fmt: skip

    def remediation(self, node_id: int, accept: bool) -> dict:
        """Record the learner's answer to the remediation offer. The remediation node itself is built by M8's Planner."""
        ref = self.store.find(node_id)
        with node_lock(ref.id):
            ps = self._proof_state(ref)
            if not ps["remediation_offer"]:
                raise LifecycleError(409, f"a remediation offer appears after {REMEDIATION_AFTER} failed submissions")
            ps["remediation_decision"] = "accepted" if accept else "declined"
            self.store.save_session(ref, "proof", ps)
            append_jsonl(self.root / "remediation.jsonl", {"ts": _now(), "node_id": ref.id, "decision": ps["remediation_decision"]})
        return self.proof_view(node_id)

    # --- oral (§7.6): stub until M7 --------------------------------------------------------------------------
    def oral_info(self, node_id: int) -> dict:
        ref = self.store.find(node_id)
        st = self.store.status(ref)
        return {"implemented": False, "milestone": "M7", "stage": st, "modes": ["text"], "can_advance": st == "oral",
                "note": "voice oral and grading arrive in M7; text mode only advances the node to Debrief for testing"}  # fmt: skip

    def oral_advance(self, node_id: int, mode: str) -> dict:
        if mode != "text":
            raise LifecycleError(422, "only mode 'text' exists until M7 (voice oral)")
        ref = self.store.find(node_id)
        with node_lock(ref.id):
            self.store.require(ref, "oral")
            self.store.transition(ref, "debrief", commit=False)
            self.debrief.finalize(ref)
            autocommit(self.root, f"node {ref.id}: oral -> debrief")
        return self.debrief_view(node_id)

    # --- debrief (§7.7) --------------------------------------------------------------------------------------
    def debrief_view(self, node_id: int) -> dict:
        ref = self.store.find(node_id)
        st = self.store.require(ref, "debrief", "done")
        with node_lock(ref.id):
            d = self.debrief._state(ref)
            if st == "debrief" and (not d["numeric_done"] or d["grading"]["status"] == "none"):
                self.debrief.finalize(ref)  # recovers a debrief entry that was interrupted
        return self.debrief.view(ref)

    def debrief_complete(self, node_id: int) -> dict:
        ref = self.store.find(node_id)
        with node_lock(ref.id):
            self.store.require(ref, "debrief")
            self.store.transition(ref, "done")
        return self.summary(ref)

    # --- grading queue (§3.1) ----------------------------------------------------------------------------------
    def retry_grades(self, force: bool = False) -> int:
        """Retry due pending grades (on app start, periodically, or `force` ignoring the backoff). -> number applied."""
        done = self.router.retry_pending_grades(self.debrief.apply_queued, now=float("inf") if force else None)
        return len(done)

    def pending_grades(self) -> list[dict]:
        return [{"id": r["id"], "task_type": r["task_type"], "node_id": r["node_id"], "attempts": r["attempts"], "last_error": r["last_error"]}
                for r in self.router.store.pending_grades()]  # fmt: skip

    # --- rescope (§7.3) ----------------------------------------------------------------------------------------
    def rescope(self, jobs, node_id: int, reason_class: str, reason: str) -> dict:
        ref = self.store.find(node_id)
        stage = self.store.require(ref, *RESCOPABLE)
        reason = reason.strip()
        if reason_class not in REASONS:
            raise LifecycleError(422, f"reason_class must be one of {list(REASONS)}")
        if not reason:
            raise LifecycleError(422, "say why in your own words")
        fw = load_framework(self.root)
        if fw is None:
            raise LifecycleError(409, "no frozen framework")
        scores = domain_scores(self.root)
        fm = self.store.frontmatter(ref)
        goals = self.root / "profile" / "goals.md"
        inp = {
            "node": {"frontmatter": {k: fm.get(k) for k in ("id", "title", "kind", "domain", "subskills", "tier", "size_est_min", "pdk", "run", "proof_artifacts")},
                     "brief": self.store.brief(ref)},  # fmt: skip
            "prelab": (ref.path / "prelab.md").read_text(encoding="utf-8"), "checker_result": public_checker(self.store.session(ref, "checker", None)),
            "reason_class": reason_class, "reason": reason, "scores": scores, "goals": goals.read_text(encoding="utf-8") if goals.is_file() else "",
        }  # fmt: skip
        system = {"role": "system", "content": (PROMPTS / "rescoper.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain.name)}
        user = {"role": "user", "content": json.dumps(inp, ensure_ascii=False)}
        msgs, errors = [system, user], []
        out = None
        for _ in range(2):  # one repair attempt, like the Planner
            res = self.router.complete("rescope", msgs, schema=RESCOPE_SCHEMA, node_id=ref.id)
            j, errors = res.json, []
            if not isinstance(j, dict) or j.get("action") not in ("edit", "replace") or not isinstance(j.get("changes"), str) or not j["changes"].strip():
                errors = ["`action` must be 'edit' or 'replace' and `changes` a non-empty string"]
            elif j["action"] == "replace":
                opt, errors = validate_option(j.get("option"), "option", fw, scores)
                if opt is not None:
                    j["option"] = opt
            if not errors:
                out = j
                break
            msgs = [system, user, {"role": "assistant", "content": res.text}, {"role": "user", "content": "Invalid:\n- " + "\n- ".join(errors) + "\nReturn corrected JSON only."}]
        if out is None:
            raise LifecycleError(502, "rescoper output invalid after retry: " + "; ".join(errors))
        try:
            builder = BuildService(self.root, self.router, self.domain)
            return self._apply_rescope(builder, jobs, ref, stage, reason_class, reason, out)
        except BuildError as e:
            raise LifecycleError(e.status, e.message) from None

    def _apply_rescope(self, builder: BuildService, jobs, ref: NodeRef, stage: str, reason_class: str, reason: str, out: dict) -> dict:
        log = {"ts": _now(), "node_id": ref.id, "stage": stage, "reason_class": reason_class, "reason": reason, "action": out["action"],
               "changes": out["changes"], "counts_as_failure": reason_class == "too_hard"}  # §7.3/§7.7: "too hard" is a failure for difficulty control
        if out["action"] == "replace":
            def done(rec: dict) -> None:  # the old node is rescoped only once its replacement exists
                new_dir = self.root / "archive" / "nodes" / rec["node_dir"]
                self.store.set_scalar(NodeRef(ref.id, new_dir), "parents", [ref.id])
                self.store.set_scalar(ref, "status", "rescoped")
                rebuild_db(self.root)
                append_jsonl(self.root / "rescopes.jsonl", {**log, "event": "replaced", "replacement": rec["node_dir"]})
                autocommit(self.root, f"node {ref.id}: rescoped -> {rec['node_dir']}")

            job, plan = builder.start(jobs, out["option"], on_success=done)
            append_jsonl(self.root / "rescopes.jsonl", {**log, "event": "requested", "replacement": plan.name})
            return {"action": "replace", "changes": out["changes"], "job_id": job.id, "node_dir": plan.name, "node_id": plan.node_id}
        starter = self.store.session_dir(ref) / "starter"
        self.store.save_session(ref, "rescope", {"editing": True, "since": _now()})  # blocks every other action until the job ends
        try:
            job, plan, spec = builder.start_edit(
                jobs, ref.path, out["changes"], starter if starter.is_dir() else None,
                on_success=lambda rec: (append_jsonl(self.root / "rescopes.jsonl", {**log, "event": "edited"}),
                                        autocommit(self.root, f"node {ref.id}: rescope edit")),  # fmt: skip
                on_done=lambda: self.store.save_session(ref, "rescope", {"editing": False}),
            )
        except Exception:
            self.store.save_session(ref, "rescope", {"editing": False})
            raise
        append_jsonl(self.root / "rescopes.jsonl", {**log, "event": "requested"})
        return {"action": "edit", "changes": out["changes"], "job_id": job.id, "node_dir": plan.name, "node_id": ref.id}


__all__ = ["Lifecycle", "LifecycleError", "LLMError"]
