"""M8 (part 2): Planner under difficulty control, SUMMARY.md, proof-failure remediation, misconception lifecycle, oral audio pre-generation, graph."""

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
from app.lifecycle.outcomes import read_outcomes
from app.lifecycle.summary import TOKEN_CAP, assemble, estimate_tokens, valid_entry
from app.main import create_app
from app.onboarding.framework import Framework
from app.planner.difficulty import DIAGNOSTIC, HOLD, LOWER, RAISE, check_options, difficulty_from, unfamiliar_subskills
from app.planner.service import Planner, PlannerError, validate_options
from app.voice.service import Voice
from app.voice.stt import VoiceUnavailable
from tests.helpers import TRACK as T, make_data
from tests.test_builder_m5 import _diag, _opt, _router, data  # noqa: F401 (data = fixture)
from tests.test_lifecycle import ANSWERS, GRADES, SyncJobs, env, make, to_lab  # noqa: F401 (env = fixture)
from tests.test_m8 import O, mk, seq
from tests.test_onboarding import Scripted, template_framework
from tests.test_oral import CLEAN, FULL, KEY, NODE, THIN, FakeRecorder, FakeSpeaker, FakeTranscriber, grades, oral_env, run_exam, to_oral, voice_lc  # noqa: F401

SCORES = {"matching": {"tier": 3}, "noise": {"tier": 3}, "devices": {"tier": 1}}
FW = Framework.model_validate(template_framework())


# ---- Planner: input ----------------------------------------------------------------------------------------------------------------------


def test_planner_input_has_the_full_set_incl_outcomes_difficulty_and_sizing(data, tmp_path):
    for i in (1, 2, 3):
        mk(data, i)
    mk(data, 4, oral=False, subs=("noise.figure_cascade",))
    mk(data, 5, status="lab", proof=(), oral=None)
    add_misconception(DataLayout(data).misconceptions_path, track=T, node_id=4, subskill="noise.figure_cascade", text="NF adds linearly")
    (DataLayout(data, T).summary_path).parent.mkdir(parents=True, exist_ok=True)
    DataLayout(data, T).summary_path.write_text("## 0001 Node 1 (challenge, matching, T3): success\n")
    rebuild_db(data)
    inp = Planner(data, T, None, "rfic").planner_input()
    assert set(inp) == {"goals", "summary", "framework", "scores", "open_misconceptions", "recent_nodes", "difficulty", "sizing", "tier_exams_unlocked"}
    assert "60 GHz" in inp["goals"] and inp["summary"].startswith("## 0001") and inp["scores"]["matching"]["tier"] == 3
    assert inp["open_misconceptions"] == [{"text": "NF adds linearly", "subskill": "noise.figure_cascade"}]
    assert [(n["id"], n["outcome"]) for n in inp["recent_nodes"]] == [(5, "in_progress"), (4, "oral_failed"), (3, "success"), (2, "success"), (1, "success")]
    d = inp["difficulty"]
    assert (d["mode"], d["rated_nodes"], d["successes"], d["success_rate"], d["challenge_nodes"]) == (HOLD, 4, 3, 0.75, 5)
    assert "matching.l_network" in inp["sizing"]["unfamiliar_subskills"]  # no events yet: nothing is familiar


def test_recent_nodes_are_the_last_ten(data):
    for i in range(1, 14):
        mk(data, i)
    rebuild_db(data)
    assert [n["id"] for n in Planner(data, T, None, "rfic").planner_input()["recent_nodes"]] == list(range(13, 3, -1))


def test_familiarity_needs_evidence_from_two_nodes(data):
    _diag(data, "matching", "matching.l_network", 3, 2, node_id=1, type_="oral_item")
    rebuild_db(data)
    assert "matching.l_network" in unfamiliar_subskills(data, T)  # the diagnostic (node 0) and one node are not enough
    _diag(data, "matching", "matching.l_network", 3, 2, node_id=2, type_="proof_pass")
    rebuild_db(data)
    assert "matching.l_network" not in unfamiliar_subskills(data, T)


# ---- Planner: difficulty and sizing rules ----------------------------------------------------------------------------------------------------


def rule_errors(options, mode, unfamiliar=()):
    d = difficulty_from(seq(*{DIAGNOSTIC: [], RAISE: ["success"] * 4, HOLD: ["success"] * 3 + ["failure"], LOWER: ["failure"] * 4}[mode]))
    assert d.mode == mode
    return check_options([{**_opt(), **o} for o in options], d, SCORES, set(unfamiliar))


def test_diagnostic_and_raise_work_at_the_current_tier():
    assert rule_errors([{"tier": 3}, {"domain": "noise", "subskills": ["noise.figure_cascade"], "tier": 3}], DIAGNOSTIC) == []
    assert "tier must be 3" in rule_errors([{"tier": 2}], DIAGNOSTIC)[0]
    errs = rule_errors([{"tier": 3}, {"tier": 2}], RAISE)
    assert any("option 2: tier must be 3" in e for e in errs) and any("integrate 2 or more subskills" in e for e in errs)
    assert rule_errors([{"tier": 3, "subskills": ["matching.l_network", "smith_chart.navigation"]}], RAISE) == []


def test_lower_goes_a_tier_down_and_short():
    assert rule_errors([{"tier": 2, "size_est_min": 30}], LOWER) == []
    errs = rule_errors([{"tier": 3, "size_est_min": 30}, {"tier": 2, "size_est_min": 90}], LOWER)
    assert any("option 1: tier must be at most 2" in e for e in errs) and any("option 2: size_est_min must be at most 45" in e for e in errs)
    assert rule_errors([{"domain": "devices", "subskills": ["devices.biasing"], "tier": 1, "size_est_min": 30}], LOWER) == []  # nothing below T1


def test_hold_has_no_extra_tier_rule_and_unfamiliar_subskills_cap_the_size():
    assert rule_errors([{"tier": 1, "size_est_min": 120}], HOLD) == []  # familiar subskills may grow into an integrative build
    errs = rule_errors([{"tier": 3, "size_est_min": 120}], HOLD, unfamiliar={"matching.l_network"})
    assert errs == ["option 1: size_est_min must be at most 45 (it includes unfamiliar subskills ['matching.l_network'])"]
    assert rule_errors([{"tier": 3, "size_est_min": 45}], HOLD, unfamiliar={"matching.l_network"}) == []


def test_schema_errors_come_before_difficulty_errors():
    d = difficulty_from(seq("failure", "failure", "failure"))
    clean, errors = validate_options({"options": [_opt(), _opt(subskills=["matching.nope"])]}, FW, SCORES, d, set())
    assert clean == [] and any("unknown subskills" in e for e in errors) and not any("tier must be" in e for e in errors)


def test_generate_repairs_a_difficulty_violation_once(data, tmp_path):
    for i in (1, 2, 3, 4):
        mk(data, i)  # 4/4 successes -> raise
    rebuild_db(data)
    two = _opt(subskills=["matching.l_network", "smith_chart.navigation"])
    ad = Scripted({"plan_branches": [{"options": [_opt(tier=2), _opt(title="b", tier=2)]}, {"options": [two, _opt(title="b")]}]})
    out = Planner(data, T, _router(tmp_path, ad), "rfic").generate()
    assert [o["tier"] for o in out["options"]] == [3, 3] and out["difficulty"]["mode"] == RAISE and out["difficulty"]["rated_nodes"] == 4
    fix = ad.requests[1].messages[-1]["content"]
    assert "tier must be 3" in fix and "difficulty mode is raise" in fix
    with pytest.raises(PlannerError) as e:
        Planner(data, T, _router(tmp_path, Scripted({"plan_branches": [{"options": [_opt(tier=2)] * 2}] * 2})), "rfic").generate()
    assert e.value.status == 502


def test_difficulty_adapts_after_a_failure_streak(data, tmp_path):
    for i, ok in enumerate([True, True, False, False, False], 1):
        mk(data, i, oral=ok)
    rebuild_db(data)
    inp = Planner(data, T, None, "rfic").planner_input()
    assert inp["difficulty"]["mode"] == LOWER and "below 0.70" in inp["difficulty"]["rule"]
    ad = Scripted({"plan_branches": [{"options": [_opt(tier=2, size_est_min=30), _opt(title="b", tier=2, size_est_min=25)]}]})
    assert len(Planner(data, T, _router(tmp_path, ad), "rfic").generate()["options"]) == 2


# ---- SUMMARY.md (§3.2) ------------------------------------------------------------------------------------------------------------------------


def finish_node(oral_env, tmp_path, script=None, jobs=None):
    """Node 901 through the whole lifecycle, text mode, to `done`. -> (lifecycle, adapter)."""
    lc, ad, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [grades({"o1": 2, "o2": 3, "o7": 2})], **(script or {})})
    lc.jobs = jobs
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    lc.oral_finish(901)
    lc.debrief_complete(901)
    return lc, ad


class StubRouter:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.requests = text, error, []

    def complete(self, task, messages, **kw):
        self.requests.append((task, messages))
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text, json=None, model="stub")


def test_summary_is_written_after_the_debrief_from_code_when_the_llm_is_down(oral_env, tmp_path):
    lc, _ = finish_node(oral_env, tmp_path)  # no `summarize` script: the call fails
    lay = DataLayout(oral_env, T)
    text = lay.summary_path.read_text(encoding="utf-8")
    assert text.startswith("# Archive summary") and "## 0901 Set an RC low-pass corner (challenge, filters, T1): success" in text
    assert "- Oral: passed;" in text and "numeric predictions" in text and estimate_tokens(text) <= TOKEN_CAP
    assert (lay.sessions_dir / NODE / "summary.md").is_file()
    assert Planner(oral_env, T, None, "rfic").planner_input()["summary"] == text  # the Planner reads exactly this file


def test_summary_uses_the_models_entry_when_valid_and_sends_no_answer_key(oral_env, tmp_path):
    lc, _ = finish_node(oral_env, tmp_path)
    stub = StubRouter("## 0901 Set an RC low-pass corner (challenge, filters, T1): success\n- Built: RC corner.\n- Prelab: p2 8% high.")
    lc.summarizer.router = stub
    ref = lc.store.find(901)
    lc.summarizer.after_debrief(ref)
    assert "- Built: RC corner." in DataLayout(oral_env, T).summary_path.read_text(encoding="utf-8")
    task, msgs = stub.requests[0]
    sent = msgs[1]["content"]
    assert task == "summarize" and "actual" not in sent and "153918" not in sent and "_hidden" not in sent  # prediction error only, never the key value
    assert json.loads(sent)["prelab"][0]["error_pct"] is not None


@pytest.mark.parametrize("bad", ["no heading at all", "## ok\n" + "\n".join(f"- l{i}" for i in range(8)), "## h\n```\ncode\n```", "## h\n" + "x" * 1200, None, ""])
def test_an_unusable_model_entry_is_replaced_by_the_code_entry(oral_env, tmp_path, bad):
    assert valid_entry(bad) is None
    lc, _ = finish_node(oral_env, tmp_path)
    lc.summarizer.router = StubRouter(bad)
    lc.summarizer.after_debrief(lc.store.find(901))
    assert "numeric predictions" in DataLayout(oral_env, T).summary_path.read_text(encoding="utf-8")


def test_summary_runs_as_a_job_when_there_is_a_job_manager_and_never_blocks_the_debrief(oral_env, tmp_path):
    seen = []

    class Jobs(SyncJobs):
        def submit(self, type, fn, node_id=None):
            seen.append((type, node_id))
            return super().submit(type, fn, node_id)

    finish_node(oral_env, tmp_path, jobs=Jobs())
    assert seen == [("summarize", f"{T}/{NODE}")] and DataLayout(oral_env, T).summary_path.is_file()


def test_a_broken_job_manager_does_not_fail_the_debrief(oral_env, tmp_path):
    class Broken:
        def submit(self, *a, **k):
            raise RuntimeError("jobs down")

    lc, _ = finish_node(oral_env, tmp_path, jobs=Broken())  # debrief_complete returned normally
    assert lc.store.status(lc.store.find(901)) == "done" and not DataLayout(oral_env, T).summary_path.exists()


def _entries(n):
    body = "\n".join(f"- {k}: " + "detail " * 30 for k in ("Built", "Prelab", "Oral", "Misconceptions", "Mastery"))
    return [f"## {i:04d} Node {i} (challenge, matching, T3): success\n" + body for i in range(1, n + 1)]


def test_assemble_shrinks_the_oldest_entries_first_and_stays_under_1500_tokens():
    entries = _entries(40)
    text = assemble(entries)
    assert estimate_tokens(text) <= TOKEN_CAP
    assert "- Built:" in text.split("## 0040")[1] and "## 0001 Node 1 (challenge, matching, T3): success\n\n## 0002" in text  # newest kept in full, oldest down to a heading
    assert text.index("## 0039") < text.index("## 0040")  # still oldest first
    assert assemble(entries[:2]).count("- Built:") == 2  # small histories are kept in full
    assert estimate_tokens(assemble([])) <= TOKEN_CAP


def test_assemble_drops_the_oldest_headings_behind_a_count_when_even_headings_do_not_fit():
    text = assemble(_entries(400))
    assert estimate_tokens(text) <= TOKEN_CAP and "## 0400 Node 400" in text and "## 0001 " not in text and "earlier node(s) omitted" in text


def test_regenerate_backfills_finished_nodes_without_an_llm_call(data, tmp_path):
    from tests.test_lifecycle import Checker  # noqa: F401

    lc, ad, _ = make(data, tmp_path)
    mk(data, 1)
    mk(data, 2, status="lab", proof=(), oral=None)
    rebuild_db(data)
    text = lc.regenerate_summary()
    assert "## 0001 Node 1" in text and "Node 2" not in text  # only finished nodes
    assert not [r for r in ad.requests if r.task_type == "summarize"]


# ---- remediation after 3 failed proof submissions (§7.5) ------------------------------------------------------------------------------------------


REM = {"title": "Probe the RC corner", "why_now": "the proof failed three times", "kind": "remediation", "domain": "matching",
       "subskills": ["matching.l_network"], "tier": 1, "size_est_min": 30}  # fmt: skip


def fake_build(monkeypatch, state):
    class FakeBuild:
        def __init__(self, root, track, router, domain):
            self.root, self.track = root, track

        def start(self, jobs, option, on_success=None):
            d = DataLayout(self.root, self.track).nodes_dir / "0902-remediate"
            d.mkdir(parents=True, exist_ok=True)
            fm = {"id": 902, "slug": "remediate", "kind": option["kind"], "domain": option["domain"], "subskills": option["subskills"], "tier": 1, "status": "prelab"}
            (d / "node.md").write_text("---\n" + yaml.safe_dump(fm) + "---\nb\n")
            state.update(option=option, release=lambda: on_success({"node_dir": d.name}))
            return SimpleNamespace(id="job-r"), SimpleNamespace(name=d.name, node_id=902)

    monkeypatch.setattr("app.lifecycle.remediation.BuildService", FakeBuild)


def three_failed_proofs(lc, chk):
    chk.verdicts = ["fail"]
    to_lab(lc)
    for _ in range(3):
        lc.proof_submit(SyncJobs(), 901)


def test_accepting_the_offer_after_three_failed_proofs_builds_a_remediation_child(oral_env, tmp_path, monkeypatch):
    state = {}
    fake_build(monkeypatch, state)
    lc, ad, chk = make(oral_env, tmp_path, {"plan_branches": [{"options": [REM]}]})
    lc.oral.remediator.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _id: SimpleNamespace(status="running", error=None)))
    three_failed_proofs(lc, chk)
    pv = lc.proof_view(901)
    assert pv["remediation_offer"] and pv["remediation"] is None and pv["stage"] == "proof"
    pv = lc.remediation(901, True)
    assert pv["remediation_decision"] == "accepted" and pv["remediation"]["status"] == "building" and pv["remediation"]["child_id"] == 902
    assert state["option"]["kind"] == "remediation"
    rem_in = json.loads([r for r in ad.requests if r.task_type == "plan_branches"][0].messages[1]["content"])["remediation"]
    assert rem_in["parent"]["id"] == 901 and "failed 3 times" in rem_in["failed_items"][0]["why"] and "f3db" in rem_in["failed_items"][0]["why"]
    rows = [json.loads(x) for x in DataLayout(oral_env, T).remediation_path.read_text().splitlines()]
    assert any(r.get("child") == "0902-remediate" and r["decision"] == "proof_fail" for r in rows)
    assert lc.remediation(901, True)["remediation"]["status"] == "building"  # idempotent: no second Planner call or build
    assert len([r for r in ad.requests if r.task_type == "plan_branches"]) == 1
    with pytest.raises(Exception) as e:
        lc.remediation(901, False)  # too late to decline: the child is being built
    assert getattr(e.value, "status", None) == 409
    state["release"]()  # the child is released: linked to its parent, the parent's record flips to ready
    assert lc.store.frontmatter(lc.store.find(902))["parents"] == [901]
    assert lc.proof_view(901)["remediation"]["status"] == "ready"
    assert read_outcomes(oral_env, T)[901].label == "proof_failed"  # counts against the success rate (the parent stays in Proof)


def test_declining_the_offer_spawns_nothing_and_a_failed_build_can_be_retried(oral_env, tmp_path, monkeypatch):
    fake_build(monkeypatch, {})
    lc, ad, chk = make(oral_env, tmp_path, {"plan_branches": [LLMErr(), {"options": [REM]}]})
    lc.oral.remediator.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _id: SimpleNamespace(status="running", error=None)))
    three_failed_proofs(lc, chk)
    pv = lc.remediation(901, False)
    assert pv["remediation_decision"] == "declined" and pv["remediation"] is None and not [r for r in ad.requests if r.task_type == "plan_branches"]
    pv = lc.remediation(901, True)  # the Planner is down: recorded as failed, not raised
    assert pv["remediation"]["status"] == "failed"
    assert lc.remediation(901, True)["remediation"]["status"] == "building"  # accepting again retries
    assert read_outcomes(oral_env, T)[901].label == "proof_failed"


def LLMErr():
    from app.llm.types import LLMError

    return LLMError("down")


def test_no_offer_before_three_failures(oral_env, tmp_path):
    lc, _, chk = make(oral_env, tmp_path)
    chk.verdicts = ["fail"]
    to_lab(lc)
    lc.proof_submit(SyncJobs(), 901)
    with pytest.raises(Exception) as e:
        lc.remediation(901, True)
    assert getattr(e.value, "status", None) == 409


# ---- misconception lifecycle (§7.7) --------------------------------------------------------------------------------------------------------------


MIS = "Q only rises with L"
INTER = {"id": "o9", "subskill": "passives.inductor_q", "tier": 1, "question": "Question 9?", "anchors": {"0": "a", "1": "b", "2": "c", "3": "d"}, "probes": [],
         "interleaved": True}  # fmt: skip


def with_second_interleaved(oral_env):
    (DataLayout(oral_env, T).nodes_dir / NODE / "_hidden" / "oral_key.yaml").write_text(yaml.safe_dump({"items": KEY + [INTER]}, sort_keys=False))


def open_mis(oral_env, node_id=5):
    add_misconception(DataLayout(oral_env).misconceptions_path, track=T, node_id=node_id, subskill="passives.inductor_q", text=MIS)


def test_the_oral_revisits_an_open_misconception_with_its_interleaved_item(oral_env, tmp_path):
    with_second_interleaved(oral_env)
    lc, _, _ = make(oral_env, tmp_path)
    ref = to_oral(lc, CLEAN)
    assert lc.oral.plan(ref)["items"][-1] == "o7"  # no open misconception: the first interleaved item in key order
    open_mis(oral_env)
    plan = lc.oral.plan(ref)
    assert plan["items"][-1] == "o9" and "revisits the open misconception on passives.inductor_q" in plan["reason"]


def test_a_node_does_not_revisit_its_own_misconception(oral_env, tmp_path):
    with_second_interleaved(oral_env)
    open_mis(oral_env, node_id=901)
    lc, _, _ = make(oral_env, tmp_path)
    assert lc.oral.plan(to_oral(lc, CLEAN))["items"][-1] == "o7"


@pytest.mark.parametrize("score, resolved", [(2, True), (3, True), (1, False), (0, False)])
def test_misconception_resolves_only_when_the_interleaved_item_scores_2_or_more(oral_env, tmp_path, score, resolved):
    with_second_interleaved(oral_env)
    open_mis(oral_env)
    g = grades({"o8": 3, "o1": 3, "o2": 3, "o3": 3, "o4": 3, "o9": score})  # o4 is a plain core item on the SAME subskill: it must not resolve it
    lc, _, _ = make(oral_env, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [g]})
    ref = to_oral(lc)  # missed prelab -> 5 core + the interleaved item
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    d = lc.oral_finish(901)
    m = next(x for x in fold(read_records(DataLayout(oral_env).misconceptions_path, T)) if x["text"] == MIS)
    assert m["resolved"] is resolved and m["resolved_by_node"] == (901 if resolved else None)
    assert [x["text"] for x in d["misconceptions"]["resolved"]] == ([MIS] if resolved else [])


def test_an_open_misconception_becomes_the_first_interleaving_target_and_gate_4_demands_an_item_on_it(data):
    add_misconception(DataLayout(data).misconceptions_path, track=T, node_id=1, subskill="passives.inductor_q", text=MIS)
    rebuild_db(data)
    ctx = interleaving_context(data, T, {"subskills": ["matching.l_network"]})
    assert ctx.open_misconceptions == [{"text": MIS, "subskill": "passives.inductor_q"}] and "passives.inductor_q" in ctx.targets()

    def gate(oral, prelab=()):
        errs: list[str] = []
        _interleaving({"oral": oral, "prelab": list(prelab)}, GateContext(**{"open_misconceptions": ctx.open_misconceptions, "stale_subskills": []}), errs)
        return errs

    missing = gate([{"id": "o1", "subskill": "matching.l_network", "interleaved": False}])
    assert any("no prelab or oral item targets" in e for e in missing) and any("at least one oral_key item must have interleaved: true" in e for e in missing)
    assert gate([{"id": "o1", "subskill": "passives.inductor_q", "interleaved": True}]) == []  # an oral item on it, flagged, passes
    assert gate([{"id": "o1", "subskill": "matching.l_network", "interleaved": False}], prelab=[{"id": "p1", "subskill": "passives.inductor_q"}])  # prelab alone is not enough: the flag is required
    # once the misconception resolves it stops being a target
    from app.datastore.misconceptions import resolve_misconception

    resolve_misconception(DataLayout(data).misconceptions_path, 1, track=T, node_id=2, event_id="e")
    rebuild_db(data)
    assert interleaving_context(data, T, {"subskills": []}).open_misconceptions == []


# ---- oral: next question's audio is synthesized while the learner answers ---------------------------------------------------------------------------


def test_the_next_question_is_pregenerated_while_the_learner_answers(oral_env, tmp_path):
    sp = FakeSpeaker()
    lc, ad = voice_lc(oral_env, tmp_path, [], speaker=sp)
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", True)
    voice = lc.oral.voice
    assert sp.said[0] == "Question 1?"
    voice._clips["Question 2?"].result(timeout=5)  # synthesized in the background before any answer
    out = lc.oral.answer(ref, FULL)
    lat = lc.oral._state(ref)["turns"][-1]["latency"]
    assert out["examiner"]["text"] == "Question 2?" and lat["pregenerated"] is True and lat["llm_s"] == 0 and out["examiner"]["audio_url"]
    assert sp.said.count("Question 2?") == 1  # used, not synthesized a second time
    assert lc.oral.audio(ref, 3) == b"RIFFfakeQuestion 2?"
    voice._clips["Question 7?"].result(timeout=5)
    lc.oral.answer(ref, FULL)
    voice._clips[CLOSING_TEXT].result(timeout=5)  # the last question pre-generates the closing line
    done = lc.oral.answer(ref, FULL)
    assert done["done"] and lc.oral._state(ref)["turns"][-1]["latency"]["pregenerated"] is True


CLOSING_TEXT = "Thanks, that's all my questions."


def test_a_live_probe_is_synthesized_in_the_foreground_but_the_next_question_stays_ready(oral_env, tmp_path):
    sp = FakeSpeaker()
    lc, ad = voice_lc(oral_env, tmp_path, [], speaker=sp, script={"oral_dialogue": [{"say": "Why does that hold?", "item_id": "o1", "kind": "probe"}]})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", True)
    lc.oral.answer(ref, THIN)
    lat = lc.oral._state(ref)["turns"][-1]["latency"]
    assert lat["pregenerated"] is False and lat["llm_s"] > 0 and "Why does that hold?" in sp.said
    lc.oral.voice._clips["Question 2?"].result(timeout=5)
    nxt = lc.oral.answer(ref, FULL)["examiner"]
    assert nxt["text"] == "Question 2?" and lc.oral._state(ref)["turns"][-1]["latency"]["pregenerated"] is True


class Flaky:
    def __init__(self):
        self.calls = 0

    def load(self):
        pass

    def synthesize(self, text):
        self.calls += 1
        if self.calls == 1:
            raise VoiceUnavailable("boom")
        return b"wav", 0.2


def test_a_failed_pregeneration_falls_back_to_a_foreground_synthesis():
    v = Voice(recorder=FakeRecorder(), transcriber=FakeTranscriber([]), speaker=Flaky())
    v.pregenerate("hello")
    assert v._clips["hello"].exception(timeout=5) is not None
    wav, secs, pre = v.clip("hello")
    assert (wav, pre) == (b"wav", False) and secs == 0.2
    assert v.clip("other")[2] is False  # never pre-generated


def test_pregeneration_is_idempotent_and_bounded():
    sp = FakeSpeaker()
    v = Voice(recorder=FakeRecorder(), transcriber=FakeTranscriber([]), speaker=sp)
    for _ in range(3):
        v.pregenerate("same")
    v._clips["same"].result(timeout=5)
    assert sp.said == ["same"]
    for i in range(40):
        v.pregenerate(f"t{i}")
    assert len(v._clips) <= 16


# ---- graph API --------------------------------------------------------------------------------------------------------------------------------------------


def test_graph_nodes_edges_status_domain_tier_and_track_scope(tmp_path):
    root = make_data(tmp_path / "d", T, "other")
    mk(root, 1)
    mk(root, 2, oral=False, subs=("noise.figure_cascade",))
    mk(root, 3, kind="remediation", status="prelab", proof=(), oral=None, parents=[2, 99, 3], subs=("noise.figure_cascade",))  # 99 does not exist, 3 is itself
    mk(root, 1, track="other", subs=("devices.biasing",))
    rebuild_db(root)
    g = graph_view(root, T)
    assert g["track"] == T and [n["id"] for n in g["nodes"]] == [1, 2, 3]
    assert g["edges"] == [{"source": 2, "target": 3}]
    n2, n3 = g["nodes"][1], g["nodes"][2]
    assert (n2["domain"], n2["tier"], n2["status"], n2["outcome"], n2["kind"]) == ("noise", 3, "done", "oral_failed", "challenge")
    assert (n3["status"], n3["outcome"], n3["parents"], n3["kind"]) == ("prelab", "in_progress", [2], "remediation")
    other = graph_view(root, "other")
    assert [n["id"] for n in other["nodes"]] == [1] and other["nodes"][0]["domain"] == "devices" and other["edges"] == []


def test_graph_summary_and_difficulty_routes(data, monkeypatch):
    for i in (1, 2, 3):
        mk(data, i, parents=[i - 1] if i > 1 else [])
    rebuild_db(data)
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(data))
    with TestClient(create_app()) as c:
        g = c.get("/api/graph").json()
        assert [n["id"] for n in g["nodes"]] == [1, 2, 3]
        assert {"source": 1, "target": 2} in g["edges"] and {"source": 2, "target": 3} in g["edges"]
        assert "/api/graph" in c.get("/openapi.json").json()["paths"]
        d = c.get("/api/planner/difficulty").json()
        assert d["difficulty"]["mode"] == RAISE and d["difficulty"]["rated_nodes"] == 3 and "unfamiliar_subskills" in d["sizing"]
        s = c.get("/api/summary").json()
        assert s["text"] == "" and s["cap"] == TOKEN_CAP
        r = c.post("/api/summary/regenerate").json()
        assert "## 0001 Node 1" in r["text"] and 0 < r["tokens"] <= TOKEN_CAP
        assert c.get("/api/summary").json()["text"] == r["text"]
