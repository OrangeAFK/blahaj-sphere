"""M5: interleaving context, minimal Planner, production Builder job (mock LLM, fake checker)."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.builder.context import interleaving_context, learner_state
from app.builder.service import BuildError, BuildService, slugify
from app.competency.engine import base_value_weight
from app.datastore.cache import rebuild_db
from app.datastore.events import append_event, new_event
from app.datastore.layout import DataLayout
from tests.helpers import TRACK as T, make_data
from app.domains import BuilderHooks, Domain
from app.llm.config import load_routing
from app.llm.router import Router
from app.llm.store import Store
from app.llm.types import Result
from app.main import create_app
from app.onboarding.framework import Framework, FrameworkStore
from app.planner.service import Planner, PlannerError, validate_options
from tests.test_gate import CHECKER, NODE_MD, ORAL_KEY, PRELAB_KEY, FakeRunner
from tests.test_onboarding import ALL, Scripted, template_framework

OPTION = {"title": "Set an RC low-pass corner", "why_now": "w", "kind": "challenge", "domain": "filters",
          "subskills": ["filters.rc_corner", "sparams.reflection_coefficient"], "tier": 1, "size_est_min": 20}  # fmt: skip


def _diag(root: Path, domain: str, subskill: str, tier: int, score: int, node_id: int = 0, type_: str = "diagnostic_item", track: str = T) -> None:
    v, w = base_value_weight(type_, score=score)
    append_event(DataLayout(root).events_path, new_event(track=track, node_id=node_id, domain=domain, subskill=subskill, tier=tier, type=type_,
                                                         value=v, weight=w, grader_model="m", framework_version=1))  # fmt: skip


@pytest.fixture
def data(tmp_path) -> Path:
    root = make_data(tmp_path / "data")
    lay = DataLayout(root, T)
    FrameworkStore(lay.framework_dir).freeze(Framework.model_validate(template_framework()))
    lay.goals_path.write_text("# Goals\n60 GHz LNA in SG13G2\n", encoding="utf-8")
    _diag(root, "matching", "matching.l_network", 1, 3)
    _diag(root, "matching", "matching.l_network", 2, 2)
    _diag(root, "noise", "noise.figure_cascade", 1, 2)
    _diag(root, "noise", "noise.sources", 2, 2)
    _diag(root, "devices", "devices.biasing", 1, 1)
    _diag(root, "matching", "matching.general", 1, 2)  # not a framework subskill: never a target
    rebuild_db(root)
    return root


def _node(root: Path, nid: int, kind: str, subskills: list[str]) -> None:
    d = DataLayout(root, T).node_dir(nid, f"n{nid}")
    d.mkdir(parents=True)
    fm = {"id": nid, "slug": f"n{nid}", "kind": kind, "domain": subskills[0].split(".")[0], "subskills": subskills, "tier": 1, "status": "prelab"}
    (d / "node.md").write_text("---\n" + yaml.safe_dump(fm) + "---\nbrief\n", encoding="utf-8")


# ---- interleaving context -------------------------------------------------------------------------


def test_stale_subskills_from_diagnostic_only(data):
    ctx = interleaving_context(data, T, {"subskills": ["matching.l_network"]})
    assert ctx.open_misconceptions == []
    assert ctx.stale_subskills == ["noise.figure_cascade", "noise.sources", "devices.biasing"]  # oldest evidence first, own excluded


def test_recent_nodes_evidence_removes_stale_and_diagnostic_is_not_a_node(data):
    _node(data, 1, "challenge", ["noise.figure_cascade"])
    _diag(data, "noise", "noise.figure_cascade", 1, 3, node_id=1, type_="oral_item")
    rebuild_db(data)
    ctx = interleaving_context(data, T, {"subskills": ["passives.inductor_q"]})
    assert ctx.stale_subskills == ["matching.l_network", "noise.sources", "devices.biasing"]
    # three newer challenge nodes push node 1 out of the window: its subskill is stale again
    for nid in (2, 3, 4):
        _node(data, nid, "challenge", ["passives.inductor_q"])
    rebuild_db(data)
    assert "noise.figure_cascade" in interleaving_context(data, T, {"subskills": []}, limit=10).stale_subskills


def test_misconceptions_come_first_and_targets_are_capped(data):
    with closing(sqlite3.connect(DataLayout(data).cache_path)) as c, c:
        c.execute("INSERT INTO misconceptions (track, id, text, subskill, node_id, resolved) VALUES ('rfic-default', 1, 'Q only rises', 'passives.inductor_q', 1, 0)")
        c.execute("INSERT INTO misconceptions (track, id, text, subskill, node_id, resolved) VALUES ('rfic-default', 2, 'old', 'noise.sources', 1, 1)")
    ctx = interleaving_context(data, T, {"subskills": []}, limit=3)
    assert ctx.open_misconceptions == [{"text": "Q only rises", "subskill": "passives.inductor_q"}]
    assert ctx.stale_subskills == ["matching.l_network", "noise.figure_cascade"]
    assert ctx.targets() == {"passives.inductor_q", "matching.l_network", "noise.figure_cascade"}


def test_learner_state(data):
    s = learner_state(data, T)
    assert "60 GHz" in s["goals"] and s["summary"] == "No completed nodes yet." and s["recent_nodes"] == []
    assert s["scores"]["matching"]["tier"] == 3 and s["scores"]["devices"]["tier"] == 1


# ---- planner ---------------------------------------------------------------------------------------


def _opt(**kw):
    return {"title": "Hand-design an L-match", "why_now": "goals need matching", "kind": "challenge", "domain": "matching",
            "subskills": ["matching.l_network"], "tier": 2, "size_est_min": 40, **kw}  # fmt: skip


class Agent(Scripted):
    """Scripted, but a callable may return a full Result (the fake Builder reports usage/meta like claude_code)."""

    def complete(self, req, model, timeout_s):
        q = self.script.get(req.task_type)
        if q and callable(q[0]):
            self.requests.append(req)
            out = q.pop(0)(req)
            if isinstance(out, Result):
                return out
            return Result(text=json.dumps(out), json=out, usage={}, backend="mock", model="mock-model")
        return super().complete(req, model, timeout_s)


def _router(tmp_path, adapter) -> Router:
    return Router(load_routing(), Store(tmp_path / "llm.db"), {n: adapter for n in ALL})


def test_planner_returns_and_stores_valid_options(data, tmp_path):
    ad = Scripted({"plan_branches": [{"options": [_opt(), _opt(domain="noise", subskills=["noise.figure_cascade"], tier=3)]}]})
    p = Planner(data, T, _router(tmp_path, ad), "rfic")
    out = p.generate()
    assert [o["domain"] for o in out["options"]] == ["matching", "noise"] and out["model"] == "mock-model"
    assert p.last_options() == out
    req = ad.requests[0]
    assert req.task_type == "plan_branches" and not req.tools and "Planner" in req.messages[0]["content"]
    inp = json.loads(req.messages[1]["content"])
    assert inp["scores"]["matching"]["tier"] == 3 and "60 GHz" in inp["goals"] and inp["difficulty"]["challenge_nodes"] == 0


def test_planner_repairs_once_with_errors_then_fails(data, tmp_path):
    bad = {"options": [_opt(subskills=["matching.nope"]), _opt(domain="devices", subskills=["devices.biasing"], tier=2)]}
    ad = Scripted({"plan_branches": [bad, {"options": [_opt(), _opt(tier=1)]}]})
    assert len(Planner(data, T, _router(tmp_path, ad), "rfic").generate()["options"]) == 2
    fix = ad.requests[1].messages[-1]["content"]
    assert "unknown subskills ['matching.nope']" in fix and "tier must be an integer 1..1" in fix  # devices is at T1
    ad = Scripted({"plan_branches": [bad, bad]})
    with pytest.raises(PlannerError) as e:
        Planner(data, T, _router(tmp_path, ad), "rfic").generate()
    assert e.value.status == 502


@pytest.mark.parametrize(
    "raw, needle",
    [
        ({"options": [_opt()]}, "2-4"),
        ({"options": [_opt(), _opt(domain="rf")]}, "not in the framework"),
        ({"options": [_opt(), _opt(subskills=["noise.sources"])]}, "must belong to domain matching"),
        ({"options": [_opt(), _opt(size_est_min=500)]}, "size_est_min"),
        ({"options": [_opt(), _opt(kind="tier_exam")]}, "kind must be"),
        ({"options": [_opt(), {"title": "x"}]}, "missing keys"),
    ],
)
def test_option_validation(raw, needle):
    fw = Framework.model_validate(template_framework())
    clean, errors = validate_options(raw, fw, {"matching": {"tier": 3}})
    assert clean == [] and any(needle in e for e in errors), errors


def test_planner_needs_framework_and_tiers(tmp_path):
    with pytest.raises(PlannerError) as e:
        Planner(tmp_path, T, None, "rfic").planner_input()
    assert e.value.status == 409


# ---- builder -----------------------------------------------------------------------------------------


class FakeBuilder:
    """Stands in for `claude -p`: writes a node into the request cwd (optionally broken) and logs MCP-style calls."""

    def __init__(self, hooks_state: dict, broken: int = 0, interleave: str = "matching.l_network"):
        self.state, self.broken, self.interleave = hooks_state, broken, interleave
        self.prompts: list[str] = []

    def __call__(self, req):
        self.prompts.append(req.messages[0]["content"])
        inp = json.loads(req.messages[0]["content"].split("```json\n", 1)[1].split("\n```", 1)[0])
        n = Path(req.cwd)
        (n / "workspace").mkdir(exist_ok=True)
        (n / "workspace" / "rc.sp").write_text("R1 a b 10k\n")
        (n / "_hidden" / "reference_solution").mkdir(parents=True, exist_ok=True)
        (n / "_hidden" / "reference_solution" / "rc.sp").write_text("R1 a b 1k\n")
        o = inp["option"]
        md = (NODE_MD.replace("id: 901", f"id: {inp['node']['id']}").replace("slug: rc-lowpass", f"slug: {inp['node']['slug']}")
              .replace("domain: filters", f"domain: {o['domain']}").replace("tier: 1\n", f"tier: {o['tier']}\n")
              .replace("size_est_min: 20", f"size_est_min: {o['size_est_min']}")
              .replace("subskills: [filters.rc_corner, sparams.reflection_coefficient]", f"subskills: {json.dumps(o['subskills'])}"))
        hints = {"ladders": [{"subskill": s, "hints": ["think", "narrower", "method"]} for s in o["subskills"]]}
        (n / "node.md").write_text(md, encoding="utf-8")
        (n / "prelab.md").write_text("**p1** corner?\n**p2** R?\n**p3** explain\n", encoding="utf-8")
        (n / "checker.py").write_text(CHECKER)
        ok = json.loads(json.dumps(ORAL_KEY))
        ok["items"].append({"id": "o3", "subskill": self.interleave, "tier": 1, "question": "link?",
                            "anchors": {0: "a", 1: "b", 2: "c", 3: "d"}, "probes": ["p"], "interleaved": True})  # fmt: skip
        for name, doc in (("_hidden/prelab_key.yaml", PRELAB_KEY), ("_hidden/oral_key.yaml", ok), ("hints.yaml", hints)):
            (n / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        if self.broken > 0:
            self.broken -= 1
            (n / "hints.yaml").unlink()
        with open(self.state["call_log"], "a", encoding="utf-8") as f:
            for tool in ("write_file", "run_checker", "delete_file"):
                f.write(json.dumps({"tool": tool, "is_error": False}) + "\n")
        return Result(text='{"node": "x"}', json=None, usage={"input_tokens": 10, "output_tokens": 5}, backend="mock",
                      model="claude-sonnet-5-5", meta={"num_turns": 7, "permission_denials": []})  # fmt: skip


def fake_domain(tmp_path, runner=None) -> tuple[Domain, dict]:
    prompt = tmp_path / "builder.md"
    prompt.write_text("# Builder prompt\n", encoding="utf-8")
    state: dict = {}

    def mcp_config(node_dir, out, call_log):
        state["call_log"] = call_log
        out.write_text(json.dumps({"node": node_dir.name}))
        return out

    hooks = BuilderHooks(prompt=prompt, tool_names=lambda: ["mcp__x__write_file"], mcp_config=mcp_config)
    return Domain(name="fake", image="x", image_size_gb=0, run_checker=runner or FakeRunner(), builder=hooks), state


def review(fail: str | None = None, quote: str = "kind: challenge") -> dict:
    """A fidelity-review reply; `fail` names the check that fails."""
    return {"checks": [{"id": i, "pass": i != fail, "file": "node.md", "quote": quote, "reason": f"{i} reason"}
                       for i in ("method_fidelity", "metrics_simulated", "no_answers_in_starter")]}  # fmt: skip


def _build_router(tmp_path, fb, reviews=None) -> tuple[Router, Scripted]:
    ad = Agent({"build_challenge": [fb, fb, fb], "review_build": reviews if reviews is not None else [review()] * 3})
    return _router(tmp_path, ad), ad


def test_slugify():
    assert slugify("Hand-design an L-match at 60 GHz!") == "hand-design-an-l-match-at-60-ghz"
    assert len(slugify("x" * 30 + " " + "y" * 30)) <= 40 and slugify("!!") == "node"


def test_build_passes_indexes_node_and_records_attempt(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    fb = FakeBuilder(state)
    router, ad = _build_router(tmp_path, fb)
    svc = BuildService(data, T, router, dom)
    plan = svc.prepare(OPTION)
    assert plan.node_id == 1 and plan.name == "0001-set-an-rc-low-pass-corner"
    assert plan.ctx.stale_subskills == ["matching.l_network", "noise.figure_cascade", "noise.sources", "devices.biasing"]
    rec = svc.run(plan)
    assert rec["passed"] and len(rec["attempts"]) == 1 and rec["sim_runtime_s"] == 5
    att = rec["attempts"][0]
    assert att["tool_calls"] == {"write_file": 1, "run_checker": 1, "delete_file": 1} and att["meta"]["num_turns"] == 7
    assert att["usage"]["output_tokens"] == 5 and att["model"] == "claude-sonnet-5-5"
    assert rec["gate4_hits"] == [{"key": "oral", "id": "o3", "subskill": "matching.l_network", "interleaved": True}]
    req = ad.requests[0]
    assert req.task_type == "build_challenge" and req.cwd == str(plan.node_dir) and req.tools == ["mcp__x__write_file"] and req.node_id == 1
    assert '"stale_subskills": [' in fb.prompts[0] and "# Builder prompt" in fb.prompts[0]
    assert json.loads((data / "builds" / T / plan.name / "record.json").read_text())["passed"]
    with closing(sqlite3.connect(DataLayout(data).cache_path)) as c:
        assert c.execute("SELECT id, kind FROM nodes WHERE id = 1").fetchone() == (1, "challenge")  # indexed
    assert svc.prepare(OPTION).node_id == 2


def test_build_retries_with_gate_errors(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    fb = FakeBuilder(state, broken=1)
    router, _ = _build_router(tmp_path, fb)
    rec = BuildService(data, T, router, dom).run(BuildService(data, T, router, dom).prepare(OPTION))
    assert rec["passed"] and len(rec["attempts"]) == 2
    assert "Previous attempt FAILED the gate" not in fb.prompts[0]
    assert "Previous attempt FAILED the gate" in fb.prompts[1] and "missing file: hints.yaml" in fb.prompts[1]


def test_build_failing_three_times_moves_node_aside(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    fb = FakeBuilder(state, interleave="filters.rc_corner")  # flagged item on a non-target subskill: gate 4 fails every time
    router, ad = _build_router(tmp_path, fb)
    svc = BuildService(data, T, router, dom)
    plan = svc.prepare(OPTION)
    with pytest.raises(BuildError) as e:
        svc.run(plan)
    assert "gate 4" in e.value.message and len(ad.requests) == 3
    assert not plan.node_dir.exists()
    rec = svc.load_record(plan.name)
    assert not rec["passed"] and rec["moved_to"].startswith(f"builds/{T}/failed/0001-") and (data / rec["moved_to"] / "node.md").is_file()
    assert svc.prepare(OPTION).node_id == 2  # the failed id is not reused


def test_build_rejects_node_md_that_does_not_match_the_option(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    fb = FakeBuilder(state)

    def drifting(req):  # the agent "improves" the option: tier 2 instead of the given 1
        out = fb(req)
        md = Path(req.cwd) / "node.md"
        md.write_text(md.read_text(encoding="utf-8").replace("tier: 1\n", "tier: 2\n"), encoding="utf-8")
        return out

    router, _ = _build_router(tmp_path, drifting)
    svc = BuildService(data, T, router, dom, max_retries=0)
    with pytest.raises(BuildError) as e:
        svc.run(svc.prepare(OPTION))
    assert "node.md tier must be 1 (given), got 2" in e.value.message


def test_domain_without_builder_is_rejected(data):
    with pytest.raises(BuildError) as e:
        BuildService(data, T, None, Domain(name="x", image="x", image_size_gb=0))
    assert e.value.status == 501


def test_rfic_domain_builder_hooks():
    from app.domains import load_domain

    dom = load_domain("rfic")
    assert dom.builder is not None and dom.builder.prompt.name == "builder.md" and dom.builder.prompt.is_file()
    assert "mcp__rfic__delete_file" in dom.builder.tool_names()


# ---- API: planner options -> build job ------------------------------------------------------------


def test_api_planner_then_build_job(data, tmp_path, monkeypatch):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(data))
    dom, state = fake_domain(tmp_path)
    ad = Agent({"plan_branches": [{"options": [_opt(), _opt(domain="noise", subskills=["noise.sources"], tier=3)]}],
                "build_challenge": [FakeBuilder(state)], "review_build": [review()]})
    app = create_app()
    app.state.domain = dom
    app.state.llm_router = _router(tmp_path, ad)
    with TestClient(app) as c:
        assert c.get("/api/planner/options").status_code == 404
        assert c.post("/api/build", json={"option_index": 0}).status_code == 409
        opts = c.post("/api/planner/options").json()
        assert len(opts["options"]) == 2 and c.get("/api/planner/options").json() == opts
        assert c.post("/api/build", json={"option_index": 5}).status_code == 422
        r = c.post("/api/build", json={"option_index": 1})
        assert r.status_code == 202, r.text
        started = r.json()
        assert started["node_dir"] == "0001-hand-design-an-l-match" and started["interleaving_targets"]["stale_subskills"]
        for _ in range(200):
            job = c.get(f"/api/jobs/{started['job_id']}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert job["status"] == "done" and job["type"] == "build_challenge" and job["node_id"] == f"{T}/{started['node_dir']}", job
        rec = c.get(f"/api/build/{started['node_dir']}").json()
        assert rec["passed"] and rec["gate4_hits"]
        assert c.get("/api/build/..%2Fx").status_code in (404, 422)


def test_shipped_plan_branches_fixture_passes_planner_validation(data, tmp_path):
    from app.llm.adapters.mock import MockAdapter

    out = Planner(data, T, _router(tmp_path, MockAdapter()), "rfic").generate()
    assert [o["domain"] for o in out["options"]] == ["matching", "noise"]


def test_dead_toolchain_aborts_without_burning_retries(data, tmp_path):
    from app.preflight import Check

    dom, state = fake_domain(tmp_path)
    up = [True]
    dom.checks = lambda: [] if up[0] else [Check(id="image", label="Toolchain image", status="fail", detail="docker down")]
    fb = FakeBuilder(state, broken=5)

    def crash_mid_build(req):
        out = fb(req)
        up[0] = False  # Docker dies while the agent runs
        return out

    router, ad = _build_router(tmp_path, crash_mid_build)
    svc = BuildService(data, T, router, dom)
    plan = svc.prepare(OPTION)
    with pytest.raises(BuildError) as e:
        svc.run(plan)
    assert e.value.status == 503 and "after the agent of attempt 1" in e.value.message and len(ad.requests) == 1
    rec = svc.load_record(plan.name)
    assert rec["aborted"]["problems"] == ["Toolchain image: docker down"] and not plan.node_dir.exists()
    up[0] = False
    plan2 = svc.prepare(OPTION)
    with pytest.raises(BuildError) as e:
        svc.run(plan2)
    assert "before attempt 1" in e.value.message and len(ad.requests) == 1  # no agent call at all


def test_planner_unescapes_html_in_option_text():
    fw = Framework.model_validate(template_framework())
    clean, errors = validate_options({"options": [_opt(title="Stabilize to K &gt; 1"), _opt()]}, fw, {"matching": {"tier": 3}})
    assert not errors and clean[0]["title"] == "Stabilize to K > 1"


# ---- gate 7: fidelity review ------------------------------------------------------------------------


def test_fidelity_review_runs_last_and_its_failure_is_fed_back(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    fb = FakeBuilder(state)
    bad = review(fail="metrics_simulated", quote="R1 a b 1k")  # quoted from the reference netlist
    bad["checks"][1]["file"] = "_hidden/reference_solution/rc.sp"
    router, ad = _build_router(tmp_path, fb, reviews=[bad, review()])
    svc = BuildService(data, T, router, dom)
    rec = svc.run(svc.prepare(OPTION))
    assert rec["passed"] and len(rec["attempts"]) == 2
    a1 = rec["attempts"][0]
    assert not a1["review"]["passed"] and a1["review"]["checks"][1]["quote_verified"]
    assert 'gate 7 (fidelity review) metrics_simulated: metrics_simulated reason [_hidden/reference_solution/rc.sp: "R1 a b 1k"]' in fb.prompts[1]
    req = next(r for r in ad.requests if r.task_type == "review_build")
    assert not req.tools and not req.mcp_config and "audit one generated fake challenge" in req.messages[0]["content"]
    sent = json.loads(req.messages[1]["content"])
    assert sent["option"] == OPTION and {"node.md", "checker.py", "workspace/rc.sp", "_hidden/reference_solution/rc.sp"} <= set(sent["files"])
    assert sent["metric_sources"] == {"f3db_hz": "out.txt", "r_ohm": "out.txt"} and sent["metrics"]["r_ohm"] == 1000.0


def test_fidelity_review_is_skipped_when_code_gates_fail(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    router, ad = _build_router(tmp_path, FakeBuilder(state, broken=1))
    svc = BuildService(data, T, router, dom)
    assert svc.run(svc.prepare(OPTION))["passed"]
    assert [r.task_type for r in ad.requests] == ["build_challenge", "build_challenge", "review_build"]


def test_unverifiable_quotes(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    router, _ = _build_router(tmp_path, FakeBuilder(state), reviews=[review(quote="not in any file")])
    svc = BuildService(data, T, router, dom, max_retries=0)
    rec = svc.run(svc.prepare(OPTION))
    assert rec["passed"] and len(rec["attempts"][0]["review"]["warnings"]) == 3  # passing quotes not verbatim: warnings
    dom, state = fake_domain(tmp_path)
    router, _ = _build_router(tmp_path, FakeBuilder(state), reviews=[review(fail="method_fidelity", quote="invented")])
    svc = BuildService(data, T, router, dom, max_retries=0)
    with pytest.raises(BuildError) as e:
        svc.run(svc.prepare(OPTION))
    assert "quote not found verbatim; the verdict stands" in e.value.message


def test_review_unavailable_or_malformed_aborts_without_burning_retries(data, tmp_path):
    from app.llm.types import LLMError

    dom, state = fake_domain(tmp_path)
    router, ad = _build_router(tmp_path, FakeBuilder(state), reviews=[{"checks": []}, {"verdict": "ok"}])
    svc = BuildService(data, T, router, dom)
    plan = svc.prepare(OPTION)
    with pytest.raises(BuildError) as e:
        svc.run(plan)
    assert e.value.status == 503 and "unusable output twice" in e.value.message and not plan.node_dir.exists()
    assert [r.task_type for r in ad.requests] == ["build_challenge", "review_build", "review_build"]
    router, _ = _build_router(tmp_path, FakeBuilder(state), reviews=[LLMError("claude down")])
    with pytest.raises(BuildError) as e:
        svc2 = BuildService(data, T, router, dom)
        svc2.run(svc2.prepare(OPTION))
    assert e.value.status == 503 and "claude down" in e.value.message


def test_abandon_marks_status_and_reindexes(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    router, _ = _build_router(tmp_path, FakeBuilder(state))
    svc = BuildService(data, T, router, dom)
    plan = svc.prepare(OPTION)
    svc.run(plan)
    svc.abandon(plan.name)
    with closing(sqlite3.connect(DataLayout(data).cache_path)) as c:
        assert c.execute("SELECT status FROM nodes WHERE id = ?", (plan.node_id,)).fetchone() == ("abandoned",)
    assert "status: abandoned" in (plan.node_dir / "node.md").read_text(encoding="utf-8")
