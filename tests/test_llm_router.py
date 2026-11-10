import json
import subprocess

import httpx
import pytest

from app.llm.adapters.claude_code import STRIPPED_ENV, ClaudeCodeAdapter, clean_env
from app.llm.adapters.mock import MockAdapter
from app.llm.adapters.openrouter import OpenRouterAdapter
from app.llm.allowlist import is_free_model
from app.llm.config import load_routing, parse_routing
from app.llm.router import Router
from app.llm.store import Store
from app.llm.types import GradingUnavailable, LLMError, ModelNotAllowed, Request, RetryableError

FREE_A = "google/gemma-4-31b-it:free"  # fixture: retryable 429
FREE_B = "nvidia/nemotron-3-ultra-550b-a55b:free"
MSG = [{"role": "user", "content": "hi"}]


def make_router(tmp_path, tasks, **top):
    cfg = parse_routing({"tasks": tasks, **top})
    mock = MockAdapter()
    adapters = {n: mock for n in ("claude_code", "cursor_cli", "openrouter", "mock")}
    return Router(cfg, Store(tmp_path / "llm.db"), adapters, allow_library_to_free=lambda: False), mock


def test_allowlist():
    assert is_free_model("a/b:free") and is_free_model("openrouter/free")
    assert not is_free_model("openai/gpt-4o") and not is_free_model("openrouter/auto")
    assert not is_free_model("openrouter/free-ish") and not is_free_model("a/b:free:extended")


def test_paid_id_rejected_at_load():
    with pytest.raises(ModelNotAllowed):
        parse_routing({"tasks": {"summarize": {"chain": [{"backend": "openrouter", "model": "openai/gpt-4o"}]}}})


def test_paid_id_rejected_by_adapter_without_http():
    def boom(request):
        raise AssertionError("no HTTP call may be made")

    a = OpenRouterAdapter(api_key=lambda: "k", transport=httpx.MockTransport(boom))
    with pytest.raises(ModelNotAllowed):
        a.complete(Request("summarize", MSG), "openai/gpt-4o", 5)


def test_shipped_routing_yaml_is_valid_and_complete():
    cfg = load_routing()
    spec_tasks = {
        "plan_branches", "build_challenge", "draft_framework", "rescope", "grade_oral", "grade_prelab_explain",
        "grade_diagnostic", "grade_tier_exam", "interview", "tutor_chat", "oral_dialogue",
        "diagnostic_dialogue", "summarize",
    }
    assert set(cfg.tasks) == spec_tasks
    assert [e.backend for e in cfg.tasks["plan_branches"].chain] == ["claude_code", "cursor_cli"]
    g = cfg.tasks["grade_oral"]
    assert g.grading and len(g.chain) == 1 and g.chain[0].model
    assert cfg.tasks["tutor_chat"].chain[-1].backend == "claude_code"


def test_mock_call_logs_and_counts(tmp_path):
    r, _ = make_router(tmp_path, {"plan_branches": {"chain": [{"backend": "mock", "model": "m"}]}})
    res = r.complete("plan_branches", MSG, node_id=7)
    assert res.json["options"][0]["tier"] == 2 and res.backend == "mock"
    (call,) = r.store.calls()
    assert (call["task_type"], call["backend"], call["status"], call["node_id"]) == ("plan_branches", "mock", "ok", 7)
    assert r.store.count("mock", "m") == 1


def test_failover_on_429(tmp_path):
    chain = [{"backend": "openrouter", "model": FREE_A}, {"backend": "openrouter", "model": FREE_B}]
    r, mock = make_router(tmp_path, {"summarize": {"chain": chain}})
    res = r.complete("summarize", MSG)
    assert res.model == FREE_B and res.text == "A short summary."
    assert [m for _, m in mock.calls] == [FREE_A, FREE_B]
    assert [c["status"] for c in reversed(r.store.calls())] == ["retryable_error", "ok"]


def test_all_fail_raises(tmp_path):
    r, _ = make_router(tmp_path, {"summarize": {"chain": [{"backend": "openrouter", "model": FREE_A}]}})
    with pytest.raises(LLMError, match="all backends failed"):
        r.complete("summarize", MSG)


def test_non_retryable_error_does_not_fail_over(tmp_path):
    chain = [{"backend": "mock", "model": "x"}, {"backend": "mock", "model": "y"}]
    r, mock = make_router(tmp_path, {"nofx": {"chain": chain}})
    with pytest.raises(LLMError, match="no mock fixture"):
        r.complete("nofx", MSG)
    assert len(mock.calls) == 1


def test_per_model_cap_skips(tmp_path):
    chain = [
        {"backend": "openrouter", "model": FREE_A, "daily_cap": 1},
        {"backend": "openrouter", "model": FREE_B},
    ]
    r, mock = make_router(tmp_path, {"summarize": {"chain": chain}})
    r.complete("summarize", MSG)  # A attempted (429) and counted -> at cap; B answers
    mock.calls.clear()
    res = r.complete("summarize", MSG)
    assert res.model == FREE_B and [m for _, m in mock.calls] == [FREE_B]


def test_backend_cap_applies_across_models(tmp_path):
    r, _ = make_router(
        tmp_path, {"summarize": {"chain": [{"backend": "openrouter", "model": FREE_B}]}},
        backend_daily_caps={"openrouter": 2},
    )
    r.complete("summarize", MSG)
    r.complete("summarize", MSG)
    with pytest.raises(LLMError, match="daily cap"):
        r.complete("summarize", MSG)
    assert r.store.usage_today() == [{"backend": "openrouter", "model": FREE_B, "count": 2}]


def test_counters_are_per_day(tmp_path):
    s = Store(tmp_path / "x.db")
    s.increment("openrouter", FREE_B)
    assert s.count("openrouter", FREE_B) == 1
    assert s.count("openrouter", FREE_B, day="2000-01-01") == 0


def test_grading_never_fails_over_and_queues(tmp_path):
    chain = [{"backend": "mock", "model": "pinned-grader"}, {"backend": "mock", "model": "other"}][:1]
    r, mock = make_router(tmp_path, {"grade_oral": {"grading": True, "chain": chain}})
    good_root = mock.root
    bad = tmp_path / "fx"
    (bad / "grade_oral").mkdir(parents=True)
    (bad / "grade_oral" / "default.json").write_text('{"error": "retryable", "message": "usage limit"}')
    mock.root = bad
    with pytest.raises(GradingUnavailable):
        r.complete("grade_oral", MSG, node_id=3)
    assert [m for _, m in mock.calls] == ["pinned-grader"]
    (row,) = r.store.pending_grades()
    assert row["node_id"] == 3 and row["status"] == "pending"
    assert r.retry_pending_grades() == []  # backoff: not due yet
    assert r.retry_pending_grades(now=1e12) == []  # due, still failing
    (row,) = r.store.pending_grades()
    assert row["attempts"] == 1
    mock.root = good_root  # grader recovers
    seen = []
    done = r.retry_pending_grades(handler=lambda row, res: seen.append(res), now=1e12)
    assert len(done) == 1 and seen[0].json["items"][0]["score"] == 2
    assert r.store.pending_grades() == []


def test_library_text_blocked_from_free_models(tmp_path):
    chain = [{"backend": "openrouter", "model": FREE_B}, {"backend": "claude_code"}]
    r, mock = make_router(tmp_path, {"summarize": {"chain": chain}})
    r.complete("summarize", MSG, library_text=True)
    assert [m for _, m in mock.calls] == [""]
    mock.calls.clear()
    r.allow_library_to_free = lambda: True
    r.complete("summarize", MSG, library_text=True)
    assert [m for _, m in mock.calls] == [FREE_B]


def test_claude_env_stripped(monkeypatch):
    for k in STRIPPED_ENV:
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = clean_env()
    assert not set(STRIPPED_ENV) & set(env) and env["KEEP_ME"] == "1"


def test_claude_cmd_flags():
    a = ClaudeCodeAdapter(binary="claude")
    cmd = a.build_cmd(Request("t", MSG, tools=["mcp__rfic__run_ngspice", "Bash(ls:*)"], mcp_config="m.json"), "claude-x")
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--model") + 1] == "claude-x"
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__rfic__run_ngspice,Bash(ls:*)"
    assert cmd[cmd.index("--disallowedTools") + 1] == "Glob,Grep,Read"  # dontAsk lets read-only built-ins through otherwise
    with pytest.raises(LLMError, match="always disallowed"):
        a.build_cmd(Request("t", MSG, tools=["Read"]), "")
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--output-format") + 1] == "json"


def test_claude_adapter_subprocess_parsing(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        out = {"result": '```json\n{"a": 1}\n```', "is_error": False,
               "usage": {"input_tokens": 5, "output_tokens": 2}, "modelUsage": {"claude-x": {}}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = ClaudeCodeAdapter(binary="claude").complete(Request("t", MSG, schema={"type": "object"}), "claude-x", 5)
    assert res.json == {"a": 1} and res.usage == {"input_tokens": 5, "output_tokens": 2} and res.model == "claude-x"
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_claude_adapter_rate_limit_is_retryable(monkeypatch):
    out = {"result": "5-hour usage limit reached", "is_error": True}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, json.dumps(out), ""))
    with pytest.raises(RetryableError):
        ClaudeCodeAdapter(binary="claude").complete(Request("t", MSG), "", 5)


def _or(handler):
    return OpenRouterAdapter(api_key=lambda: "k", transport=httpx.MockTransport(handler))


def test_openrouter_ok():
    def h(request):
        assert request.headers["authorization"] == "Bearer k"
        assert json.loads(request.content)["model"] == FREE_B
        return httpx.Response(200, json={"model": FREE_B, "choices": [{"message": {"content": '{"x": 2}'}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    res = _or(h).complete(Request("t", MSG, schema={"type": "object"}), FREE_B, 5)
    assert res.json == {"x": 2} and res.usage == {"input_tokens": 3, "output_tokens": 1}


@pytest.mark.parametrize("resp", [
    httpx.Response(429, text="slow down"),
    httpx.Response(503, text="down"),
    httpx.Response(200, json={"error": {"code": 429, "message": "rate"}}),
])
def test_openrouter_retryable(resp):
    with pytest.raises(RetryableError):
        _or(lambda request: resp).complete(Request("t", MSG), FREE_B, 5)


def test_openrouter_timeout_is_retryable():
    def h(request):
        raise httpx.ReadTimeout("t")

    with pytest.raises(RetryableError):
        _or(h).complete(Request("t", MSG), FREE_B, 5)


def test_openrouter_auth_error_is_fatal():
    with pytest.raises(LLMError) as ei:
        _or(lambda request: httpx.Response(401, text="no")).complete(Request("t", MSG), FREE_B, 5)
    assert not isinstance(ei.value, RetryableError)
