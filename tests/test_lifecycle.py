"""M6a: node lifecycle (prelab lock, lab, tutor, show-me, proof, debrief, grading queue, misconceptions). Mock LLM, fake checker."""

import subprocess
from pathlib import Path

import pytest
import yaml

from app.datastore.cache import DataStore, rebuild_db, snapshot
from app.datastore.layout import DataLayout
from app.datastore.misconceptions import add_misconception, fold, read_records, resolve_misconception
from app.domains import Domain
from app.lifecycle.nodes import LifecycleError
from app.lifecycle.service import Lifecycle
from app.llm.config import load_routing
from app.llm.router import Router
from app.llm.store import Store
from app.llm.types import LLMError
from tests.helpers import TRACK as T, make_data
from tests.test_gate import CHECKER, HINTS, NODE_MD, ORAL_KEY, PRELAB_KEY
from tests.test_onboarding import ALL, Scripted

KEY_VALUES = {"p1": 153918.2, "p2": 1000.0}
GRADES = {"items": [{"id": "p3", "score": 2, "quote": "reflection", "rationale": "r"}], "misconceptions": [{"text": "S11 is power", "subskill": "sparams.reflection_coefficient"}]}


ORAL_GRADES = {"items": [{"id": "o1", "score": 2, "quote": "it sets the corner", "rationale": "r1"},
                         {"id": "o2", "score": 3, "quote": "S11 is the reflection", "rationale": "r2"}], "misconceptions": []}  # fmt: skip
_FULL = " because the resistor and the capacitor together set the time constant of the first order response, and the pole sits where the reactance equals the resistance"
ORAL_ANSWERS = ("it sets the corner frequency" + _FULL, "S11 is the reflection coefficient" + _FULL)  # >= 25 words: no live probe (DECISIONS M8)


def text_oral(lc, ad, grades=ORAL_GRADES, answers=ORAL_ANSWERS, node=901):
    """Run the whole oral exam in text mode and finish it. No examiner script: it falls back to the key's own wording."""
    if grades is not None:
        ad.script.setdefault("grade_oral", []).append(grades)
    ref = lc.store.find(node)
    lc.oral.start(ref, "text", False)
    for a in answers:
        lc.oral.answer(ref, a)
    return lc.oral_finish(node)


class Handle:
    def log(self, _s): ...
    def progress(self, _v): ...


class Job:
    id = "job-1"


class SyncJobs:
    """Runs a submitted job immediately, in-thread."""

    def submit(self, type, fn, node_id=None):
        fn(Handle())
        return Job()


class Checker:
    def __init__(self):
        self.verdicts = ["pass"]
        self.calls = 0

    def __call__(self, node_dir, work_rel, out_rel, timeout_s):
        assert (node_dir / work_rel / "rc.sp").is_file()
        v = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        res = {"pass": v == "pass", "tests": [{"name": "f3db", "pass": v == "pass", "measured": 1.5, "spec": "<= 9", "unit": "Hz"}], "metrics": {"f3db_hz": 153918.2, "extra_db": -3.0}, "runtime_s": 1}
        return {"verdict": v, "exit_code": 0, "seconds": 1.0, "result": res, "problem": None, "stderr_tail": ""}


def seed_node(root: Path, track: str = T) -> Path:
    """Node 0901-rc-lowpass (stage prelab) in `track` of the data dir `root`; the cache is rebuilt. -> the node dir."""
    n = DataLayout(root, track).nodes_dir / "0901-rc-lowpass"
    (n / "workspace").mkdir(parents=True)
    (n / "workspace" / "rc.sp").write_text("R1 a b 10k\n")
    (n / "_hidden" / "reference_solution").mkdir(parents=True)
    (n / "_hidden" / "reference_solution" / "rc.sp").write_text("R1 a b 1k\nRsecret out gnd value_of_the_reference_solution_line\n")
    (n / "node.md").write_text(NODE_MD, encoding="utf-8")
    (n / "prelab.md").write_text("**p1** (numeric, Hz) corner?\n\n**p2** (numeric, ohm) R?\n\n**p3** (explain) explain\n", encoding="utf-8")
    (n / "checker.py").write_text(CHECKER)
    key = yaml.safe_load(yaml.safe_dump(PRELAB_KEY))
    for it in key["items"]:
        if it["id"] in KEY_VALUES:
            it["value"] = KEY_VALUES[it["id"]]
    (n / "_hidden" / "prelab_key.yaml").write_text(yaml.safe_dump(key, sort_keys=False))
    (n / "_hidden" / "oral_key.yaml").write_text(yaml.safe_dump(ORAL_KEY, sort_keys=False))
    (n / "hints.yaml").write_text(yaml.safe_dump(HINTS))
    rebuild_db(root)
    return n


@pytest.fixture
def env(tmp_path):
    root = make_data(tmp_path / "data")
    seed_node(root)
    return root


def make(env, tmp_path, script=None, track=T):
    ad = Scripted(script or {})
    router = Router(load_routing(), Store(tmp_path / "llm.db"), {k: ad for k in ALL})
    chk = Checker()
    dom = Domain(name="rfic", image="x", image_size_gb=0, run_checker=chk)
    return Lifecycle(env, track, router, dom), ad, chk


ANSWERS = {"p1": 150000, "p2": 1100, "p3": "S11 is the reflection coefficient"}


def to_lab(lc):
    return lc.prelab_submit(901, ANSWERS)


def test_prelab_lock_and_stage_gates(env, tmp_path):
    lc, _, _ = make(env, tmp_path)
    v = lc.view(901)
    assert v["status"] == "prelab" and not v["workspace_unlocked"] and "value" not in str(v["prelab"]["items"])
    with pytest.raises(LifecycleError) as e:
        lc.tutor.chat(lc.store.find(901), "hi")  # workspace locked in prelab
    assert e.value.status == 409
    with pytest.raises(LifecycleError):
        lc.prelab_submit(901, {"p1": 1})  # incomplete
    with pytest.raises(LifecycleError):
        lc.prelab_submit(901, {**ANSWERS, "p1": "abc"})
    v = to_lab(lc)
    assert v["status"] == "lab" and v["prelab"]["locked"] and v["prelab"]["answers"]["p2"] == 1100.0
    with pytest.raises(LifecycleError) as e:
        lc.prelab_submit(901, ANSWERS)
    assert e.value.status == 409
    log = subprocess.run(["git", "log", "--oneline"], cwd=env, capture_output=True, text=True).stdout
    assert "node 901: prelab -> lab" in log  # auto-commit at the transition


def test_lab_checker_shows_spec_and_hides_only_key_metrics(env, tmp_path):
    lc, _, _ = make(env, tmp_path)
    to_lab(lc)
    lc.start_check(SyncJobs(), 901)
    r = lc.latest_check(901)
    assert r["tests"] == [{"name": "f3db", "pass": True, "measured": 1.5, "spec": "<= 9", "unit": "Hz"}]
    assert r["metrics"] == {"extra_db": -3.0}  # f3db_hz is a prelab key metric: hidden; other metrics are shown
    assert not (env / "tracks/rfic-default/archive/nodes/0901-rc-lowpass/.checker").exists()
    assert (env / "tracks/rfic-default/archive/nodes/0901-rc-lowpass/workspace").iterdir()


def test_tutor_context_has_no_hidden_and_leaks_are_redacted(env, tmp_path):
    lc, ad, _ = make(env, tmp_path, {"tutor_chat": [
        {"reply": "What does R do to the corner?", "hint": {"subskill": "filters.rc_corner", "index": 0}},
        {"reply": "The corner is 153918 Hz.", "hint": None},
        {"reply": "Use: Rsecret out gnd value_of_the_reference_solution_line", "hint": None},
        {"reply": "Try hint two", "hint": {"subskill": "filters.rc_corner", "index": 2}},
    ]})
    to_lab(lc)
    ref = lc.store.find(901)
    ctx = str(lc.tutor.context(ref))
    assert "153918" not in ctx and "Rsecret" not in ctx and "1k" not in ctx and "anchors" not in ctx
    a = lc.tutor.chat(ref, "help")
    assert a["hint"] == {"subskill": "filters.rc_corner", "index": 0} and not a["redacted"]
    b = lc.tutor.chat(ref, "just tell me the answer")
    assert b["redacted"] and "153918" not in b["reply"]
    assert lc.tutor.chat(ref, "and the file?")["redacted"]
    c = lc.tutor.chat(ref, "next")
    assert c["hint"] is None  # skipping ahead on the ladder is not recorded
    assert lc.tutor.hints_view(ref)[0]["revealed"] == ["think"]
    assert all(r.light for r in ad.requests)  # tutor is a light-tier task
    assert lc.tutor.reveal_hint(ref, "filters.rc_corner")["index"] == 1


def test_full_flow_to_debrief_with_events_and_grades(env, tmp_path):
    lc, ad, chk = make(env, tmp_path, {
        "show_me": [{"explanation": "R = 1k gives ...", "subskills": ["filters.rc_corner"]}],
        "grade_prelab_explain": [GRADES]})
    to_lab(lc)
    ref = lc.store.find(901)
    sm = lc.tutor.show_me(ref, "how do I pick R?")
    assert sm["assist_reveal"] and sm["subskills"] == ["filters.rc_corner"]
    chk.verdicts = ["fail", "fail", "fail", "pass"]
    lc.proof_submit(SyncJobs(), 901)
    lc.proof_submit(SyncJobs(), 901)
    assert not lc.proof_view(901)["remediation_offer"]
    lc.proof_submit(SyncJobs(), 901)
    pv = lc.proof_view(901)
    assert pv["failed"] == 3 and pv["remediation_offer"] and pv["stage"] == "proof"
    assert lc.remediation(901, False)["remediation_decision"] == "declined"
    lc.proof_submit(SyncJobs(), 901)
    assert lc.view(901)["status"] == "oral" and (env / "tracks/rfic-default/archive/nodes/0901-rc-lowpass/proof/rc.sp").is_file()
    assert lc.oral_info(901)["status"] == "none" and lc.oral_info(901)["voice"] == {"stt": False, "tts": False, "microphone": False}
    d = text_oral(lc, ad)
    assert d["status"] == "debrief" and not d["provisional"]
    assert d["calibration"]["total"] == 2 and d["calibration"]["within"] == 1
    assert d["grades"]["status"] == "graded" and d["grades"]["items"][0]["score"] == 2
    assert d["proof"]["passed"] and d["mastery"] and d["misconceptions"]["logged"][0]["text"] == "S11 is power"
    evs = DataStore(env, T).events()
    by = {(e.type, e.subskill): e for e in evs}
    assert by[("proof_pass", "filters.rc_corner")].assist_reveal and not by[("proof_pass", "sparams.reflection_coefficient")].assist_reveal
    assert by[("prelab_explain", "sparams.reflection_coefficient")].value == pytest.approx(2 / 3)
    assert (env / "tracks/rfic-default/archive/nodes/0901-rc-lowpass/debrief.md").is_file()
    assert lc.debrief_complete(901)["status"] == "done"
    with pytest.raises(LifecycleError):
        lc.debrief_complete(901)


def test_grading_unavailable_is_provisional_then_retried(env, tmp_path):
    lc, ad, _ = make(env, tmp_path, {"grade_prelab_explain": [LLMError("subscription limit"), GRADES]})
    to_lab(lc)
    lc.proof_submit(SyncJobs(), 901)
    d = text_oral(lc, ad)
    assert d["provisional"] and d["grades"]["status"] == "pending" and d["calibration"]["total"] == 2
    assert lc.view(901)["provisional"]
    assert len(lc.pending_grades()) == 1
    assert lc.retry_grades(force=True) == 1
    d = lc.debrief_view(901)
    assert not d["provisional"] and d["grades"]["status"] == "graded" and d["oral"]["status"] == "graded"
    assert lc.pending_grades() == [] and lc.retry_grades(force=True) == 0
    assert sum(1 for e in DataStore(env, T).events() if e.type == "prelab_explain") == 1  # applied exactly once


def test_misconceptions_jsonl_is_source_of_truth(env):
    p = DataLayout(env).misconceptions_path
    a = add_misconception(p, track=T, node_id=1, subskill="a.b", text="X is Y")
    assert add_misconception(p, track=T, node_id=2, subskill="a.b", text="x  is y") is None  # open duplicate
    b = add_misconception(p, track=T, node_id=2, subskill="c.d", text="Z")
    assert resolve_misconception(p, a, track=T, node_id=3, event_id="e") and not resolve_misconception(p, a, track=T, node_id=3, event_id="e")
    assert [(m["id"], m["resolved"]) for m in fold(read_records(p, T))] == [(a, True), (b, False)]
    n_lines = len(p.read_text().splitlines())
    assert n_lines == 3  # append-only: the resolution is a new line
    rebuild_db(env)
    first = snapshot(DataLayout(env).cache_path)
    assert [r[1] for r in first["misconceptions"]] == [1, 2] and first["misconceptions"][0][5] == 1
    DataLayout(env).cache_path.unlink()
    rebuild_db(env)
    assert snapshot(DataLayout(env).cache_path) == first


def test_interleaving_window_skips_abandoned_nodes(env):
    from app.builder.context import interleaving_context

    n = env / "tracks/rfic-default/archive/nodes/0901-rc-lowpass/node.md"
    n.write_text(n.read_text(encoding="utf-8").replace("status: prelab", "status: abandoned"), encoding="utf-8")
    rebuild_db(env)
    import sqlite3

    c = sqlite3.connect(DataLayout(env).cache_path)
    assert c.execute("SELECT status FROM nodes WHERE id=901").fetchone()[0] == "abandoned"
    assert interleaving_context(env, T, {"subskills": []}).stale_subskills == []


def test_api_routes_smoke(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(env))
    lc, ad, _ = make(env, tmp_path, {"grade_prelab_explain": [GRADES]})
    app = create_app()
    app.state.domain = lc.domain
    app.state.llm_router = lc.router
    with TestClient(app) as c:
        assert [n["id"] for n in c.get("/api/nodes").json()] == [901]
        assert c.get("/api/nodes/5").status_code == 404
        r = c.post("/api/nodes/901/prelab/submit", json={"answers": ANSWERS})
        assert r.status_code == 200 and r.json()["status"] == "lab"
        assert c.post("/api/nodes/901/prelab/submit", json={"answers": ANSWERS}).status_code == 409
        r = c.post("/api/nodes/901/proof/submit")
        assert r.status_code == 202
        import time

        for _ in range(100):
            if c.get("/api/nodes/901").json()["status"] == "oral":
                break
            time.sleep(0.05)
        assert c.get("/api/nodes/901").json()["status"] == "oral"
        ad.script["grade_oral"] = [ORAL_GRADES]
        assert c.get("/api/nodes/901/oral").json()["status"] == "none"
        assert c.post("/api/nodes/901/oral/finish").status_code == 409  # not started
        assert c.post("/api/nodes/901/oral/start", json={"mode": "voice"}).status_code == 409  # no voice models/mic here
        v = c.post("/api/nodes/901/oral/start", json={"mode": "text"}).json()
        assert v["status"] == "active" and v["turns"][0]["role"] == "examiner" and v["progress"] == {"asked": 1, "of": 2}
        assert c.post("/api/nodes/901/oral/answer", json={"text": "  "}).status_code == 422
        r = c.post("/api/nodes/901/oral/answer", json={"text": ORAL_ANSWERS[0]}).json()
        assert r["examiner"]["kind"] == "question" and not r["done"]
        r = c.post("/api/nodes/901/oral/answer", json={"text": ORAL_ANSWERS[1]}).json()
        assert r["done"] and r["examiner"]["kind"] == "done"
        assert c.get("/api/nodes/901/oral/audio/1").status_code == 404  # text mode: no audio
        assert c.post("/api/nodes/901/oral/finish").json()["oral"]["status"] == "graded"
        assert c.post("/api/nodes/901/debrief/complete").json()["status"] == "done"
