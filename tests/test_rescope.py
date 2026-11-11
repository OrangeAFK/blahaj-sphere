"""M6a.1: Rescope (SPEC §7.3), both paths against a mock LLM and a fake checker: replace, edit, and edit that fails and is restored."""

import json
from pathlib import Path

import pytest
import yaml

from app.builder.service import BuildError, BuildService
from app.datastore.layout import DataLayout
from app.lifecycle.nodes import LifecycleError
from app.lifecycle.service import Lifecycle
from tests.test_builder_m5 import OPTION, Agent, FakeBuilder, _opt, _router, data, fake_domain, review  # noqa: F401 (data = fixture)
from tests.helpers import TRACK as T
from tests.test_lifecycle import ANSWERS, Handle, Job


class Jobs:
    """Runs a job inline; a failing job is recorded (like the real manager marking it failed), not raised."""

    def __init__(self):
        self.errors: list[Exception] = []

    def submit(self, type, fn, node_id=None):
        try:
            fn(Handle())
        except Exception as e:  # noqa: BLE001
            self.errors.append(e)
        return Job()


class PrelabTamperer(FakeBuilder):
    """A Builder that rewrites prelab.md, which a rescope edit must not do (the learner's answers are locked against it)."""

    def __call__(self, req):
        out = super().__call__(req)
        (Path(req.cwd) / "prelab.md").write_text("**p1** a different question?\n**p2** R?\n**p3** explain\n", encoding="utf-8")
        return out


def setup(data, tmp_path, rescope_reply, builder_cls=FakeBuilder, n_builds=4):
    dom, state = fake_domain(tmp_path)
    fb = FakeBuilder(state)  # the initial release is always a clean build
    edit_fb = builder_cls(state)
    ad = Agent({"build_challenge": [fb] + [edit_fb] * n_builds, "review_build": [review()] * (n_builds + 1), "rescope": [rescope_reply]})
    router = _router(tmp_path, ad)
    rec = BuildService(data, T, router, dom).run(BuildService(data, T, router, dom).prepare(OPTION))
    assert rec["passed"]
    lc = Lifecycle(data, T, router, dom)
    lc.prelab_submit(1, ANSWERS)
    ws = data / "tracks/rfic-default/archive/nodes/0001-set-an-rc-low-pass-corner/workspace/rc.sp"
    ws.write_text("R1 a b 5k\n")  # the learner's own work
    return lc, ad, fb, ws


def test_rescope_replace_builds_new_node_then_marks_old_rescoped(data, tmp_path):
    reply = {"action": "replace", "reason_class": "too_hard", "changes": "split off the foundation",
             "option": _opt(title="Foundations of matching", subskills=["matching.l_network"], tier=1, size_est_min=20)}
    lc, ad, fb, _ = setup(data, tmp_path, reply)
    fb.interleave = "noise.figure_cascade"
    for b in ad.script["build_challenge"][1:]:
        b.interleave = "noise.figure_cascade"
    jobs = Jobs()
    out = lc.rescope(jobs, 1, "too_hard", "this is far too hard for me")
    assert not jobs.errors, jobs.errors
    assert out["action"] == "replace" and out["node_id"] == 2 and out["node_dir"].startswith("0002-")
    assert lc.view(1)["status"] == "rescoped" and lc.view(2)["status"] == "prelab"
    fm = lc.store.frontmatter(lc.store.find(2))
    assert fm["parents"] == [1]
    log = [json.loads(l) for l in (data / "tracks/rfic-default/rescopes.jsonl").read_text().splitlines()]
    by = {l["event"]: l for l in log}  # order depends on whether the job already finished when "requested" is written
    assert set(by) == {"requested", "replaced"} and by["requested"]["counts_as_failure"] and by["replaced"]["replacement"] == out["node_dir"]
    sent = json.loads(next(r for r in ad.requests if r.task_type == "rescope").messages[1]["content"])
    assert sent["reason_class"] == "too_hard" and "anchors" not in json.dumps(sent) and "1k" not in json.dumps(sent)  # nothing hidden
    with pytest.raises(LifecycleError) as e:  # a rescoped node is out of the lifecycle
        lc.rescope(jobs, 1, "too_hard", "again")
    assert e.value.status == 409


def test_replace_that_fails_to_build_leaves_the_old_node_alone(data, tmp_path):
    reply = {"action": "replace", "changes": "x", "option": _opt(tier=1)}
    lc, ad, _, _ = setup(data, tmp_path, reply)
    for b in ad.script["build_challenge"][1:]:
        b.broken = 9  # every attempt of the replacement fails the gate
    jobs = Jobs()
    lc.rescope(jobs, 1, "wrong_direction", "I want noise instead")
    assert jobs.errors and isinstance(jobs.errors[0], BuildError)
    assert lc.view(1)["status"] == "lab"  # not rescoped: there is no replacement


def test_rescope_edit_regates_in_place_and_keeps_learner_progress(data, tmp_path):
    reply = {"action": "edit", "reason_class": "broken_env", "changes": "fix the broken include path in rc.sp", "option": None}
    lc, ad, _, ws = setup(data, tmp_path, reply)
    node = ws.parent.parent
    answers_before = (node / "prelab_answers.yaml").read_text()
    jobs = Jobs()
    out = lc.rescope(jobs, 1, "broken_env", "the netlist will not load")
    assert not jobs.errors, jobs.errors
    assert out["action"] == "edit" and out["node_id"] == 1
    prompt = ad.requests[-2 if ad.requests[-1].task_type == "review_build" else -1].messages[0]["content"]
    assert "Rescope EDIT" in prompt and "fix the broken include path" in prompt
    assert lc.view(1)["status"] == "lab" and (node / "prelab_answers.yaml").read_text() == answers_before  # stage and locked answers kept
    assert ws.read_text() == "R1 a b 10k\n"  # workspace reset to the original starter
    backups = list((data / "builds/rfic-default/0001-set-an-rc-low-pass-corner").glob("rescope-*"))
    assert any(b.is_dir() and (b / "node/workspace/rc.sp").read_text() == "R1 a b 5k\n" for b in backups)  # the learner's old work is kept
    rec = json.loads(next((data / "builds/rfic-default/0001-set-an-rc-low-pass-corner").glob("rescope-*.json")).read_text())
    assert rec["passed"] and rec["edit"]["changes"].startswith("fix the broken") and rec["learner_workspace_backup"]
    assert not lc.store.session(lc.store.find(1), "rescope", {}).get("editing")
    assert {json.loads(l)["event"] for l in (data / "tracks/rfic-default/rescopes.jsonl").read_text().splitlines()} == {"requested", "edited"}
    assert lc.tutor.hints_view(lc.store.find(1))  # the node is usable again


def test_rescope_edit_that_cannot_pass_is_restored_exactly(data, tmp_path):
    reply = {"action": "edit", "reason_class": "objection", "changes": "change the prelab question", "option": None}
    lc, ad, _, ws = setup(data, tmp_path, reply, builder_cls=PrelabTamperer)
    node = ws.parent.parent
    before = {p.relative_to(node).as_posix(): p.read_bytes() for p in sorted(node.rglob("*")) if p.is_file()}
    jobs = Jobs()
    lc.rescope(jobs, 1, "objection", "the prelab question is wrong")
    assert jobs.errors and isinstance(jobs.errors[0], BuildError) and "prelab" in str(jobs.errors[0]).lower()
    after = {p.relative_to(node).as_posix(): p.read_bytes() for p in sorted(node.rglob("*")) if p.is_file()}
    assert after == before  # every file, including the learner's workspace edit and the locked answers
    assert ws.read_text() == "R1 a b 5k\n" and lc.view(1)["status"] == "lab" and lc.view(1)["prelab"]["locked"]
    rec = json.loads(next((data / "builds/rfic-default/0001-set-an-rc-low-pass-corner").glob("rescope-*.json")).read_text())
    assert not rec["passed"] and rec["restored_from"] and len(rec["attempts"]) == 3
    assert not lc.store.session(lc.store.find(1), "rescope", {}).get("editing")  # unblocked after the failure


def test_rescope_validation(data, tmp_path):
    lc, _, _, _ = setup(data, tmp_path, {"action": "edit", "changes": "c"})
    with pytest.raises(LifecycleError) as e:
        lc.rescope(Jobs(), 1, "bored", "x")
    assert e.value.status == 422
    with pytest.raises(LifecycleError):
        lc.rescope(Jobs(), 1, "too_hard", "  ")
