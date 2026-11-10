import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import preflight
from app.domains import Domain
from app.main import create_app
from app.preflight import Check, Probe, run_preflight
from app.settings import Settings


class FakeProbe(Probe):
    def __init__(self, **kw):
        self.docker_ok = kw.get("docker_ok", True)
        self.claude = kw.get("claude", "ok")  # ok | missing | logged_out
        self.or_status = kw.get("or_status", 200)
        self.free_gb = kw.get("free_gb", 100.0)
        self.mics = kw.get("mics", ["Mic"])
        self.or_calls: list[str] = []

    def run(self, argv, timeout=20):
        if argv[:2] == ["docker", "info"]:
            return (0, "29.6.2\n") if self.docker_ok else (1, "")
        if argv[:3] == ["claude", "auth", "status"]:
            if self.claude == "logged_out":
                return 1, '{"loggedIn": false}'
            return 0, '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro", "email": "a@b.c"}'
        raise AssertionError(argv)

    def which(self, name):
        if name == "claude" and self.claude == "missing":
            return None
        return f"/bin/{name}"

    def openrouter_status(self, key):
        self.or_calls.append(key)
        return self.or_status

    def disk_free_gb(self, path):
        return self.free_gb

    def microphones(self):
        return self.mics


def by_id(checks):
    return {c.id: c for c in checks}


def test_all_green(tmp_path):
    (tmp_path / "models" / "whisper").mkdir(parents=True)
    (tmp_path / "models" / "whisper" / "m.bin").write_text("x")
    (tmp_path / "models" / "piper").mkdir(parents=True)
    (tmp_path / "models" / "piper" / "v.onnx").write_text("x")
    cs = by_id(run_preflight(None, Settings(openrouter_api_key="k"), tmp_path, FakeProbe()))
    assert {c.status for c in cs.values()} == {"ok"}
    assert cs["claude"].detail == "claude.ai (pro)" and "@" not in cs["claude"].detail  # no email leaked to the UI
    assert cs["docker"].detail == "Docker 29.6.2"


def test_docker_down_and_missing_claude_have_fix_hints(tmp_path):
    cs = by_id(run_preflight(None, Settings(), tmp_path, FakeProbe(docker_ok=False, claude="missing")))
    assert cs["docker"].status == "fail" and "Docker Desktop" in cs["docker"].fix
    assert cs["claude"].status == "fail" and "claude auth login" in cs["claude"].fix
    cs = by_id(run_preflight(None, Settings(), tmp_path, FakeProbe(claude="logged_out")))
    assert cs["claude"].status == "fail" and "not logged in" in cs["claude"].detail


@pytest.mark.parametrize(
    "key,status,expect",
    [("", 200, "warn"), ("k", 200, "ok"), ("k", 401, "fail"), ("k", 403, "fail"), ("k", None, "warn"), ("k", 500, "warn")],
)
def test_openrouter_key_states(tmp_path, key, status, expect):
    p = FakeProbe(or_status=status)
    c = by_id(run_preflight(None, Settings(openrouter_api_key=key), tmp_path, p))["openrouter"]
    assert c.status == expect and bool(c.fix) == (expect != "ok")
    assert p.or_calls == ([key] if key else [])  # no network call without a key


def test_disk_rule_is_image_plus_10gb(tmp_path):
    d = Domain(name="t", image="x:1", image_size_gb=17.1)
    ok = by_id(run_preflight(d, Settings(), tmp_path, FakeProbe(free_gb=27.2)))["disk"]
    bad = by_id(run_preflight(d, Settings(), tmp_path, FakeProbe(free_gb=27.0)))["disk"]
    assert ok.status == "ok" and bad.status == "fail" and "27 GB" in bad.detail and bad.fix


def test_voice_models_and_microphone_are_warnings_not_failures(tmp_path):
    (tmp_path / "models" / "whisper").mkdir(parents=True)  # present but empty
    cs = by_id(run_preflight(None, Settings(), tmp_path, FakeProbe(mics=[])))
    assert cs["models"].status == "warn" and "whisper" in cs["models"].detail and "piper" in cs["models"].detail
    assert cs["microphone"].status == "warn"
    assert by_id(run_preflight(None, Settings(), tmp_path, FakeProbe(mics=None)))["microphone"].status == "warn"


def test_domain_checks_are_included_in_order(tmp_path):
    d = Domain(name="t", image="x:1", image_size_gb=1, checks=lambda: [Check(id="image", label="img", status="fail", fix_action="build_image")])
    ids = [c.id for c in run_preflight(d, Settings(), tmp_path, FakeProbe())]
    assert ids[:2] == ["docker", "image"] and ids[-1] == "microphone"


def test_real_probe_handles_missing_binaries(tmp_path):
    p = Probe()
    assert p.run(["definitely-not-a-real-binary-xyz"]) == (127, "")
    assert p.disk_free_gb(tmp_path / "does" / "not" / "exist") > 0


# ---- routes ----


def test_preflight_route_and_fix_action(tmp_path, monkeypatch):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path))
    started = []
    app = create_app()
    app.state.domain = Domain(
        name="t", image="x:1", image_size_gb=1,
        checks=lambda: [Check(id="image", label="img", status="fail", fix_action="build_image")],
        fixes={"build_image": lambda jobs: jobs.submit("image_build", lambda h: started.append(1))},
    )  # fmt: skip
    monkeypatch.setattr(preflight, "Probe", FakeProbe)
    monkeypatch.setattr("app.routes.preflight.run_preflight", lambda d, s, data: run_preflight(d, s, data, FakeProbe()))
    with TestClient(app) as c:
        body = c.get("/api/preflight").json()
        assert [x["id"] for x in body][:2] == ["docker", "image"] and body[1]["fix_action"] == "build_image"
        r = c.post("/api/preflight/fix/build_image")
        assert r.status_code == 200 and r.json()["type"] == "image_build"
        assert c.post("/api/preflight/fix/nope").status_code == 404
        import time

        for _ in range(100):
            if c.get(f"/api/jobs/{r.json()['id']}").json()["status"] == "done":
                break
            time.sleep(0.05)
        assert started == [1]


def test_fix_without_running_worker_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path))
    app = create_app()
    app.state.domain = Domain(name="t", image="x", image_size_gb=1, fixes={"f": lambda j: None})
    assert TestClient(app).post("/api/preflight/fix/f").status_code == 503  # no lifespan -> worker not started


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PATHEXT shims")
def test_probe_runs_cmd_shims_found_via_path(tmp_path, monkeypatch):
    """Regression: `claude` is an npm `.CMD` shim; a bare-name subprocess call returned 127 and preflight said 'not logged in'."""
    (tmp_path / "fakecli.cmd").write_text("@echo shim-ok\r\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    code, out = Probe().run(["fakecli", "arg"])
    assert code == 0 and out.strip() == "shim-ok"
