import io
import json

import pytest

from app.jobs.store import JobStore
from domains.rfic.container import ExecResult
from domains.rfic.tools_mcp.server import TOOL_SPECS, McpServer
from domains.rfic.tools_mcp.tools import RficTools, ToolError


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


@pytest.fixture
def tools(fake, store):
    return RficTools(fake, store, node="n1")


def rpc(server, method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "method": method, **({"id": id} if id is not None else {})}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def call(server, name, args):
    r = rpc(server, "tools/call", {"name": name, "arguments": args})["result"]
    return r["isError"], json.loads(r["content"][0]["text"])


# ---- protocol ----


def test_initialize_negotiates_version_and_advertises_tools(tools):
    s = McpServer(tools)
    r = rpc(s, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})["result"]
    assert r["protocolVersion"] == "2024-11-05" and "tools" in r["capabilities"]
    assert rpc(s, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_has_all_spec_tools_with_schemas(tools):
    names = [t["name"] for t in rpc(McpServer(tools), "tools/list")["result"]["tools"]]
    assert names == ["run_ngspice", "run_openems", "run_python", "run_checker", "parse_touchstone", "list_pdk_models", "write_file", "read_file"]
    for t in TOOL_SPECS:
        assert t["inputSchema"]["type"] == "object" and t["description"]
        assert set(t["inputSchema"]["required"]) <= set(t["inputSchema"]["properties"])
        assert hasattr(RficTools, t["method"])


def test_notifications_get_no_response_and_ping_works(tools):
    s = McpServer(tools)
    assert rpc(s, "notifications/initialized", id=None) is None
    assert rpc(s, "notifications/cancelled", {"requestId": 1}, id=None) is None
    assert rpc(s, "ping")["result"] == {}
    assert rpc(s, "nonsense/method", id=None) is None  # unknown notification: silent
    assert rpc(s, "nonsense/method")["error"]["code"] == -32601


def test_unknown_tool_is_protocol_error_but_tool_failures_are_iserror_results(tools):
    s = McpServer(tools)
    assert rpc(s, "tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602
    is_err, body = call(s, "run_ngspice", {"netlist": "workspace/missing.sp"})
    assert is_err and "not found" in body["error"]
    is_err, body = call(s, "run_ngspice", {})  # missing required arg
    assert is_err and "bad arguments" in body["error"]
    is_err, body = call(s, "read_file", {"path": "a", "extra": 1})
    assert is_err and "unexpected arguments" in body["error"]


def test_serve_loop_over_streams_handles_garbage_and_blank_lines(tools):
    s = McpServer(tools)
    inp = io.StringIO('\n{not json}\n[1]\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
    out = io.StringIO()
    s.serve(inp, out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x.get("error", {}).get("code") for x in lines[:2]] == [-32700, -32600]
    assert lines[2] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert "\n" not in out.getvalue().strip("\n").replace("\n", "")  # newline-delimited, one object per line


def test_internal_errors_do_not_kill_the_server(tools, monkeypatch):
    s = McpServer(tools, log=lambda m: None)
    monkeypatch.setattr(tools, "read_file", lambda **kw: 1 / 0)
    assert rpc(s, "tools/call", {"name": "read_file", "arguments": {"path": "x"}})["error"]["code"] == -32603
    assert rpc(s, "ping")["result"] == {}


# ---- sandbox ----


def test_pinned_node_paths_are_relative_to_node_dir_and_sandboxed(tools, nodes_root):
    r = tools.write_file("workspace/a.txt", "hi")
    assert r["written"] == "workspace/a.txt"
    assert (nodes_root / "n1" / "workspace" / "a.txt").read_text() == "hi"
    # tolerated: nodes-root-relative spelling of the same file
    assert tools.read_file("n1/workspace/a.txt")["content"] == "hi"
    for bad in ["../n2/workspace/x", "../../x", "/etc/passwd", "/work/nodes/n2/workspace/x", str(nodes_root / "n2" / "workspace" / "x"), "\\etc\\passwd"]:
        with pytest.raises(ToolError):
            tools.write_file(bad, "pwn")
    assert not (nodes_root / "n2" / "workspace" / "x").exists()
    # A relative path that merely *names* another node stays inside this one.
    assert tools.write_file("n2/workspace/x", "ok")["written"] == "n2/workspace/x"
    assert (nodes_root / "n1" / "n2" / "workspace" / "x").exists() and not (nodes_root / "n2" / "workspace" / "x").exists()


def test_unpinned_server_uses_nodes_root_relative_paths(fake, store, nodes_root):
    t = RficTools(fake, store)
    t.write_file("n2/workspace/b.txt", "x")
    assert (nodes_root / "n2" / "workspace" / "b.txt").exists()
    with pytest.raises(ToolError):
        t.write_file("../outside", "x")


def test_write_file_uses_lf_creates_dirs_and_caps_size(tools, nodes_root):
    tools.write_file("workspace/sub/dir/s.sh", "a\nb\n")
    assert (nodes_root / "n1" / "workspace" / "sub" / "dir" / "s.sh").read_bytes() == b"a\nb\n"
    with pytest.raises(ToolError, match="too large"):
        tools.write_file("workspace/big", "x" * 2_000_001)


def test_read_file_truncates_and_reports(tools, nodes_root):
    (nodes_root / "n1" / "workspace" / "big.txt").write_text("x" * 500)
    r = tools.read_file("workspace/big.txt", max_bytes=100)
    assert r["truncated"] and len(r["content"]) == 100 and r["bytes"] == 500
    with pytest.raises(ToolError, match="not found"):
        tools.read_file("workspace/none")


# ---- run_* with a fake container ----


def test_run_ngspice_translates_paths_reads_log_and_records_job(tools, fake, nodes_root, store):
    ws = nodes_root / "n1" / "workspace"
    (ws / "rc.sp").write_text("* rc\n.end\n")

    def container_writes_log(argv, workdir):
        (ws / "rc.log").write_text("Circuit: rc\nf3db = 1.59e5\n")

    fake.exec_hook = container_writes_log
    fake.exec_lines = ["Batch mode\n"]
    out = tools.run_ngspice("workspace/rc.sp", pdk="sky130A", timeout_min=2)
    call_ = fake.exec_calls[0]
    assert call_["argv"] == ["ngspice", "-b", "-o", "/work/nodes/n1/workspace/rc.log", "/work/nodes/n1/workspace/rc.sp"]
    assert (call_["workdir"], call_["pdk"], call_["timeout"]) == ("/work/nodes/n1/workspace", "sky130A", 120)
    assert out["ok"] and out["log_file"] == "workspace/rc.log" and "f3db" in out["log_tail"]
    assert out["files_changed"] == ["rc.log"]
    (job,) = store.list_jobs()
    assert (job.type, job.node_id, job.status, job.external) == ("sim.ngspice", "n1", "done", True)
    assert "Batch mode" in job.log_tail and job.log_tail[0].startswith("$ ngspice")


def test_run_ngspice_flags_errors_and_failed_job(tools, fake, nodes_root, store):
    (nodes_root / "n1" / "workspace" / "bad.sp").write_text("x")
    fake.next_result = ExecResult(1, "", "Error: unknown subckt: x1 a b foo\n")
    out = tools.run_ngspice("workspace/bad.sp")
    assert not out["ok"] and out["exit_code"] == 1 and "unknown subckt" in out["problems"][0]
    assert store.list_jobs()[0].status == "failed" and store.list_jobs()[0].error == "exit code 1"


def test_run_ngspice_rejects_bad_pdk_names_and_timeouts(tools, nodes_root):
    (nodes_root / "n1" / "workspace" / "a.sp").write_text("x")
    with pytest.raises(ToolError, match="invalid pdk"):
        tools.run_ngspice("workspace/a.sp", pdk="sky130A; rm -rf /")
    for t in (0, -1, 241):
        with pytest.raises(ToolError, match="timeout"):
            tools.run_ngspice("workspace/a.sp", timeout_min=t)


def test_timeout_marks_job_failed(tools, fake, nodes_root, store):
    (nodes_root / "n1" / "workspace" / "sim.py").write_text("x")
    fake.next_result = ExecResult(124, "", "", timed_out=True)
    out = tools.run_openems("workspace/sim.py", timeout_min=1)
    assert not out["ok"] and out["exit_code"] == 124
    assert store.list_jobs()[0].status == "failed" and "timed out" in store.list_jobs()[0].error


def test_run_openems_reports_progress_to_a_supplied_job(fake, nodes_root, store):
    (nodes_root / "n1" / "workspace" / "sim.py").write_text("x")
    t = RficTools(fake, jobs=None, node="n1")
    job = store.create("sim.openems", "n1", status="running")
    from app.jobs.store import JobHandle

    h = JobHandle(store, job.id)
    fake.exec_lines = ["Max. number of timesteps: 1000\n", "[@ 1s] Timestep: 250 || Speed: 9 MC/s\n", "[@ 2s] Timestep: 500 || Speed: 9 MC/s\n"]
    t.run_openems("workspace/sim.py", job=h)
    assert store.get(job.id).progress == 0.5
    assert fake.exec_calls[0]["env"]["PYTHONUNBUFFERED"] == "1"
    assert len(store.list_jobs()) == 1  # supplied job is used; no extra external job


def test_run_python_passes_args_and_headless_matplotlib(tools, fake, nodes_root):
    (nodes_root / "n1" / "workspace" / "p.py").write_text("x")
    tools.run_python("workspace/p.py", args=["--n", "3"])
    c = fake.exec_calls[0]
    assert c["argv"] == ["python3", "-u", "/work/nodes/n1/workspace/p.py", "--n", "3"] and c["env"]["MPLBACKEND"] == "Agg"


def test_run_checker_is_provisional_but_finds_and_parses(tools, fake, nodes_root):
    with pytest.raises(ToolError, match="no checker"):
        tools.run_checker("n1", "workspace")
    (nodes_root / "n1" / "checker.py").write_text("x")
    fake.next_result = ExecResult(0, '{"passed": true, "metrics": {"gain_db": 20}}', "")
    out = tools.run_checker(target="reference")
    assert out["result"] == {"passed": True, "metrics": {"gain_db": 20}} and out["target"] == "reference"
    assert fake.exec_calls[0]["argv"][-2:] == ["--target", "reference"]
    assert fake.exec_calls[0]["workdir"] == "/work/nodes/n1"
    with pytest.raises(ToolError):
        tools.run_checker("n2", "workspace")  # pinned to n1
    with pytest.raises(ToolError):
        tools.run_checker("n1", "everything")


def test_parse_touchstone_and_pdk_listing_wrap_container_python(tools, fake, nodes_root):
    (nodes_root / "n1" / "workspace" / "x.s2p").write_text("x")
    fake.next_result = ExecResult(0, "warn line\n" + json.dumps({"f_hz": [1.0], "S": []}), "")
    assert tools.parse_touchstone("workspace/x.s2p", max_points=10)["f_hz"] == [1.0]
    c = fake.exec_calls[0]
    assert c["argv"] == ["python3", "-", "/work/nodes/n1/workspace/x.s2p", "10"] and "skrf" in c["input"]
    fake.next_result = ExecResult(0, json.dumps({"error": "unknown pdk 'zz'", "available": ["sky130A"]}), "")
    with pytest.raises(ToolError, match="available: sky130A"):
        tools.list_pdk_models("zz")
    with pytest.raises(ToolError, match="invalid pdk"):
        tools.list_pdk_models("../..")
    fake.next_result = ExecResult(1, "", "Traceback ...")
    with pytest.raises(ToolError, match="could not parse"):
        tools.parse_touchstone("workspace/x.s2p")


def test_job_bookkeeping_failure_never_breaks_a_sim(fake, nodes_root, store, monkeypatch):
    (nodes_root / "n1" / "workspace" / "a.sp").write_text("x")
    t = RficTools(fake, store, node="n1")
    monkeypatch.setattr(store, "begin_external", lambda *a, **k: 1 / 0)
    assert t.run_ngspice("workspace/a.sp")["exit_code"] == 0


def test_mcp_config_points_at_this_python_and_repo(tmp_path, nodes_root):
    from domains.rfic.tools_mcp.config import REPO_ROOT, mcp_config, tool_names

    cfg = mcp_config(nodes_root, "n1", tmp_path)["mcpServers"]["rfic"]
    assert cfg["args"][:4] == ["-X", "utf8", "-m", "domains.rfic.tools_mcp"] and cfg["args"][cfg["args"].index("--node") + 1] == "n1"
    assert cfg["env"]["PYTHONPATH"] == str(REPO_ROOT) and (REPO_ROOT / "domains" / "rfic" / "tools_mcp" / "__main__.py").exists()
    assert "mcp__rfic__run_ngspice" in tool_names()



def test_openems_progress_uses_energy_decay_when_step_limit_is_the_default(fake, nodes_root, store):
    from app.jobs.store import JobHandle

    (nodes_root / "n1" / "workspace" / "sim.py").write_text("x")
    job = store.create("sim.openems", "n1", status="running")
    fake.exec_lines = [
        "Max. number of timesteps: 1000000000\n",
        "[@ 4s] Timestep: 1020 || Speed: 9 MC/s (4.9e-03 s/TS) || Energy: ~4.03e-15 (- 4.63dB)\n",
        "[@ 9s] Timestep: 2040 || Speed: 9 MC/s (4.9e-03 s/TS) || Energy: ~3.19e-17 (-30.00dB)\n",
        "[@ 12s] Timestep: 3060 || Speed: 9 MC/s (4.9e-03 s/TS) || Energy: ~1.0e-15 (- 10.00dB)\n",  # energy re-rises: never go backwards
    ]
    RficTools(fake, jobs=None, node="n1").run_openems("workspace/sim.py", job=JobHandle(store, job.id))
    assert store.get(job.id).progress == 0.5  # 30 dB of an assumed 60 dB, not 3060/1e9
