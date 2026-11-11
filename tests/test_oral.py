"""M7: oral exam (adaptive length, examiner flow, grading, pass rule, remediation, voice turns). Mock LLM, fake audio devices."""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from app.datastore.cache import DataStore, rebuild_db
from app.datastore.layout import DataLayout
from app.datastore.misconceptions import fold, read_records
from app.lifecycle.nodes import LifecycleError
from app.lifecycle.oral import CLOSING, oral_verdict
from app.llm.types import LLMError
from app.onboarding.framework import Framework, FrameworkStore
from app.voice.service import Voice
from app.voice.stt import SAMPLE_RATE, Transcript, VoiceUnavailable, build_prompt
from tests.test_builder_m5 import _diag
from tests.test_lifecycle import ANSWERS, GRADES, SyncJobs, env, make, to_lab  # noqa: F401 (env = fixture)
from tests.test_onboarding import template_framework

ANCHORS = {i: f"ANCHOR-SECRET-{i}" for i in range(4)}


def _item(i: int, sub: str, inter: bool = False) -> dict:
    return {"id": f"o{i}", "subskill": sub, "tier": 1, "question": f"Question {i}?", "anchors": {k: f"{v}-o{i}" for k, v in ANCHORS.items()},
            "probes": [f"Probe {i}a?", f"Probe {i}b?"], "interleaved": inter}  # fmt: skip


KEY = [_item(1, "matching.l_network"), _item(2, "noise.figure_cascade"), _item(3, "devices.biasing"), _item(4, "passives.inductor_q"),
       _item(5, "noise.sources"), _item(6, "matching.l_network"), _item(8, "filters.rc_corner"), _item(7, "devices.biasing", True)]  # fmt: skip
CLEAN = {"p1": 150000, "p2": 1002, "p3": "S11 is the reflection coefficient"}  # both numeric items within tolerance
NODE = "0901-rc-lowpass"


@pytest.fixture
def oral_env(env):
    """env (node 901 at stage oral soon) + a frozen framework, a goals file, scores, and a bigger oral key."""
    FrameworkStore(env / "framework").freeze(Framework.model_validate(template_framework()))
    (env / "profile" / "goals.md").write_text("# Goals\n60 GHz LNA\n", encoding="utf-8")
    for dom, sub in (("matching", "matching.l_network"), ("noise", "noise.figure_cascade"), ("devices", "devices.biasing")):
        _diag(env, dom, sub, 1, 2)
    (env / "archive/nodes" / NODE / "_hidden" / "oral_key.yaml").write_text(yaml.safe_dump({"items": KEY}, sort_keys=False))
    rebuild_db(env)
    return env


def to_oral(lc, answers=ANSWERS):
    lc.prelab_submit(901, answers)
    lc.proof_submit(SyncJobs(), 901)
    assert lc.view(901)["status"] == "oral"
    return lc.store.find(901)


def run_exam(lc, ref, n=None, answers=None):
    """Answer until the exam is done (text mode). -> the turns."""
    n = n or 40
    for i in range(n):
        v = lc.oral.view(ref)
        if v["status"] == "complete":
            break
        text = (answers or ["I would use the quality factor and the reflection coefficient"])[min(i, len(answers or [1]) - 1)]
        lc.oral.answer(ref, text)
    return lc.oral.view(ref)["turns"]


def grades(scores: dict[str, int], quote="quality factor", mis=None):
    return {"items": [{"id": k, "score": v, "quote": quote, "rationale": f"r-{k}"} for k, v in scores.items()], "misconceptions": mis or []}


# ---- plan: adaptive length (§7.6) --------------------------------------------------------------------------------------------------


def test_clean_prelab_gives_two_core_plus_the_interleaved_question(oral_env, tmp_path):
    lc, ad, _ = make(oral_env, tmp_path)
    ref = to_oral(lc, CLEAN)
    v = lc.oral.start(ref, "text", False)
    assert v["plan"]["n_core"] == 2 and v["plan"]["n_interleaved"] == 1 and v["progress"]["of"] == 3
    assert lc.oral._state(ref)["plan"]["items"] == ["o1", "o2", "o7"]  # key order, interleaved last
    assert v["turns"][0]["text"] == "Question 1?" and v["turns"][0]["kind"] == "question"


def test_missed_prelab_gives_five_core_missed_subskills_first(oral_env, tmp_path):
    lc, _, _ = make(oral_env, tmp_path)
    ref = to_oral(lc)  # p2 = 1100 vs 1000 +- 5: outside tolerance
    v = lc.oral.start(ref, "text", False)
    assert v["plan"]["n_core"] == 5 and "filters.rc_corner" in v["plan"]["reason"]
    assert lc.oral._state(ref)["plan"]["items"] == ["o8", "o1", "o2", "o3", "o4", "o7"]  # o8 targets the missed subskill


def test_no_interleaved_item_in_the_key_means_core_only(oral_env, tmp_path):
    (oral_env / "archive/nodes" / NODE / "_hidden" / "oral_key.yaml").write_text(yaml.safe_dump({"items": KEY[:3]}, sort_keys=False))
    lc, _, _ = make(oral_env, tmp_path)
    ref = to_oral(lc, CLEAN)
    assert lc.oral.start(ref, "text", False)["plan"] == {"n_core": 2, "n_interleaved": 0, "reason": "all numeric prelab answers were within tolerance"}


# ---- examiner: no anchors, flow owned by code ---------------------------------------------------------------------------------------


def test_examiner_receives_questions_and_probes_but_never_anchors(oral_env, tmp_path):
    lc, ad, _ = make(oral_env, tmp_path, {"oral_dialogue": [{"say": "Question 1?", "item_id": "o1", "kind": "question"},
                                                            {"say": "Why?", "item_id": "o1", "kind": "probe"}]})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    lc.oral.answer(ref, "because of the reflection coefficient")
    reqs = [r for r in ad.requests if r.task_type == "oral_dialogue"]
    assert len(reqs) == 2 and all(r.light for r in reqs)
    blob = json.dumps([r.messages for r in reqs])
    assert "ANCHOR-SECRET" not in blob and "anchors" not in blob and "matching.l_network" not in blob  # nor subskills/tiers
    sent = json.loads(reqs[1].messages[1]["content"])
    assert sent["items"][0] == {"id": "o1", "question": "Question 1?", "probes": ["Probe 1a?", "Probe 1b?"], "interleaved": False}
    assert [i["interleaved"] for i in sent["items"]] == [False, False, True]
    assert [t["role"] for t in sent["transcript"]] == ["examiner", "learner"] and sent["transcript"][0]["item_id"] == "o1"


def test_flow_is_enforced_whatever_the_examiner_says(oral_env, tmp_path):
    script = [
        {"say": "Why do we need that? Question 1?", "item_id": "o1", "kind": "question"},  # opening, fine
        {"say": "Probe one", "item_id": "o1", "kind": "probe"},
        {"say": "Probe two", "item_id": "o1", "kind": "probe"},
        {"say": "Probe three", "item_id": "o1", "kind": "probe"},  # a 3rd follow-up is refused -> o2's key question
        {"say": "all done", "item_id": "o2", "kind": "done"},  # done while o7 is still open -> o7's key question
        {"say": "wrong item", "item_id": "o2", "kind": "question"},  # o7 is the last item: only "done" is acceptable -> closing line
    ]
    lc, _, _ = make(oral_env, tmp_path, {"oral_dialogue": script})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    turns = run_exam(lc, ref)
    ex = [t for t in turns if t["role"] == "examiner"]
    assert [(t["kind"], t["source"]) for t in ex] == [("question", "llm"), ("probe", "llm"), ("probe", "llm"), ("question", "coerced"),
                                                      ("question", "coerced"), ("done", "coerced")]  # fmt: skip
    kinds = [t["kind"] for t in ex]
    assert kinds.count("probe") == 2 and kinds[-1] == "done" and kinds.count("question") == 3  # o1, o2, o7 each asked exactly once
    assert ex[0]["text"] == "Why do we need that? Question 1?"  # the examiner's wording is used when it follows the flow
    questions = [t["text"] for t in ex if t["kind"] == "question"]
    assert questions[1:] == ["Question 2?", "Question 7?"]  # coerced ones use the key wording
    assert lc.oral.view(ref)["status"] == "complete" and lc.oral.view(ref)["can_finish"]
    with pytest.raises(LifecycleError) as e:
        lc.oral.answer(ref, "one more")
    assert e.value.status == 409


def test_examiner_outage_falls_back_to_the_key_wording(oral_env, tmp_path):
    lc, _, _ = make(oral_env, tmp_path)  # no oral_dialogue script: every call fails
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    turns = run_exam(lc, ref)
    ex = [t for t in turns if t["role"] == "examiner"]
    assert [t["text"] for t in ex] == ["Question 1?", "Question 2?", "Question 7?", CLOSING]
    assert {t["source"] for t in ex} == {"fallback"} and ex[-1]["kind"] == "done"


def test_start_resumes_and_blocks_other_stages(oral_env, tmp_path):
    lc, _, _ = make(oral_env, tmp_path)
    with pytest.raises(LifecycleError) as e:
        lc.oral.start(lc.store.find(901), "text", False)  # still prelab
    assert e.value.status == 409
    ref = to_oral(lc, CLEAN)
    a = lc.oral.start(ref, "text", False)
    lc.oral.answer(ref, "an answer")
    b = lc.oral.start(ref, "text", False)  # resume: same exam, same turns
    assert [t["n"] for t in b["turns"]] == [1, 2, 3] and b["plan"] == a["plan"]
    with pytest.raises(LifecycleError) as e:
        lc.oral_finish(901)  # not complete
    assert e.value.status == 409 and "not finished" in e.value.message


# ---- grading, pass rule, evidence ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("scores, passed, mean", [
    ([2, 2], True, 2.0), ([3, 1], True, 2.0), ([3, 0], False, 1.5), ([2, 1], False, 1.5), ([3, 3, 3, 0, 3], False, 2.4), ([1, 3, 2, 2, 2], True, 2.0),
])  # fmt: skip
def test_pass_rule(scores, passed, mean):
    res = [{"score": s, "interleaved": False} for s in scores] + [{"score": 0, "interleaved": True}]  # an interleaved 0 never fails the node
    v = oral_verdict(res)
    assert v["passed"] is passed and v["mean"] == pytest.approx(mean)


def test_pass_writes_oral_items_misconceptions_and_files(oral_env, tmp_path):
    g = grades({"o1": 2, "o2": 3, "o7": 0}, mis=[{"text": "Q only rises with L", "subskill": "matching.l_network"}])
    lc, ad, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [g]})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref, answers=["the quality factor sets it", "the quality factor again", "quality factor"])
    d = lc.oral_finish(901)
    assert d["status"] == "debrief" and not d["provisional"]
    o = d["oral"]
    assert o["status"] == "graded" and o["passed"] is True and o["mean"] == pytest.approx(2.5)
    assert [(i["id"], i["score"], i["interleaved"], i["quote"]) for i in o["items"]] == [("o1", 2, False, "quality factor"), ("o2", 3, False, "quality factor"), ("o7", 0, True, "quality factor")]
    evs = [e for e in DataStore(oral_env).events() if e.type == "oral_item"]
    assert [(e.subskill, e.interleaved, round(e.value, 3), e.weight) for e in evs] == [
        ("matching.l_network", False, 0.667, 1.0), ("noise.figure_cascade", False, 1.0, 1.0), ("devices.biasing", True, 0.0, 1.0)]  # fmt: skip
    mis = fold(read_records(DataLayout(oral_env).misconceptions_path))
    texts = [m["text"] for m in mis if m["node_id"] == 901]
    assert "Q only rises with L" in texts and any("devices.biasing" in t and "Could not answer" in t for t in texts)  # grader's + the interleaved 0
    assert o["remediation"] is None
    assert (oral_env / "archive/nodes" / NODE / "oral.md").read_text().count("**Learner") == 3
    assert "Passed (mean 2.50" in (oral_env / "archive/nodes" / NODE / "debrief.md").read_text()
    # grader input: anchors present, transcript with item ids, frozen prompt as the system message
    req = [r for r in ad.requests if r.task_type == "grade_oral"][0]
    sent = json.loads(req.messages[1]["content"])
    assert sent["items"][0]["anchors"]["3"] == "ANCHOR-SECRET-3-o1" and sent["transcript"][1]["item_id"] == "o1"
    assert req.messages[0]["content"].startswith("# Role\nYou grade an oral exam") and not req.light


def test_failed_oral_logs_misconceptions_and_spawns_a_remediation_child(oral_env, tmp_path, monkeypatch):
    fake = {}

    class FakeBuild:
        def __init__(self, root, router, domain):
            self.root = Path(root)

        def start(self, jobs, option, on_success=None):
            d = self.root / "archive" / "nodes" / "0902-remediate"
            d.mkdir(parents=True, exist_ok=True)
            (d / "node.md").write_text("---\n" + yaml.safe_dump({"id": 902, "slug": "remediate", "kind": option["kind"], "domain": option["domain"],
                                                                   "subskills": option["subskills"], "tier": 1, "status": "prelab"}) + "---\nb\n")  # fmt: skip
            fake.update(option=option, release=lambda: on_success({"node_dir": d.name}))
            return SimpleNamespace(id="job-r"), SimpleNamespace(name=d.name, node_id=902)

    monkeypatch.setattr("app.lifecycle.oral.BuildService", FakeBuild)
    rem_opt = {"title": "Probe the L-match Q", "why_now": "the oral showed a gap", "kind": "remediation", "domain": "matching",
               "subskills": ["matching.l_network"], "tier": 1, "size_est_min": 30}  # fmt: skip
    g = grades({"o1": 0, "o2": 3, "o7": 3}, mis=[{"text": "L-match always raises Q", "subskill": "matching.l_network"}])
    lc, ad, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [g], "plan_branches": [{"options": [rem_opt]}]})
    lc.oral.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _id: SimpleNamespace(status="running", error=None)))
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    d = lc.oral_finish(901)
    o = d["oral"]
    assert o["passed"] is False and o["items"][0]["score"] == 0
    assert o["remediation"]["status"] == "building" and o["remediation"]["node_dir"] == "0902-remediate" and o["remediation"]["child_id"] == 902
    plan_req = [r for r in ad.requests if r.task_type == "plan_branches"][0]
    rem_in = json.loads(plan_req.messages[1]["content"])["remediation"]
    assert rem_in["parent"]["id"] == 901 and rem_in["failed_items"][0]["subskill"] == "matching.l_network" and rem_in["misconceptions"]
    assert fake["option"]["kind"] == "remediation"
    # misconceptions: the grader's, and the weak core answer logged so it is revisited
    texts = [m["text"] for m in fold(read_records(DataLayout(oral_env).misconceptions_path)) if m["node_id"] == 901]
    assert "L-match always raises Q" in texts and not any(t.startswith("Weak answer") for t in texts)  # the grader already named matching.l_network
    assert [json.loads(x)["child"] for x in (oral_env / "remediation.jsonl").read_text().splitlines()] == ["0902-remediate"]
    # the child is released -> linked to the parent
    fake["release"]()
    child = lc.store.frontmatter(lc.store.find(902))
    assert child["parents"] == [901]
    assert lc.oral.debrief_view(ref)["remediation"]["status"] == "ready"
    assert lc.debrief_complete(901)["outcome"] == "done-with-remediation"
    assert lc.summary(lc.store.find(901))["outcome"] == "done-with-remediation"


def test_failed_oral_without_grader_misconceptions_logs_the_weak_cores(oral_env, tmp_path):
    g = grades({"o1": 1, "o2": 2, "o7": 3})  # core mean 1.5
    lc, _, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [g]})  # no jobs: remediation is only recorded
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    d = lc.oral_finish(901)
    assert d["oral"]["passed"] is False and d["oral"]["remediation"] == {"status": "needed"}
    texts = [m["text"] for m in fold(read_records(DataLayout(oral_env).misconceptions_path)) if m["node_id"] == 901]
    weak = [t for t in texts if t.startswith("Weak answer")]
    assert len(weak) == 1 and "matching.l_network" in weak[0]  # only o1 scored < 2 (o2's 2 is not weak)


def test_remediation_failure_is_reported_then_retried(oral_env, tmp_path, monkeypatch):
    class FakeBuild:
        def __init__(self, *a):
            pass

        def start(self, jobs, option, on_success=None):
            return SimpleNamespace(id="job-r"), SimpleNamespace(name="0902-r", node_id=902)

    monkeypatch.setattr("app.lifecycle.oral.BuildService", FakeBuild)
    opt = {"title": "t", "why_now": "w", "kind": "remediation", "domain": "matching", "subskills": ["matching.l_network"], "tier": 1, "size_est_min": 30}
    lc, _, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [grades({"o1": 0, "o2": 3, "o7": 3})],
                                         "plan_branches": [LLMError("down"), {"options": [opt]}]})  # fmt: skip
    lc.oral.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _id: SimpleNamespace(status="running", error=None)))
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    assert lc.oral_finish(901)["oral"]["remediation"]["status"] == "failed"
    assert lc.oral.retry_remediation(ref)["status"] == "building"
    with pytest.raises(LifecycleError):
        lc.oral.retry_remediation(ref)  # nothing left to retry


def test_remediation_with_an_invalid_option_is_a_reported_failure(oral_env, tmp_path):
    bad = {"title": "t", "why_now": "w", "kind": "challenge", "domain": "matching", "subskills": ["matching.l_network"], "tier": 1, "size_est_min": 30}
    lc, _, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [grades({"o1": 0, "o2": 3, "o7": 3})],
                                         "plan_branches": [{"options": [bad]}, {"options": [bad]}]})  # fmt: skip
    lc.oral.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _id: None))
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    rem = lc.oral_finish(901)["oral"]["remediation"]
    assert rem["status"] == "failed" and "kind must be one of ['remediation']" in rem["error"]


def test_unavailable_grading_is_provisional_then_applied_exactly_once(oral_env, tmp_path):
    lc, _, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [LLMError("subscription limit"), grades({"o1": 2, "o2": 2, "o7": 3})]})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    d = lc.oral_finish(901)
    assert d["provisional"] and d["oral"]["status"] == "pending" and d["grades"]["status"] == "graded"  # explanations graded, oral pending
    assert not [e for e in DataStore(oral_env).events() if e.type == "oral_item"]
    assert [p["task_type"] for p in lc.pending_grades()] == ["grade_oral"]
    assert lc.retry_grades(force=True) == 1
    d = lc.debrief_view(901)
    assert not d["provisional"] and d["oral"]["status"] == "graded" and d["oral"]["passed"] is True
    assert len([e for e in DataStore(oral_env).events() if e.type == "oral_item"]) == 3
    assert lc.retry_grades(force=True) == 0 and lc.pending_grades() == []


def test_a_quote_that_is_not_verbatim_is_repaired_once(oral_env, tmp_path):
    bad = grades({"o1": 2, "o2": 2, "o7": 2}, quote="something the learner never said")
    good = grades({"o1": 2, "o2": 2, "o7": 2}, quote="Quality Factor!")  # case/punctuation differences are tolerated
    lc, ad, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [bad, good]})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    d = lc.oral_finish(901)
    assert [i["quote"] for i in d["oral"]["items"]] == ["Quality Factor!"] * 3
    repair = [r for r in ad.requests if r.task_type == "grade_oral"][1]
    assert "not verbatim" in repair.messages[-1]["content"]


def test_unusable_grader_output_is_queued_not_applied(oral_env, tmp_path):
    lc, _, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [{"items": [{"id": "o1", "score": 9}], "misconceptions": []}]})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    d = lc.oral_finish(901)
    assert d["provisional"] and d["oral"]["status"] == "pending"
    assert not [e for e in DataStore(oral_env).events() if e.type == "oral_item"]


# ---- voice turns (fake recorder, transcriber, speaker) -------------------------------------------------------------------------------


class FakeRecorder:
    def __init__(self, seconds=3.0):
        self.seconds, self.recording, self.starts = seconds, False, 0

    def start(self):
        if self.recording:
            return
        self.recording = True
        self.starts += 1

    def stop(self):
        if not self.recording:
            raise VoiceUnavailable("not recording")
        self.recording = False
        return np.zeros(int(self.seconds * SAMPLE_RATE), dtype=np.float32)

    def cancel(self):
        self.recording = False


class FakeTranscriber:
    def __init__(self, texts):
        self.texts, self.prompts = list(texts), []

    def load(self):
        pass

    def transcribe(self, audio, prompt=""):
        self.prompts.append(prompt)
        return Transcript(self.texts.pop(0), len(audio) / SAMPLE_RATE, 0.25)


class FakeSpeaker:
    def __init__(self, fail=False):
        self.said, self.fail = [], fail

    def load(self):
        pass

    def synthesize(self, text):
        if self.fail:
            raise VoiceUnavailable("no voice")
        self.said.append(text)
        return b"RIFFfake" + text.encode(), 0.1


def voice_lc(oral_env, tmp_path, texts, script=None, speaker=None):
    lc, ad, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], **(script or {})})
    lc.oral.voice = Voice(recorder=FakeRecorder(), transcriber=FakeTranscriber(texts), speaker=speaker or FakeSpeaker())
    lc.domain.vocab = oral_env / "vocab.txt"
    (oral_env / "vocab.txt").write_text("Smith chart\nS11\n\nL-match\n")
    return lc, ad


def test_push_to_talk_turn_transcribes_asks_and_speaks_with_latency(oral_env, tmp_path):
    lc, ad = voice_lc(oral_env, tmp_path, ["the quality factor sets the bandwidth"])
    ref = to_oral(lc, CLEAN)
    v = lc.oral.start(ref, "voice", True)
    assert v["mode"] == "voice" and v["tts"] and v["turns"][0]["audio_url"] == f"/api/nodes/901/oral/audio/1"
    assert lc.oral.audio(ref, 1) == b"RIFFfakeQuestion 1?"
    assert lc.oral.record_start(ref) == {"recording": True} and lc.oral.view(ref)["recording"]
    lc.oral.record_start(ref)  # key auto-repeat: ignored
    assert lc.oral.voice.recorder.starts == 1
    out = lc.oral.record_stop(ref)
    assert out["empty"] is False and out["transcript"] == "the quality factor sets the bandwidth"
    assert out["learner"]["text"] == out["transcript"] and out["examiner"]["kind"] in ("question", "probe") and out["examiner"]["audio_url"]
    lat = lc.oral.view(ref)["latency"][-1]
    assert lat["record_s"] == 3.0 and lat["stt_s"] == 0.25 and {"llm_s", "tts_s", "total_s"} <= set(lat) and lat["tts_s"] == 0.1
    assert lat["total_s"] >= lat["llm_s"]
    # the STT prompt: the node's own vocab first, then the domain's vocab.txt, no duplicates
    p = lc.oral.voice.transcriber.prompts[0]
    assert p == "corner frequency, RC, Smith chart, S11, L-match"


def test_silence_is_not_an_answer(oral_env, tmp_path):
    lc, _ = voice_lc(oral_env, tmp_path, [""])
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "voice", False)
    lc.oral.record_start(ref)
    out = lc.oral.record_stop(ref)
    assert out["empty"] is True and len(out["view"]["turns"]) == 1  # no learner turn, no examiner call, still on question 1


def test_cancelled_recording_and_state_errors(oral_env, tmp_path):
    lc, _ = voice_lc(oral_env, tmp_path, [])
    ref = to_oral(lc, CLEAN)
    with pytest.raises(LifecycleError) as e:
        lc.oral.record_start(ref)  # exam not started
    assert e.value.status == 409
    lc.oral.start(ref, "voice", False)
    lc.oral.record_start(ref)
    assert lc.oral.record_cancel(ref) == {"recording": False}
    with pytest.raises(LifecycleError) as e:
        lc.oral.record_stop(ref)  # nothing recording any more
    assert e.value.status == 409


def test_tts_failure_does_not_stop_the_exam_and_voice_needs_models(oral_env, tmp_path):
    lc, _ = voice_lc(oral_env, tmp_path, ["an answer"], speaker=FakeSpeaker(fail=True))
    ref = to_oral(lc, CLEAN)
    v = lc.oral.start(ref, "voice", True)
    assert v["turns"][0]["text"] == "Question 1?" and "audio_url" not in v["turns"][0]
    out = lc.oral.answer(ref, "typed during a voice exam is fine")
    assert out["examiner"]["text"] and "audio_url" not in out["examiner"]
    lc2, _, _ = make(oral_env, tmp_path / "x")  # no voice object at all: text only
    ref2 = lc2.store.find(901)
    with pytest.raises(LifecycleError) as e:
        lc2.oral.start(ref2, "voice", False)
    assert e.value.status == 409 and "text mode" in e.value.message
    assert lc2.oral.start(ref2, "text", True)["tts"] is False  # spoken questions silently off without a voice


def test_build_prompt_orders_dedupes_and_caps():
    assert build_prompt(["S11", "gm"], ["gm", "Smith chart", "s11", " "]) == "S11, gm, Smith chart"
    long = build_prompt(["x" * 500], ["y" * 500, "z"], max_chars=900)
    assert long == "x" * 500 and len(long) <= 900


def test_quotes_that_stay_unverified_are_blanked_but_scores_kept(oral_env, tmp_path):
    bad = grades({"o1": 2, "o2": 2, "o7": 2}, quote="something the learner never said")
    lc, _, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [bad, bad]})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    o = lc.oral_finish(901)["oral"]
    assert [(i["score"], i["quote"]) for i in o["items"]] == [(2, "")] * 3 and o["passed"] is True
