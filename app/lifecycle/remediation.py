"""Remediation children (SPEC §7.5, §7.6): a failed oral or 3 failed proof submissions -> the Planner proposes ONE kind=remediation option
-> a normal build job releases it with parents=[failed node]. Shared by the oral exam and the proof stage.

The state of the attempt ({status: building|ready|failed|needed, job_id, child_id, node_dir, title, option, error}) is kept by the caller in
its own session file (sessions/<node>/oral.json or proof.json, key `remediation`) through the `load`/`save` callbacks, so the same code
serves both origins and a finished build can flip the parent's state to `ready` from inside the job.
"""

from datetime import datetime
from typing import Callable

from app.builder.service import BuildError, BuildService
from app.datastore.cache import rebuild_db
from app.lifecycle.nodes import NodeRef, NodeStore, append_jsonl, autocommit, node_lock
from app.llm.types import LLMError
from app.planner.service import Planner, PlannerError


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Remediator:
    def __init__(self, store: NodeStore, router, domain, jobs):
        self.store, self.router, self.domain, self.jobs = store, router, domain, jobs

    def spawn(self, ref: NodeRef, source: str, ctx: dict, load: Callable[[NodeRef], dict], save: Callable[[NodeRef, dict], None]) -> dict:
        """Planner option + build job for `ref`'s remediation child. `ctx` = the Planner's `remediation` input
        ({parent, failed_items, misconceptions}). `load(ref)` returns the caller's session dict and `save(ref, dict)` stores it; the
        record lives at its `remediation` key. -> the record (never raises for Planner/Builder/LLM problems: status "failed" + error)."""
        if self.jobs is None:  # nothing can build here (no job manager): recorded so the UI can offer it later
            return {"status": "needed"}
        parent_id = ref.id
        try:
            option = Planner(self.store.root, ref.track, self.router, self.domain.name).remediation_option(ctx)
            builder = BuildService(self.store.root, ref.track, self.router, self.domain)

            def done(rec: dict) -> None:  # the child is released: link it to its parent, flip the parent's state
                new_dir = self.store.layout.nodes_dir / rec["node_dir"]
                self.store.set_scalar(NodeRef(int(rec["node_dir"].split("-", 1)[0]), new_dir, ref.track), "parents", [parent_id])
                rebuild_db(self.store.root)
                with node_lock(ref.track, parent_id):
                    p = self.store.find(parent_id)
                    s2 = load(p)
                    s2["remediation"] = {**(s2.get("remediation") or {}), "status": "ready"}
                    save(p, s2)
                autocommit(self.store.root, f"node {parent_id}: remediation child {rec['node_dir']} released")

            job, plan = builder.start(self.jobs, option, on_success=done)
        except (PlannerError, BuildError, LLMError) as e:
            return {"status": "failed", "error": str(getattr(e, "message", e))[:300]}
        append_jsonl(self.store.layout.remediation_path, {"ts": _now(), "node_id": parent_id, "decision": f"{source}_fail", "child": plan.name})
        return {"status": "building", "job_id": job.id, "child_id": plan.node_id, "node_dir": plan.name, "title": option["title"], "option": option}

    def refresh(self, rem: dict | None) -> bool:
        """A build job that died leaves the record at "building": look the job up and surface the failure. -> True if `rem` was changed."""
        if not rem or rem.get("status") != "building" or self.jobs is None:
            return False
        store = getattr(self.jobs, "store", None)
        job = store.get(rem["job_id"]) if store is not None else None
        if job is not None and str(getattr(job, "status", "")) == "failed":
            rem.update(status="failed", error=str(getattr(job, "error", "") or "the build failed")[:300])
            return True
        return False
