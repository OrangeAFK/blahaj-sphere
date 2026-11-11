"""M6c: Run simulation job, debrief checker, scores + usage-cap endpoints, rescope backup path. Mock LLM, fake domain."""

import os
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.lifecycle.nodes import LifecycleError
from app.llm.adapters.openrouter import OpenRouterAdapter
from app.llm.types import Request, RetryableError
from tests.test_builder_m5 import _opt, data  # noqa: F401 (data = fixture)
from tests.test_lifecycle import ANSWERS, GRADES, SyncJobs, env, make, text_oral, to_lab  # noqa: F401 (env = fixture)
from tests.test_rescope import Jobs, setup


class FakeRun:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, node_dir, commands, timeout_s, job=None):
        self.calls.append((node_dir.name, list(commands)))
        if job:
            job.log("$ " + commands[0])
        steps = [{"cmd": c, "exit_code": 0 if self.ok else 1, "seconds": 0.5, "timed_out": False, "stderr_tail": "" if self.ok else "boom"} for c in commands]
        return {"ok": self.ok, "steps": steps, "files_changed": [{"path": "out.txt", "size": 12}] if self.ok else []}


def test_run_simulation_job_records_steps_and_files(env, tmp_path):
    lc, _, _ = make(env, tmp_path)
    run = lc.domain.run_commands = FakeRun()
    with pytest.raises(LifecycleError) as e:  # locked in prelab
        lc.start_run(SyncJobs(), 901)
    assert e.value.status == 409
    to_lab(lc)
    assert lc.latest_run(901) is None
    lc.start_run(SyncJobs(), 901)
    rec = lc.latest_run(901)
    assert run.calls == [("0901-rc-lowpass", ["python3 run_sim.py"])] and rec["ok"] and rec["files_changed"] == [{"path": "out.txt", "size": 12}]
    assert rec["steps"][0]["exit_code"] == 0 and rec["ts"]


def test_failed_run_is_recorded_and_fails_the_job(env, tmp_path):
    lc, _, _ = make(env, tmp_path)
    lc.domain.run_commands = FakeRun(ok=False)
    to_lab(lc)
    with pytest.raises(RuntimeError, match="exited 1"):  # SyncJobs lets the job error surface; the real manager marks the job failed
        lc.start_run(SyncJobs(), 901)
    assert lc.latest_run(901)["ok"] is False
    lc.domain.run_commands = FakeRun()
    lc.start_run(SyncJobs(), 901)  # the claim was released
    assert lc.latest_run(901)["ok"]


def test_debrief_shows_the_persisted_checker_result(env, tmp_path):
    lc, ad, chk = make(env, tmp_path, {"grade_prelab_explain": [GRADES]})
    to_lab(lc)
    lc.proof_submit(SyncJobs(), 901)
    assert lc.view(901)["status"] == "oral"
    d = text_oral(lc, ad)
    assert d["checker"]["kind"] == "proof" and d["checker"]["verdict"] == "pass"
    assert d["checker"]["tests"][0] == {"name": "f3db", "pass": True, "measured": 1.5, "spec": "<= 9", "unit": "Hz"}
    assert "f3db_hz" not in d["checker"]["metrics"]  # key metrics stay hidden here too
    assert lc.debrief_view(901)["checker"] == d["checker"]  # still there on a later read


def test_scores_and_usage_caps_routes(env, tmp_path, monkeypatch):
    from app.main import create_app

    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(env))
    lc, _, _ = make(env, tmp_path)
    monkeypatch.setattr("app.routes.llm.Router", lambda adapters: lc.router)
    lc.router.store.increment("openrouter", "x:free")
    lc.router.store.increment("openrouter", "y:free")
    lc.router.store.increment("claude_code", "claude-haiku-5-5")
    app = create_app()
    with TestClient(app) as c:
        u = c.get("/api/llm/usage").json()
        by = {b["backend"]: b for b in u["backends"]}
        assert by["openrouter"] == {"backend": "openrouter", "count": 2, "cap": 50, "remaining": 48}
        assert by["claude_code"]["cap"] is None and by["claude_code"]["remaining"] is None
        assert {(r["model"], r["cap"]) for r in u["usage"] if r["backend"] == "openrouter"} == {("x:free", 50), ("y:free", 50)}
        s = c.get("/api/scores")
        assert s.status_code == 200 and isinstance(s.json(), list)


def test_rescope_edit_returns_and_keeps_the_backup_path(data, tmp_path, monkeypatch):
    reply = {"action": "edit", "reason_class": "broken_env", "changes": "fix the include path", "option": None}
    lc, _, _, ws = setup(data, tmp_path, reply)
    out = lc.rescope(Jobs(), 1, "broken_env", "netlist will not load")
    assert out["backup"].startswith("builds/0001-set-an-rc-low-pass-corner/rescope-") and out["backup"].endswith("/node/workspace")
    assert (data / out["backup"] / "rc.sp").read_text() == "R1 a b 5k\n"  # the learner's pre-reset file
    st = lc.store.session(lc.store.find(1), "rescope", {})
    assert st["backup"] == out["backup"] and st["editing"] is False  # kept after the job ended
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(str(p)), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert lc.open_folder(1, "backup")["path"].replace("\\", "/").endswith("node/workspace") and opened
    lc.store.save_session(lc.store.find(1), "rescope", {"editing": False})  # no backup recorded
    with pytest.raises(LifecycleError) as e:
        lc.open_folder(1, "backup")
    assert e.value.status == 404


class _Slow(httpx.SyncByteStream):
    """Keepalive whitespace every 50 ms (what OpenRouter sends while a request is queued), then the JSON body."""

    def __iter__(self):
        for _ in range(20):
            time.sleep(0.05)
            yield b" "
        yield b'{"choices":[{"message":{"content":"hi"}}]}'


def _adapter(stream):
    return OpenRouterAdapter(api_key=lambda: "k", transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream)))


def test_openrouter_attempt_has_a_hard_deadline_even_with_keepalive_bytes():
    req = Request("tutor_chat", [{"role": "user", "content": "x"}], None, None, None, None, None)
    t0 = time.monotonic()
    with pytest.raises(RetryableError, match="exceeded"):
        _adapter(_Slow()).complete(req, "a/b:free", 0.3)
    assert time.monotonic() - t0 < 0.8  # fell through well before the body arrived
    assert _adapter(_Slow()).complete(req, "a/b:free", 5).text == "hi"  # inside the deadline it works (leading whitespace is fine)
