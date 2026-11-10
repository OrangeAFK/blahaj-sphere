import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from domains.rfic.container import ContainerError, ExecResult
from domains.rfic.routes import get_containers


@pytest.fixture
def client(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path))
    app = create_app()
    app.dependency_overrides[get_containers] = lambda: fake
    with TestClient(app) as c:
        yield c


def wait_job(client, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.03)
    raise AssertionError("job did not finish")


def test_domain_routes_are_mounted_only_under_rfic(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/api/rfic/container", "/api/rfic/gui", "/api/rfic/jupyter", "/api/rfic/run", "/api/rfic/image/build"} <= paths


def test_run_route_makes_a_streaming_job_that_succeeds(client, fake, nodes_root):
    (nodes_root / "n1" / "workspace" / "rc.sp").write_text("x")
    fake.exec_lines = ["Batch mode\n"]
    r = client.post("/api/rfic/run", json={"tool": "ngspice", "node": "n1", "path": "workspace/rc.sp", "pdk": "sky130A"})
    assert r.status_code == 200
    job = wait_job(client, r.json()["id"])
    assert (job["type"], job["node_id"], job["status"], job["progress"]) == ("sim.ngspice", "n1", "done", 1.0)
    assert "Batch mode" in job["log_tail"]
    assert len(client.get("/api/jobs").json()) == 1  # one job, not a second external one
    assert fake.exec_calls[0]["pdk"] == "sky130A"


def test_run_route_failed_sim_marks_job_failed(client, fake, nodes_root):
    (nodes_root / "n1" / "workspace" / "sim.py").write_text("x")
    fake.next_result = ExecResult(2, "", "Traceback")
    job = wait_job(client, client.post("/api/rfic/run", json={"tool": "openems", "node": "n1", "path": "workspace/sim.py"}).json()["id"])
    assert job["status"] == "failed" and "exit 2" in job["error"]


def test_run_route_cannot_leave_the_node(client):
    r = client.post("/api/rfic/run", json={"tool": "python", "node": "n1", "path": "../n2/workspace/x.py"})
    job = wait_job(client, r.json()["id"])  # rejected inside the job, recorded as failure
    assert job["status"] == "failed" and "outside" in job["error"]
    assert client.post("/api/rfic/run", json={"tool": "bash", "node": "n1", "path": "x"}).status_code == 422


def test_gui_and_jupyter_routes(client, fake, monkeypatch):
    monkeypatch.setattr(fake, "launch_gui", lambda tool, **kw: f"http://x/{tool}/{kw['node']}/{kw['file']}")
    monkeypatch.setattr(fake, "launch_jupyter", lambda node, **kw: f"http://j/{node}")
    assert client.post("/api/rfic/gui", json={"tool": "xschem", "node": "n1", "file": "workspace/a.sch"}).json() == {"url": "http://x/xschem/n1/n1/workspace/a.sch"}
    assert client.post("/api/rfic/jupyter", json={"node": "n1"}).json() == {"url": "http://j/n1"}
    assert client.post("/api/rfic/gui", json={"tool": "emacs"}).status_code == 422

    def boom(*a, **k):
        raise ContainerError("docker not found")

    monkeypatch.setattr(fake, "launch_gui", boom)
    r = client.post("/api/rfic/gui", json={"tool": "xschem"})
    assert r.status_code == 400 and "docker not found" in r.json()["detail"]


def test_image_build_route_runs_a_job(client, fake, monkeypatch):
    def fake_build(dockerfile, on_line=None, on_progress=None):
        on_line("#1 [1/2] FROM base\n")
        on_progress(0.5)

    monkeypatch.setattr(fake, "build_image", fake_build)
    job = wait_job(client, client.post("/api/rfic/image/build").json()["id"])
    assert job["type"] == "image_build" and job["status"] == "done"
    assert any("FROM base" in line for line in job["log_tail"])
