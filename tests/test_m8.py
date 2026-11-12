"""M8: outcomes + difficulty control (§7.7), Planner input/validation, SUMMARY.md, proof-failure remediation, misconception lifecycle,
oral audio pre-generation, graph API. Mock LLM, fake audio, fake checker."""

import json
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app.builder.context import interleaving_context
from app.builder.gate import GateContext, _interleaving
from app.datastore.cache import rebuild_db
from app.datastore.layout import DataLayout
from app.datastore.misconceptions import add_misconception, fold, read_records
from app.lifecycle.graph import graph_view
from app.lifecycle.outcomes import EXCLUDED, FAILURE, PENDING, SUCCESS, Outcome, classify, read_outcomes
from app.lifecycle.summary import TOKEN_CAP, assemble, estimate_tokens, valid_entry
from app.main import create_app
from app.onboarding.framework import Framework
from app.planner.difficulty import DIAGNOSTIC, HOLD, LOWER, RAISE, check_options, difficulty_from, read_difficulty, unfamiliar_subskills
from app.planner.service import Planner, PlannerError, validate_options
from app.voice.service import Voice
from app.voice.stt import VoiceUnavailable
from tests.helpers import TRACK as T, make_data
from tests.test_builder_m5 import _diag, _opt, _router, data  # noqa: F401 (data = fixture)
from tests.test_lifecycle import ANSWERS, GRADES, SyncJobs, make, to_lab
from tests.test_oral import CLEAN, FULL, KEY, NODE, THIN, FakeRecorder, FakeSpeaker, FakeTranscriber, grades, oral_env, run_exam, to_oral  # noqa: F401
from tests.test_onboarding import Scripted, template_framework


def mk(root, nid, *, kind="challenge", status="done", proof=("pass",), oral=True, oral_rem=None, proof_rem=None, parents=(),
       subs=("matching.l_network",), track=T):
    """A node with the session files outcomes are read from. oral: True/False = graded passed/failed, None = not graded."""
    lay = DataLayout(root, track)
    d = lay.node_dir(nid, f"n{nid}")
    d.mkdir(parents=True, exist_ok=True)
    fm = {"id": nid, "slug": f"n{nid}", "title": f"Node {nid}", "kind": kind, "domain": subs[0].split(".")[0], "subskills": list(subs), "tier": 3,
          "status": status, "parents": list(parents)}  # fmt: skip
    (d / "node.md").write_text("---\n" + yaml.safe_dump(fm) + "---\nbrief\n", encoding="utf-8")
    sess = lay.sessions_dir / d.name
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "proof.json").write_text(json.dumps({"submissions": [{"n": i + 1, "verdict": v} for i, v in enumerate(proof)], "remediation": proof_rem}))
    (sess / "oral.json").write_text(json.dumps({"grading": {"status": "none" if oral is None else "done", "passed": oral}, "remediation": oral_rem}))


def O(i, result, kind="challenge", reason=None):
    return Outcome(i, kind, "done", result, reason)


# ---- outcomes (§7.7 success definition) ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status, proof, oral, oral_rem, proof_rem, too_hard, result, reason", [
    ("done", {"submissions": [{"verdict": "pass"}]}, {"grading": {"status": "done", "passed": True}}, None, None, False, SUCCESS, None),
    ("done", {"submissions": [{"verdict": "fail"}, {"verdict": "pass"}]}, {"grading": {"status": "done", "passed": True}}, None, None, False, SUCCESS, None),
    ("done", {"submissions": [{"verdict": "pass"}]}, {"grading": {"status": "done", "passed": False}}, None, None, False, FAILURE, "oral_failed"),
    ("done", {"submissions": [{"verdict": "pass"}]}, {"grading": {"status": "done", "passed": True}, "remediation": {"status": "ready"}}, None, None, False, FAILURE, "oral_failed"),
    ("proof", {"submissions": [{"verdict": "fail"}] * 3, "remediation": {"status": "building"}}, {}, None, None, False, FAILURE, "proof_failed"),
    ("proof", {"submissions": [{"verdict": "fail"}] * 3, "remediation": None}, {}, None, None, False, PENDING, None),  # offer declined / not answered
    ("lab", {}, {}, None, None, False, PENDING, None),
    ("debrief", {"submissions": [{"verdict": "pass"}]}, {"grading": {"status": "pending"}}, None, None, False, PENDING, None),  # grading not landed
    ("rescoped", {}, {}, None, None, False, EXCLUDED, None),
    ("abandoned", {}, {}, None, None, False, EXCLUDED, None),
    ("rescoped", {}, {}, None, None, True, FAILURE, "too_hard"),  # "too hard" is a failure (§7.3), even for a finished node
    ("done", {"submissions": [{"verdict": "pass"}]}, {"grading": {"status": "done", "passed": True}}, None, None, True, FAILURE, "too_hard"),
])  # fmt: skip
def test_classify(status, proof, oral, oral_rem, proof_rem, too_hard, result, reason):
    o = classify(7, "challenge", status, proof, oral, too_hard)
    assert (o.result, o.reason) == (result, reason)


def test_read_outcomes_from_files_incl_too_hard_rescope(data):
    mk(data, 1)
    mk(data, 2, oral=False)
    mk(data, 3, status="rescoped", proof=(), oral=None)
    mk(data, 4, status="lab", proof=(), oral=None)
    mk(data, 5, kind="remediation", parents=[2])
    DataLayout(data, T).rescopes_path.write_text(json.dumps({"node_id": 3, "reason_class": "too_hard", "counts_as_failure": True}) + "\n"
                                                 + json.dumps({"node_id": 4, "reason_class": "broken_env", "counts_as_failure": False}) + "\n")
    rebuild_db(data)
    out = read_outcomes(data, T)
    assert {i: o.label for i, o in out.items()} == {1: "success", 2: "oral_failed", 3: "too_hard", 4: "in_progress", 5: "success"}


# ---- difficulty control (§7.7) ---------------------------------------------------------------------------------------------------------


def seq(*results, kind="challenge"):
    return {i: O(i, r, kind) for i, r in enumerate(results, 1)}


@pytest.mark.parametrize("results, mode, n, s", [
    ([], DIAGNOSTIC, 0, 0),
    ([SUCCESS, SUCCESS], DIAGNOSTIC, 2, 2),  # fewer than 3 rated nodes: diagnostic tier at default sizes
    ([SUCCESS] * 3, RAISE, 3, 3),
    ([SUCCESS, SUCCESS, FAILURE], LOWER, 3, 2),  # 0.667 < 0.70
    ([SUCCESS] * 5 + [FAILURE], HOLD, 6, 5),  # 0.833 inside [0.70, 0.85]
    ([SUCCESS] * 6 + [FAILURE], RAISE, 7, 6),  # 0.857 > 0.85
    ([SUCCESS] * 6 + [FAILURE] * 2, HOLD, 8, 6),  # 0.75
    ([SUCCESS] * 5 + [FAILURE] * 3, LOWER, 8, 5),  # 0.625
    ([FAILURE] * 4 + [SUCCESS] * 8, RAISE, 8, 8),  # only the LAST 8 count: the old failures dropped out
    ([SUCCESS] * 8 + [FAILURE] * 3, LOWER, 8, 5),
])
def test_rolling_rate_and_mode(results, mode, n, s):
    d = difficulty_from(seq(*results))
    assert (d.mode, d.n, d.successes) == (mode, n, s)


def test_only_rated_challenge_nodes_count():
    outs = {1: O(1, SUCCESS), 2: O(2, SUCCESS), 3: O(3, FAILURE, "remediation"), 4: O(4, FAILURE, "tier_exam"), 5: O(5, PENDING), 6: O(6, EXCLUDED),
            7: O(7, SUCCESS)}  # fmt: skip
    d = difficulty_from(outs)
    assert (d.n, d.successes, d.mode) == (3, 3, RAISE) and [i for i, _ in d.window] == [1, 2, 7]
    inp = d.to_input()
    assert inp["success_rate"] == 1.0 and inp["target"] == [0.7, 0.85] and inp["rated_nodes"] == 3


def test_read_difficulty_from_the_track_files(data):
    for i in (1, 2, 3):
        mk(data, i)
    mk(data, 4, oral=False)
    mk(data, 5, kind="remediation", oral=False)  # never counts
    rebuild_db(data)
    d = read_difficulty(data, T)
    assert (d.mode, d.n, d.successes) == (HOLD, 4, 3)  # 0.75; the remediation node is not rated
