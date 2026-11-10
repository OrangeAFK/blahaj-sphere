import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.jobs.manager import JobManager
from app.jobs.store import LOG_TAIL_LINES, JobHandle, JobStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "jobs.db")


def test_store_lifecycle(store):
    j = store.create("sim.ngspice", "n1")
    assert (j.status, j.progress, j.node_id, j.external) == ("queued", 0.0, "n1", False)
    store.set_status(j.id, "running")
    store.update(j.id, progress=0.4, log_tail="a\nb")
    got = store.get(j.id)
    assert (got.status, got.progress, got.log_tail) == ("running", 0.4, ["a", "b"])
    assert got.started_ns is not None and got.updated_ns >= j.updated_ns
    store.set_status(j.id, "done")
    done = store.get(j.id)
    assert (done.status, done.progress) == ("done", 1.0) and done.finished_ns
    assert store.get("nope") is None


def test_store_failure_records_error_and_progress_is_clamped(store):
    j = store.create("x")
    store.update(j.id, progress=7)
    assert store.get(j.id).progress == 1.0
    store.set_status(j.id, "failed", error="boom")
    assert (store.get(j.id).status, store.get(j.id).error) == ("failed", "boom")


def test_list_active_only_and_order(store):
    a, b, c = store.create("a"), store.create("b"), store.create("c")
    store.set_status(b.id, "done")
    assert {j.id for j in store.list_jobs(active_only=True)} == {a.id, c.id}
    assert [j.id for j in store.list_jobs()] == [c.id, b.id, a.id]  # newest first


def test_handle_keeps_only_log_tail_and_truncates_lines(store):
    j = store.create("x", status="running")
    h = JobHandle(store, j.id)
    for i in range(LOG_TAIL_LINES + 25):
        h.log(f"line {i}\n")
    h.log("y" * 5000)
    h.flush()
    tail = store.get(j.id).log_tail
    assert len(tail) == LOG_TAIL_LINES
    assert tail[-2] == f"line {LOG_TAIL_LINES + 24}" and len(tail[-1]) <= 400


def test_handle_done_and_fail_flush_pending_log(store):
    j1, j2 = store.create("x", status="running"), store.create("y", status="running")
    h1, h2 = JobHandle(store, j1.id), JobHandle(store, j2.id)
    h1.log("one")
    h1.done()
    h2.log("two")
    h2.fail("bad")
    assert store.get(j1.id).log_tail == ["one"] and store.get(j1.id).status == "done"
    assert store.get(j2.id).log_tail == ["two"] and store.get(j2.id).error == "bad"


def test_fail_interrupted_spares_live_external_jobs(store):
    app_job = store.create("image_build", status="running")
    queued = store.create("sim.ngspice")
    live_ext = store.begin_external("sim.ngspice", "n1")
    stale_ext = store.begin_external("sim.openems", "n1")
    assert store.fail_interrupted(stale_external_s=3600) == 2
    assert store.get(app_job.id).status == "failed" and store.get(queued.id).status == "failed"
    assert store.get(live_ext.id).status == "running" and store.get(stale_ext.id).status == "running"
    time.sleep(0.05)
    assert store.fail_interrupted(stale_external_s=0.01) == 2  # both quiet externals are now stale
    assert store.get(live_ext.id).status == "failed"


# ---- manager ----


async def _wait_status(store, job_id, status, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if store.get(job_id).status == status:
            return store.get(job_id)
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck at {store.get(job_id).status}, wanted {status}")


@pytest.mark.anyio
async def test_manager_runs_sync_and_async_jobs_and_survives_failures(store):
    m = JobManager(store, concurrency=2)
    await m.start()
    try:

        def sync_job(h):
            h.log("working")
            h.progress(0.5)

        async def async_job(h):
            await asyncio.sleep(0.01)
            h.log("async ok")

        def bad_job(h):
            h.log("about to fail")
            raise ValueError("kaboom")

        j1, j2, j3 = m.submit("a", sync_job, "n1"), m.submit("b", async_job), m.submit("c", bad_job)
        d1, d2 = await _wait_status(store, j1.id, "done"), await _wait_status(store, j2.id, "done")
        d3 = await _wait_status(store, j3.id, "failed")
        assert d1.log_tail == ["working"] and d1.progress == 1.0 and d1.node_id == "n1"
        assert d2.log_tail == ["async ok"]
        assert d3.error == "ValueError: kaboom" and "ValueError: kaboom" in d3.log_tail
        j4 = m.submit("d", sync_job)  # the worker pool is still alive after a failure
        await _wait_status(store, j4.id, "done")
    finally:
        await m.stop()


@pytest.mark.anyio
async def test_manager_start_fails_jobs_orphaned_by_a_restart(store):
    orphan = store.create("old", status="running")
    m = JobManager(store)
    await m.start()
    try:
        assert store.get(orphan.id).status == "failed"
    finally:
        await m.stop()


def test_submit_before_start_raises(store):
    with pytest.raises(RuntimeError):
        JobManager(store).submit("x", lambda h: None)


@pytest.mark.anyio
async def test_events_stream_snapshot_changes_and_other_process_writes(store, tmp_path):
    old = store.create("old")
    store.set_status(old.id, "done")
    m = JobManager(store)
    await m.start()
    try:
        got: list = []

        async def consume():
            async for job in m.events(poll_s=0.02, keepalive_s=0.3):
                got.append(job)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.2)
        # Another process (the MCP server) writes to the same DB file.
        other = JobStore(tmp_path / "jobs.db")
        h = other.begin_external("sim.ngspice", "n1")
        h.log("from mcp")
        h.progress(0.3)
        h.done()
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await m.stop()

    real = [j for j in got if j is not None]
    assert real[0].id == old.id  # snapshot first
    ext = [j for j in real if j.external]
    assert ext, "external job never streamed"
    assert ext[-1].status == "done" and ext[-1].log_tail == ["from mcp"]
    assert None in got  # keepalive ticks while idle
    # No duplicates of an unchanged row.
    seen = [(j.id, j.updated_ns) for j in real]
    assert len(seen) == len(set(seen))


# ---- SSE over a real socket (TestClient buffers infinite streams) ----


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path))
    from app.main import create_app

    app = create_app()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def test_sse_endpoint_streams_job_events(live_server):
    app, base = live_server
    events: list[dict] = []
    with httpx.stream("GET", f"{base}/api/jobs/stream", timeout=10) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        # Submit after the stream is open: the manager lives in the server's loop.
        threading.Timer(0.3, lambda: app.state.job_manager.submit("demo", _demo_job, "n1")).start()
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "job":
                events.append(json.loads(line[5:]))
                if events[-1]["status"] == "done":
                    break
    assert [e["status"] for e in events][0] in ("queued", "running")
    assert events[-1]["status"] == "done" and events[-1]["type"] == "demo"
    assert any(e["progress"] == 0.5 for e in events)
    assert any("step 1" in e["log_tail"] for e in events)


def _demo_job(h):
    h.log("step 1")
    h.progress(0.5)
    time.sleep(0.6)


def test_jobs_rest_endpoints(live_server):
    app, base = live_server
    j = app.state.job_manager.submit("demo", _demo_job, "n9")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = httpx.get(f"{base}/api/jobs/{j.id}").json()
        if body["status"] == "done":
            break
        time.sleep(0.05)
    assert body["node_id"] == "n9" and body["status"] == "done"
    assert j.id in [x["id"] for x in httpx.get(f"{base}/api/jobs").json()]
    assert httpx.get(f"{base}/api/jobs?active=true").json() == []
    assert httpx.get(f"{base}/api/jobs/missing").status_code == 404
