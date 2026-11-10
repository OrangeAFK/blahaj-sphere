"""Onboarding (SPEC §9, M4): interview -> framework -> diagnostic, all against mock LLM adapters."""

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.competency.engine import Event, initial_tier
from app.datastore.events import read_events
from app.llm.adapters.mock import MockAdapter
from app.llm.config import load_routing
from app.llm.router import Router
from app.llm.store import Store
from app.llm.types import LLMError, Result, RetryableError
from app.main import create_app

TEMPLATE = Path(__file__).resolve().parents[1] / "domains" / "rfic" / "framework_template.yaml"
ALL = ("claude_code", "cursor_cli", "openrouter", "mock")


def template_framework() -> dict:
    t = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    return {"domain_pack": "rfic", "domains": [{**d, "tiers": {str(k): v for k, v in d["tiers"].items()} } for d in t["domains"]]}


class Scripted:
    """Adapter returning queued JSON per task_type (a callable item receives the Request)."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.requests: list = []

    def complete(self, req, model, timeout_s):
        self.requests.append(req)
        q = self.script.get(req.task_type)
        if not q:
            raise LLMError(f"no scripted reply for {req.task_type}")
        item = q.pop(0)
        item = item(req) if callable(item) else item
        if isinstance(item, Exception):
            raise item
        return Result(text=json.dumps(item), json=item, usage={}, backend="mock", model="mock-model")


def make_client(tmp_path, monkeypatch, adapter) -> TestClient:
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path))
    app = create_app()
    app.state.llm_router = Router(load_routing(), Store(tmp_path / "llm.db"), {n: adapter for n in ALL})
    return TestClient(app)


def interview_reply(done=False, msg="Q?"):
    goals = {"target_capability": "60 GHz LNA", "motivation": "thesis", "hours_per_week": "8", "background": "analog course"}
    return {"message": msg, "done": done, "goals": goals if done else None}


def test_interview_writes_goals_md(tmp_path, monkeypatch):
    ad = Scripted({"interview": [interview_reply(msg="What do you want to build?"), interview_reply(msg="Hours per week?"),
                                 interview_reply(done=True, msg="Thanks!")]})
    c = make_client(tmp_path, monkeypatch, ad)
    v = c.post("/api/onboarding/interview/turn", json={}).json()
    assert v["last_message"] == "What do you want to build?" and not v["done"] and v["questions_asked"] == 1
    assert c.post("/api/onboarding/interview/turn", json={}).status_code == 422  # a question is open: answer required
    c.post("/api/onboarding/interview/turn", json={"answer": "an LNA"})
    v = c.post("/api/onboarding/interview/turn", json={"answer": "8"}).json()
    assert v["done"] and "60 GHz LNA" in v["goals"]
    goals = (tmp_path / "profile" / "goals.md").read_text(encoding="utf-8")
    assert "## Target capability" in goals and "thesis" in goals
    sys_msg = ad.requests[0].messages[0]
    assert sys_msg["role"] == "system" and "rfic" in sys_msg["content"]
    assert all(r.light for r in ad.requests)  # interview is a light-tier task
    assert c.get("/api/onboarding/state").json()["stage"] == "framework"


def test_interview_is_capped_at_ten_questions(tmp_path, monkeypatch):
    # a model that never stops asking is forced to finish at question 10
    ad = Scripted({"interview": [interview_reply(msg=f"q{i}") for i in range(10)] + [interview_reply(done=True, msg="done")]})
    c = make_client(tmp_path, monkeypatch, ad)
    v = c.post("/api/onboarding/interview/turn", json={}).json()
    for _ in range(10):
        if v["done"]:
            break
        v = c.post("/api/onboarding/interview/turn", json={"answer": "a"}).json()
    assert v["done"]
    assert len(ad.requests) == 11  # 10 questions + the forced finish call
    assert "Finish now" in ad.requests[-1].messages[-1]["content"]
    assert (tmp_path / "profile" / "goals.md").exists()


def test_interview_unusable_reply_is_502(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, Scripted({"interview": [{"nonsense": 1}]}))
    assert c.post("/api/onboarding/interview/turn", json={}).status_code == 502


def finish_interview(c, tmp_path):
    (tmp_path / "profile").mkdir(parents=True, exist_ok=True)
    (tmp_path / "profile" / "goals.md").write_text("# Goals\n60 GHz LNA", encoding="utf-8")


def test_framework_needs_goals(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, Scripted({}))
    assert c.post("/api/onboarding/framework/draft").status_code == 409


def test_framework_draft_edit_approve_freeze(tmp_path, monkeypatch):
    ad = Scripted({"draft_framework": [template_framework()]})
    c = make_client(tmp_path, monkeypatch, ad)
    finish_interview(c, tmp_path)
    v = c.post("/api/onboarding/framework/draft").json()
    assert not v["frozen"] and len(v["framework"]["domains"]) == 7
    req = ad.requests[0]
    assert not req.light and req.task_type == "draft_framework"
    assert "template" in req.messages[1]["content"] and "60 GHz LNA" in req.messages[1]["content"]
    assert (tmp_path / "framework" / "competency-v1.draft.yaml").exists()

    fw = v["framework"]
    fw["domains"][0]["tiers"]["1"] = "Edited by the user."
    assert c.put("/api/onboarding/framework/draft", json=fw).status_code == 200
    v = c.post("/api/onboarding/framework/approve", json={"framework": fw}).json()
    assert v["frozen"]
    frozen = tmp_path / "framework" / "competency-v1.yaml"
    data = yaml.safe_load(frozen.read_text(encoding="utf-8"))
    assert data["domains"][0]["tiers"][1] == "Edited by the user." and data["version"] == 1
    assert not (tmp_path / "framework" / "competency-v1.draft.yaml").exists()
    # frozen: no re-draft, edit, or second approval
    assert c.post("/api/onboarding/framework/draft").status_code == 409
    assert c.put("/api/onboarding/framework/draft", json=fw).status_code == 409
    assert c.post("/api/onboarding/framework/approve", json={}).status_code == 409
    assert c.get("/api/onboarding/state").json()["stage"] == "diagnostic"


def test_framework_draft_is_repaired_once_then_fails(tmp_path, monkeypatch):
    bad = template_framework()
    bad["domains"] = bad["domains"][:3]  # < 5 domains
    ad = Scripted({"draft_framework": [bad, template_framework()]})
    c = make_client(tmp_path, monkeypatch, ad)
    finish_interview(c, tmp_path)
    assert c.post("/api/onboarding/framework/draft").status_code == 200
    assert "invalid" in ad.requests[1].messages[-1]["content"] and "5-8 domains" in ad.requests[1].messages[-1]["content"]

    ad2 = Scripted({"draft_framework": [bad, bad]})
    c2 = make_client(tmp_path / "b", monkeypatch, ad2)
    finish_interview(c2, tmp_path / "b")
    assert c2.post("/api/onboarding/framework/draft").status_code == 502


@pytest.mark.parametrize("mutate,msg", [
    (lambda fw: fw["domains"][0]["tiers"].pop("3"), "tiers must be exactly 1..5"),
    (lambda fw: fw["domains"][0].update(id="Bad Id"), "snake_case"),
    (lambda fw: fw["domains"][1].update(id=fw["domains"][0]["id"]), "unique"),
    (lambda fw: fw["domains"][0].update(subskills=["NoDot"]), "must look like"),
    (lambda fw: fw["domains"][0]["tiers"].update({"2": "Two\nlines"}), "one non-empty sentence"),
])
def test_framework_validation(tmp_path, monkeypatch, mutate, msg):
    c = make_client(tmp_path, monkeypatch, Scripted({}))
    fw = template_framework()
    mutate(fw)
    r = c.post("/api/onboarding/framework/approve", json={"framework": fw})
    assert r.status_code == 422 and msg in json.dumps(r.json())
    assert not (tmp_path / "framework" / "competency-v1.yaml").exists()


def freeze(c, tmp_path, monkeypatch=None):
    fw = template_framework()
    assert c.post("/api/onboarding/framework/approve", json={"framework": fw}).status_code == 200
    return [d["id"] for d in fw["domains"]]


def probe(domain):
    return {"subskill": f"{domain}.x", "question": f"Question about {domain}?",
            "anchors": {"0": "wrong", "1": "weak", "2": "ok", "3": "great"}}


def grade(score, quote="my answer"):
    return {"items": [{"id": "d1", "score": score, "quote": quote, "rationale": "because"}], "misconceptions": []}


def run_diagnostic(c, scores_by_domain: dict[str, list[int]]):
    """Answer every probe; the score for the n-th probe of a domain comes from scores_by_domain."""
    asked = {}
    v = c.post("/api/onboarding/diagnostic/next").json()
    while not v["done"]:
        p = v["pending"]
        n = asked[p["domain"]] = asked.get(p["domain"], 0) + 1
        assert p["tier"] == n  # climbs one tier per probe, from T1
        r = c.post("/api/onboarding/diagnostic/answer", json={"answer": "my answer"})
        assert r.status_code == 200, r.text
        v = r.json()["view"]
        if not v["done"]:
            v = c.post("/api/onboarding/diagnostic/next").json()
    return v, asked


def test_diagnostic_climbs_until_first_miss_and_sets_tiers(tmp_path, monkeypatch):
    plan = {  # domain -> scores of successive probes
        "passives": [3, 3, 3],   # passes T1..T3 (probe cap) -> start T4
        "matching": [3, 1],      # T1 pass, T2 miss -> T2
        "devices": [1],          # T1 miss -> T1
        "noise": [2, 2, 1],      # score 2 passes the diagnostic bar; T3 miss -> T3
        "linearity": [0],
        "em_layout": [3, 0],
        "measurement": [2, 3, 2],
    }
    seq = [s for d in plan.values() for s in d]
    scores = iter(seq)
    ad = Scripted({
        "diagnostic_dialogue": [lambda r: probe(json.loads(r.messages[1]["content"])["domain"]["id"]) for _ in seq],
        "grade_diagnostic": [lambda r: grade(next(scores)) for _ in seq],
    })
    c = make_client(tmp_path, monkeypatch, ad)
    finish_interview(c, tmp_path)
    freeze(c, tmp_path)
    v, asked = run_diagnostic(c, plan)
    assert v["tiers"] == {"passives": 4, "matching": 2, "devices": 1, "noise": 3, "linearity": 1, "em_layout": 2, "measurement": 4}
    assert asked == {d: len(s) for d, s in plan.items()} and max(asked.values()) <= 3

    events, bad = read_events(tmp_path / "events.jsonl")
    assert not bad and len(events) == len(seq) and all(e.type == "diagnostic_item" and e.node_id == 0 for e in events)
    assert {e.grader_model for e in events} == {"mock-model"} and {e.framework_version for e in events} == {1}
    m = [e for e in events if e.domain == "matching"]
    assert [(e.tier, round(e.value, 3)) for e in m] == [(1, 1.0), (2, round(1 / 3, 3))]
    for dom, want in v["tiers"].items():  # the engine agrees with the stored result
        assert initial_tier([e for e in events if e.domain == dom]) == want

    node = tmp_path / "archive" / "nodes" / "0000-diagnostic" / "node.md"
    fm = yaml.safe_load(node.read_text(encoding="utf-8").split("---")[1])
    assert fm["kind"] == "diagnostic" and fm["id"] == 0 and fm["status"] == "done"
    assert "Question about matching?" in node.read_text(encoding="utf-8")
    import sqlite3
    con = sqlite3.connect(tmp_path / "cache.db")
    assert con.execute("select count(*) from nodes where kind='diagnostic'").fetchone()[0] == 1
    assert dict(con.execute("select domain, tier from scores").fetchall()) == v["tiers"]
    assert c.get("/api/onboarding/state").json()["stage"] == "done"
    # grading ran on the heavy path, dialogue on the light path
    assert all(r.light for r in ad.requests if r.task_type == "diagnostic_dialogue")
    assert not any(r.light for r in ad.requests if r.task_type == "grade_diagnostic")


def test_diagnostic_requires_frozen_framework(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, Scripted({}))
    assert c.post("/api/onboarding/diagnostic/next").status_code == 409


def test_diagnostic_question_is_stable_and_hides_anchors(tmp_path, monkeypatch):
    ad = Scripted({"diagnostic_dialogue": [probe("passives")]})
    c = make_client(tmp_path, monkeypatch, ad)
    freeze(c, tmp_path)
    a = c.post("/api/onboarding/diagnostic/next").json()
    b = c.post("/api/onboarding/diagnostic/next").json()  # reload/retry: same open question, no second LLM call
    assert a["pending"] == b["pending"] and len(ad.requests) == 1
    assert "anchors" not in json.dumps(a)
    assert c.post("/api/onboarding/diagnostic/answer", json={"answer": "  "}).status_code == 422


def test_grading_unavailable_keeps_probe_open(tmp_path, monkeypatch):
    ad = Scripted({"diagnostic_dialogue": [probe("passives")], "grade_diagnostic": [RetryableError("limit reached"), grade(3)]})
    c = make_client(tmp_path, monkeypatch, ad)
    freeze(c, tmp_path)
    c.post("/api/onboarding/diagnostic/next")
    r = c.post("/api/onboarding/diagnostic/answer", json={"answer": "my answer"})
    assert r.status_code == 503
    assert not (tmp_path / "events.jsonl").read_text(encoding="utf-8") if (tmp_path / "events.jsonl").exists() else True
    assert c.get("/api/onboarding/diagnostic").json()["pending"]["domain"] == "passives"
    assert c.post("/api/onboarding/diagnostic/answer", json={"answer": "my answer"}).json()["score"] == 3  # retry works


def test_unverbatim_quote_is_dropped(tmp_path, monkeypatch):
    ad = Scripted({"diagnostic_dialogue": [probe("passives")], "grade_diagnostic": [grade(2, quote="something never said")]})
    c = make_client(tmp_path, monkeypatch, ad)
    freeze(c, tmp_path)
    c.post("/api/onboarding/diagnostic/next")
    c.post("/api/onboarding/diagnostic/answer", json={"answer": "my answer"})
    st = json.loads((tmp_path / "profile" / "diagnostic.json").read_text(encoding="utf-8"))
    assert st["domains"][0]["probes"][0]["quote"] == ""


def test_diagnostic_pass_bar_is_two_for_single_probes():
    def d(tier, s):
        return Event("i", "t", 0, "m", f"m.s{tier}", tier, "diagnostic_item", s / 3, 1.0)

    assert initial_tier([d(1, 2)]) == 2  # one probe scoring 2 passes T1 (the 2.3 exam bar would have failed it)
    assert initial_tier([d(1, 1)]) == 1 and initial_tier([d(1, 3), d(2, 0)]) == 2


def test_mock_fixture_files_drive_the_real_mock_adapter(tmp_path, monkeypatch):
    """The checked-in fixtures (tests/fixtures/llm/*) are valid inputs for the whole flow."""
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path))
    app = create_app()
    mock = MockAdapter()
    app.state.llm_router = Router(load_routing(), Store(tmp_path / "llm.db"), {n: mock for n in ALL})
    c = TestClient(app)
    v = c.post("/api/onboarding/interview/turn", json={}).json()
    assert v["done"] and "60 GHz LNA" in v["goals"]
    assert len(c.post("/api/onboarding/framework/draft").json()["framework"]["domains"]) == 7
    c.post("/api/onboarding/framework/approve", json={})
    p = c.post("/api/onboarding/diagnostic/next").json()["pending"]
    assert p["domain"] == "passives" and p["subskill"] == "matching.l_network"
    assert c.post("/api/onboarding/diagnostic/answer", json={"answer": "loaded Q sets bandwidth"}).json()["score"] == 2
