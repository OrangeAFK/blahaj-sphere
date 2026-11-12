"""Node outcomes (SPEC §7.7): the success/failure record difficulty control, the Planner and the graph read.

success = proof passed AND oral passed AND no remediation was spawned AND the node was not rescoped as "too hard".
Everything is derived from files (sessions/<node>/{proof,oral}.json, rescopes.jsonl, the node's status), so it is reproducible.

result    meaning
success   all of the above
failure   `reason`: oral_failed | proof_failed (3 failed submissions, remediation spawned) | too_hard (rescoped as too hard)
pending   still in progress, or the oral grading has not landed yet: no verdict, so it is not counted
excluded  abandoned, or rescoped for a reason other than "too hard": carries no evidence, not counted
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.builder.context import cache_rows
from app.datastore.layout import DataLayout

SUCCESS, FAILURE, PENDING, EXCLUDED = "success", "failure", "pending", "excluded"
FAILURE_REASONS = ("oral_failed", "proof_failed", "too_hard")


@dataclass(frozen=True)
class Outcome:
    id: int
    kind: str
    status: str
    result: str
    reason: str | None = None

    @property
    def label(self) -> str:
        """One word for the Planner and the graph: success, oral_failed, proof_failed, too_hard, in_progress, abandoned, rescoped."""
        if self.result == FAILURE:
            return self.reason or "failed"
        return {PENDING: "in_progress", EXCLUDED: self.status}.get(self.result, self.result)


def _json(path: Path) -> dict:
    try:
        out = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _too_hard(lay: DataLayout) -> set[int]:
    """Nodes the learner rescoped as too hard (rescopes.jsonl counts_as_failure, §7.3), whatever came of the rescope job."""
    out: set[int] = set()
    if not lay.rescopes_path.is_file():
        return out
    for raw in lay.rescopes_path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("counts_as_failure") and isinstance(r.get("node_id"), int):
            out.add(r["node_id"])
    return out


def _live(remediation) -> bool:
    return isinstance(remediation, dict) and bool(remediation.get("status"))


def classify(node_id: int, kind: str, status: str, proof: dict, oral: dict, too_hard: bool) -> Outcome:
    def out(result: str, reason: str | None = None) -> Outcome:
        return Outcome(node_id, kind, status, result, reason)

    if too_hard:
        return out(FAILURE, "too_hard")
    if status in ("abandoned", "rescoped"):
        return out(EXCLUDED)
    grading = oral.get("grading") or {}
    oral_graded = grading.get("status") == "done"
    if oral_graded and grading.get("passed") is False:
        return out(FAILURE, "oral_failed")
    if _live(proof.get("remediation")):  # the learner took the remediation offer after 3 failed submissions
        return out(FAILURE, "proof_failed")
    if _live(oral.get("remediation")):
        return out(FAILURE, "oral_failed")
    proof_passed = any(s.get("verdict") == "pass" for s in proof.get("submissions") or [])
    if oral_graded and grading.get("passed") is True and proof_passed:
        return out(SUCCESS)
    return out(PENDING)


def read_outcomes(root: Path, track: str) -> dict[int, Outcome]:
    """Every non-diagnostic node of the track -> its Outcome."""
    lay = DataLayout(Path(root), track)
    hard = _too_hard(lay)
    outs: dict[int, Outcome] = {}
    for r in cache_rows(root, track, "SELECT id, dir, kind, status FROM nodes WHERE track = ? AND kind != 'diagnostic' ORDER BY id"):
        sess = lay.sessions_dir / r["dir"]
        outs[r["id"]] = classify(r["id"], r["kind"] or "", r["status"] or "", _json(sess / "proof.json"), _json(sess / "oral.json"), r["id"] in hard)
    return outs
