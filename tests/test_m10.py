"""M10: tier exams (SPEC §8.4): unlock, exam node creation, 6-question oral, pass/fail, tier-up, Planner offer, Competency API. Mock LLM only."""

import json

import pytest
from fastapi.testclient import TestClient

from app.competency.engine import score_domain
from app.datastore.cache import rebuild_db
from app.datastore.events import append_event, new_event, read_events
from app.datastore.layout import DataLayout
from app.datastore.misconceptions import fold, read_records
from app.domains import Domain
from app.lifecycle.exam import TierExams, validate_exam
from app.lifecycle.nodes import LifecycleError, NodeStore
from app.lifecycle.outcomes import read_outcomes
from app.lifecycle.service import Lifecycle
from app.llm.config import load_routing
from app.llm.router import Router
from app.llm.store import Store
from app.llm.types import LLMError
from app.main import create_app
from app.onboarding.framework import Framework, FrameworkStore
from app.planner.service import Planner
from tests.helpers import TRACK as T, make_data
from tests.test_builder_m5 import _opt, _router, data  # noqa: F401 (data = fixture)
from tests.test_lifecycle import Checker
from tests.test_oral import FULL
from tests.test_onboarding import ALL, Scripted, template_framework

SUBS = ["matching.l_network", "smith_chart.navigation", "matching.bandwidth"]  # the third is emergent (§8.1)


def exam_json(subs=None):
    subs = subs or SUBS
    items = [{"id": f"e{i}", "subskill": subs[(i - 1) % len(subs)], "question": f"Exam question {i}?",
              "anchors": {str(k): f"anchor {k} of e{i}" for k in range(4)}, "probes": [f"probe {i}a?", f"probe {i}b?"],
              "key_terms": ["quality factor", "reflection", "match"]} for i in range(1, 7)]  # fmt: skip
    return {"title": "Tier 3 exam: Impedance matching", "brief": "Six oral questions on matching.", "items": items}


def seed(root, n=13, v=0.8, subs=SUBS, domain="matching", tier=3, track=T):
    """n tier events (value v) round-robin over `subs`: 13 x 0.8 -> mastery exactly 65 (§8.3 reference)."""
    for i in range(n):
        append_event(DataLayout(root).events_path, new_event(track=track, node_id=1, domain=domain, subskill=subs[i % len(subs)], tier=tier, type="oral_item",
                                                             value=v, weight=1.0, grader_model="m", framework_version=1))  # fmt: skip
    rebuild_db(root)


def grades(scores, mis=None):
    return {"items": [{"id": f"e{i}", "score": s, "quote": "quality factor", "rationale": f"r{i}"} for i, s in enumerate(scores, 1)], "misconceptions": mis or []}


def lc_for(root, tmp_path, script, track=T):
    ad = Scripted(script)
    router = Router(load_routing(), Store(tmp_path / "llm.db"), {k: ad for k in ALL})
    return Lifecycle(root, track, router, Domain(name="rfic", image="x", image_size_gb=0, run_checker=Checker())), ad


def exams_for(lc) -> TierExams:
    return TierExams(lc.store, lc.router, "rfic")


def matching(lc):
    return {x["domain"]: x for x in exams_for(lc).competency()["domains"]}["matching"]


def take(lc, nid):
    """Answer the whole exam in text mode and finish it -> the debrief."""
    ref = lc.store.find(nid)
    lc.oral.start(ref, "text", False)
    for _ in range(40):
        if lc.oral.view(ref)["status"] == "complete":
            break
        lc.oral.answer(ref, FULL)
    return lc.oral_finish(nid)


# ---- validation ------------------------------------------------------------------------------------------------


def test_validate_exam_accepts_a_good_exam_and_rejects_each_defect():
    ok, errs = validate_exam(exam_json(), set(SUBS))
    assert errs == [] and len(ok["items"]) == 6 and ok["items"][0]["interleaved"] is False

    def bad(mut):
        j = exam_json()
        mut(j)
        return validate_exam(j, set(SUBS))[1]

    assert bad(lambda j: j["items"].pop())
    assert bad(lambda j: j["items"][0].update(subskill="noise.sources"))
    assert bad(lambda j: j["items"][0].update(id="x"))
    assert bad(lambda j: j["items"][0]["anchors"].pop("2"))
    assert bad(lambda j: j["items"][0].update(key_terms=["a", "b"]))
    assert bad(lambda j: j["items"][0].update(key_terms=["a", "a", "b"]))
    assert bad(lambda j: j["items"][0].update(probes=[]))
    assert bad(lambda j: j.update(title=" "))
    assert validate_exam(exam_json(SUBS[:2]), set(SUBS))[1]  # only 2 distinct subskills: the exam must span >= 3


# ---- unlock + competency view ----------------------------------------------------------------------------------


def test_competency_shows_locked_then_available(data, tmp_path):
    lc, _ = lc_for(data, tmp_path, {})
    d = matching(lc)
    assert d["tier"] == 3 and d["exam"]["state"] == "locked" and d["exam"]["missing"]
    assert [t["state"] for t in d["tiers"]] == ["passed", "passed", "current", "locked", "locked"] and d["tiers"][2]["descriptor"]
    seed(data, n=12)  # mastery 100*9.6/15 = 64: just under
    d = matching(lc)
    assert d["exam"]["state"] == "locked" and any("mastery" in m for m in d["exam"]["missing"])
    seed(data, n=1)  # 13 x 0.8
    d = matching(lc)
    assert d["mastery"] == 65.0 and d["unlock"]["subskills_ok"] and d["exam"]["state"] == "available" and d["exam"]["missing"] == []
    assert {s["name"]: s["evidenced"] for s in d["subskills"]}["matching.bandwidth"]  # emergent subskills appear with evidence


def test_two_subskills_are_not_enough_even_at_high_mastery(data, tmp_path):
    seed(data, n=20, v=1.0, subs=SUBS[:2])
    lc, _ = lc_for(data, tmp_path, {})
    d = matching(lc)
    assert d["exam"]["state"] == "locked" and any("subskills" in m for m in d["exam"]["missing"])
    with pytest.raises(LifecycleError) as e:
        exams_for(lc).start("matching")
    assert e.value.status == 409


def test_start_unknown_domain_404(data, tmp_path):
    lc, _ = lc_for(data, tmp_path, {})
    with pytest.raises(LifecycleError) as e:
        exams_for(lc).start("nope")
    assert e.value.status == 404


# ---- creating the exam node ----------------------------------------------------------------------------------


def test_start_creates_an_oral_stage_exam_node_with_a_hidden_key(data, tmp_path):
    seed(data)
    lc, ad = lc_for(data, tmp_path, {"build_exam": [exam_json()]})
    out = exams_for(lc).start("matching")
    assert out["reused"] is False
    ref = lc.store.find(out["node_id"])
    fm = lc.store.frontmatter(ref)
    assert (fm["kind"], fm["domain"], fm["tier"], fm["status"], fm["track"]) == ("tier_exam", "matching", 3, "oral", T)
    assert len(lc.store.oral_key(ref)) == 6 and (ref.path / "_hidden" / "oral_key.yaml").is_file()
    assert "anchor" not in lc.view(ref.id)["brief"] and "Exam question" not in lc.view(ref.id)["brief"]
    req = json.loads(ad.requests[0].messages[1]["content"])
    assert req["tier"] == 3 and req["subskills"] and "matching.bandwidth" in req["evidenced_subskills"]
    again = exams_for(lc).start("matching")  # one exam in progress per domain: the same node comes back, no second LLM call
    assert again == {"node_id": ref.id, "dir": ref.name, "reused": True} and len(ad.requests) == 1
    d = matching(lc)
    assert d["exam"]["state"] == "in_progress" and d["exam"]["node_id"] == ref.id
    assert exams_for(lc).offers() == []  # not offered again while it is in progress


def test_invalid_exam_gets_one_repair_then_502_and_creates_nothing(data, tmp_path):
    seed(data)
    broken = exam_json()
    broken["items"] = broken["items"][:5]
    lc, ad = lc_for(data, tmp_path, {"build_exam": [broken, exam_json()]})
    out = exams_for(lc).start("matching")  # repaired on the second answer
    assert out["node_id"] and len(ad.requests) == 2 and "exactly 6" in ad.requests[1].messages[-1]["content"]
    lc2, _ = lc_for(data, tmp_path, {"build_exam": [broken, broken]})
    lc2.store.set_scalar(lc2.store.find(out["node_id"]), "status", "done")
    rebuild_db(data)
    before = {p.name for p in lc2.store.layout.nodes_dir.iterdir()}
    with pytest.raises(LifecycleError) as e:
        exams_for(lc2).start("matching")
    assert e.value.status == 502 and {p.name for p in lc2.store.layout.nodes_dir.iterdir()} == before


# ---- taking the exam ---------------------------------------------------------------------------------------


def test_exam_pass_raises_the_tier_and_starts_mastery_fresh(data, tmp_path):
    seed(data)
    lc, ad = lc_for(data, tmp_path, {"build_exam": [exam_json()], "grade_tier_exam": [grades([3, 2, 3, 2, 3, 2])]})
    nid = exams_for(lc).start("matching")["node_id"]
    d = take(lc, nid)
    assert d["kind"] == "tier_exam" and d["oral"]["passed"] is True and abs(d["oral"]["mean"] - 2.5) < 1e-9
    assert d["oral"]["exam"]["tier_up"] and (d["oral"]["exam"]["tier_before"], d["oral"]["exam"]["tier_after"]) == (3, 4)
    assert len(d["oral"]["items"]) == 6 and d["oral"]["remediation"] is None
    events = read_events(lc.store.layout.events_path, T)[0]
    exam_events = [e for e in events if e.type == "tier_exam_item"]
    assert len(exam_events) == 6 and {e.node_id for e in exam_events} == {nid}
    s = score_domain(events, "matching")
    assert s.tier == 4 and s.mastery == 0.0 and s.n_window == 0 and not s.unlocked  # fresh
    assert ad.requests[-1].task_type == "grade_tier_exam"
    lc.debrief_complete(nid)
    assert lc.view(nid)["status"] == "done" and read_outcomes(data, T)[nid].label == "success"
    assert matching(lc)["tier"] == 4


def test_exam_fail_logs_misconceptions_keeps_the_tier_and_reapplies_the_unlock(data, tmp_path):
    seed(data, n=26, v=1.0)  # plenty of margin: mastery stays >= 65 after a failed exam
    lc, _ = lc_for(data, tmp_path, {"build_exam": [exam_json(), exam_json()],
                                    "grade_tier_exam": [grades([3, 3, 3, 3, 3, 0], mis=[{"text": "Q is bandwidth", "subskill": "matching.bandwidth"}])]})  # fmt: skip
    nid = exams_for(lc).start("matching")["node_id"]
    d = take(lc, nid)
    assert d["oral"]["passed"] is False and d["oral"]["exam"]["tier_up"] is False and d["oral"]["remediation"] is None
    events = read_events(lc.store.layout.events_path, T)[0]
    assert score_domain(events, "matching").tier == 3
    mis = [m for m in fold(read_records(lc.store.layout.misconceptions_path, T)) if m["node_id"] == nid]
    assert {m["subskill"] for m in mis} >= {"matching.bandwidth", SUBS[(6 - 1) % 3]}  # the grader's + the zero-scored item's
    with pytest.raises(LifecycleError):  # an exam never spawns a remediation node
        lc.oral.spawn_remediation(lc.store.find(nid))
    lc.debrief_complete(nid)
    d2 = matching(lc)
    assert d2["exam"]["state"] == "available" and d2["exam"]["attempts"][0]["passed"] is False  # may retake: a new exam node
    assert exams_for(lc).start("matching")["node_id"] != nid


def test_exam_mean_below_2_3_fails_even_without_a_zero(data, tmp_path):
    seed(data)
    lc, _ = lc_for(data, tmp_path, {"build_exam": [exam_json()], "grade_tier_exam": [grades([2, 2, 2, 2, 3, 2])]})  # mean 2.17
    d = take(lc, exams_for(lc).start("matching")["node_id"])
    assert d["oral"]["passed"] is False


def test_exam_grading_outage_is_queued_then_applied(data, tmp_path):
    seed(data)
    lc, ad = lc_for(data, tmp_path, {"build_exam": [exam_json()], "grade_tier_exam": [LLMError("down")]})
    nid = exams_for(lc).start("matching")["node_id"]
    d = take(lc, nid)
    assert d["oral"]["status"] == "pending" and not [e for e in read_events(lc.store.layout.events_path, T)[0] if e.type == "tier_exam_item"]
    ad.script["grade_tier_exam"] = [grades([3, 3, 3, 3, 3, 3])]
    assert lc.retry_grades(force=True) == 1
    assert score_domain(read_events(lc.store.layout.events_path, T)[0], "matching").tier == 4


def test_exam_node_needs_no_prelab_and_has_no_workspace_stages(data, tmp_path):
    seed(data)
    lc, _ = lc_for(data, tmp_path, {"build_exam": [exam_json()]})
    nid = exams_for(lc).start("matching")["node_id"]
    v = lc.view(nid)
    assert v["status"] == "oral" and v["kind"] == "tier_exam" and v["prelab"]["items"] == [] and not v["rescopable"]
    with pytest.raises(LifecycleError):
        lc.prelab_submit(nid, {})
    plan = lc.oral.start(lc.store.find(nid), "text", False)["plan"]
    assert plan["n_core"] == 6 and plan["n_interleaved"] == 0 and "tier exam" in plan["reason"]


# ---- Planner offer + API ---------------------------------------------------------------------------------------


def test_planner_appends_the_exam_offer_when_unlocked(data, tmp_path):
    opts = {"options": [_opt(), _opt(domain="noise", subskills=["noise.figure_cascade"], tier=3)]}
    p = Planner(data, T, _router(tmp_path, Scripted({"plan_branches": [opts, opts]})), "rfic")
    assert [o["kind"] for o in p.generate()["options"]] == ["challenge", "challenge"]
    seed(data)
    out = p.generate()
    assert [o["kind"] for o in out["options"]] == ["challenge", "challenge", "tier_exam"]
    ex = out["options"][-1]
    assert ex["domain"] == "matching" and ex["tier"] == 3 and "unlocked" in ex["why_now"]
    assert p.last_options() == out


def test_api_competency_start_exam_and_build_guard(data, tmp_path, monkeypatch):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(data))
    seed(data)
    opts = {"options": [_opt(), _opt(domain="noise", subskills=["noise.figure_cascade"], tier=3)]}
    app = create_app()
    app.state.llm_router = _router(tmp_path, Scripted({"plan_branches": [opts], "build_exam": [exam_json()]}))
    with TestClient(app) as c:
        comp = c.get("/api/competency").json()
        m = next(x for x in comp["domains"] if x["domain"] == "matching")
        assert comp["track"] == T and m["exam"]["state"] == "available" and m["tiers"][2]["state"] == "current"
        o = c.post("/api/planner/options").json()
        assert o["options"][-1]["kind"] == "tier_exam"
        r = c.post("/api/build", json={"option_index": len(o["options"]) - 1})
        assert r.status_code == 422 and "/api/competency/matching/exam" in r.json()["detail"]
        assert c.post("/api/competency/noise/exam").status_code == 409
        started = c.post("/api/competency/matching/exam")
        assert started.status_code == 200 and started.json()["reused"] is False
        nid = started.json()["node_id"]
        assert c.get(f"/api/nodes/{nid}").json()["kind"] == "tier_exam"
        assert c.get("/api/graph").json()["nodes"][-1]["kind"] == "tier_exam"
        assert c.post("/api/competency/matching/exam").json() == {**started.json(), "reused": True}


def test_exams_are_track_scoped(tmp_path):
    root = make_data(tmp_path / "data", T, "other")
    for t in (T, "other"):
        FrameworkStore(DataLayout(root, t).framework_dir).freeze(Framework.model_validate(template_framework()))
    seed(root, tier=1, track=T)  # no diagnostic: both tracks start at T1
    lc, _ = lc_for(root, tmp_path, {"build_exam": [exam_json()]})
    other, _ = lc_for(root, tmp_path, {}, track="other")
    a, b = matching(lc), matching(other)
    assert a["exam"]["state"] == "available" and b["exam"]["state"] == "locked" and b["mastery"] == 0
    exams_for(lc).start("matching")
    assert NodeStore(root, "other").all() == [] and exams_for(other).offers() == []
    with pytest.raises(LifecycleError):
        exams_for(other).start("matching")
