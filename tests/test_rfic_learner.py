"""Learner container (SPEC §4.2, §6.1): it mounts ONLY the active node's workspace/ and nothing a learner tool can reach
contains a `_hidden/` or another node. The Builder/checker container (test_rfic_container.py) keeps the full tracks mount."""

import os
from pathlib import Path

import pytest

from domains.rfic.container import ContainerError, PathError, parse_node_token
from domains.rfic.learner import CONTAINER_WORKSPACE, LearnerContainer, run_commands
from tests.helpers import DockerScript, make_dir_link

N1, N2 = "t1/archive/nodes/n1", "t1/archive/nodes/n2"
OTHER_TRACK = "t2/archive/nodes/n9"


class ScriptedLearner(DockerScript, LearnerContainer):
    def __init__(self, tracks_root, **kw):
        LearnerContainer.__init__(self, tracks_root, name="t-rfic-learner", **kw)
        DockerScript.__init__(self)


@pytest.fixture
def world(tracks_root) -> Path:
    """Two nodes of track t1 and one node of track t2, each with a workspace, hidden answers, a checker and a node.md."""
    for token in (N1, N2, OTHER_TRACK):
        node = tracks_root / token
        (node / "workspace").mkdir(parents=True, exist_ok=True)
        (node / "workspace" / "amp.sch").write_text("schematic")
        (node / "workspace" / "sub").mkdir()
        (node / "workspace" / "sub" / "deep.sp").write_text("netlist")
        (node / "_hidden" / "reference_solution").mkdir(parents=True)
        (node / "_hidden" / "prelab_key.yaml").write_text("answer: 42")
        (node / "_hidden" / "reference_solution" / "amp.sch").write_text("solution")
        (node / "node.md").write_text("---\npdk: sky130\n---\nbrief")
        (node / "checker.py").write_text("print('check')")
    (tracks_root / "t1" / "profile").mkdir(parents=True)
    (tracks_root / "t1" / "profile" / "goals.md").write_text("goals")
    return tracks_root


@pytest.fixture
def learner(world) -> ScriptedLearner:
    return ScriptedLearner(world)


def _mounts(run: tuple[str, ...]) -> list[str]:
    return [run[i + 1] for i, a in enumerate(run) if a in ("-v", "--volume", "--mount")]


# ---- what is mounted ----


def test_container_mounts_only_the_active_nodes_workspace(learner, world):
    learner.ensure_running(N1)
    (run,) = learner.run_calls()
    ws = world.resolve() / "t1" / "archive" / "nodes" / "n1" / "workspace"
    assert _mounts(run) == [f"{ws}:{CONTAINER_WORKSPACE}"]  # exactly one mount, of workspace/ alone
    joined = " ".join(run)
    assert "_hidden" not in joined and "n2" not in joined and "t2" not in joined and "/work/tracks" not in joined
    assert "org.blahajsphere.role=learner" in run
    assert f"org.blahajsphere.learner_node={N1}" in run
    assert "127.0.0.1:6080:80" in run and "127.0.0.1:8888:8888" in run  # loopback only
    assert run[-2:] == ("rfic:1", "--wait")


def test_secrets_are_random_per_container(world):
    a, b = ScriptedLearner(world), ScriptedLearner(world)
    a.ensure_running(N1), b.ensure_running(N1)
    assert a._env(a.container, "VNC_PW") != b._env(b.container, "VNC_PW")
    assert a._env(a.container, "JUPYTER_TOKEN") != b._env(b.container, "JUPYTER_TOKEN") and len(a._env(a.container, "JUPYTER_TOKEN")) >= 24


def test_switching_nodes_recreates_the_container_with_the_new_workspace(learner, world):
    learner.ensure_running(N1)
    first_token = learner._env(learner.container, "JUPYTER_TOKEN")
    learner.ensure_running(N1)  # same node: idempotent
    assert len(learner.run_calls()) == 1 and learner.bound_node() == N1

    learner.ensure_running(N2)
    assert [c[0] for c in learner.calls if c[0] in ("rm", "run")] == ["run", "rm", "run"]
    ws2 = world.resolve() / "t1" / "archive" / "nodes" / "n2" / "workspace"
    assert _mounts(learner.run_calls()[-1]) == [f"{ws2}:{CONTAINER_WORKSPACE}"]
    assert learner.bound_node() == N2
    assert learner._env(learner.container, "JUPYTER_TOKEN") != first_token

    learner.ensure_running(OTHER_TRACK)  # another track is just another node
    assert learner.bound_node() == OTHER_TRACK and _mounts(learner.run_calls()[-1])[0].endswith("n9" + os.sep + "workspace:/work/workspace")

    learner.ensure_running()  # no node: keep what is mounted
    assert learner.bound_node() == OTHER_TRACK and len(learner.run_calls()) == 3


def test_without_a_node_nothing_is_mounted(learner):
    learner.ensure_running()
    (run,) = learner.run_calls()
    assert _mounts(run) == [] and learner.bound_node() is None


def test_a_workspace_dir_that_was_replaced_gets_a_fresh_mount(learner, world):
    """Bind mounts follow the directory, not its path: a deleted-and-recreated workspace/ must not keep the stale mount."""
    import shutil

    learner.ensure_running(N1)
    ws = world / N1 / "workspace"
    shutil.rmtree(ws)
    ws.mkdir()
    if ws.stat().st_ino == 0:
        pytest.skip("this filesystem reports no inode numbers")
    learner.container["Config"]["Labels"]["org.blahajsphere.learner_workspace"] = "stale"  # what the old directory's label would be
    learner.ensure_running(N1)
    assert len(learner.run_calls()) == 2


def test_container_from_another_tracks_root_or_the_builder_role_is_replaced(learner, world):
    learner.ensure_running(N1)
    learner.container["Config"]["Labels"]["org.blahajsphere.role"] = "builder"  # e.g. a name clash with a full-mount container
    learner.ensure_running(N1)
    assert len(learner.run_calls()) == 2
    learner.container["Config"]["Labels"]["org.blahajsphere.tracks_root"] = "/elsewhere"
    learner.ensure_running()
    assert len(learner.run_calls()) == 3


def test_node_cannot_switch_under_a_running_simulation(learner):
    learner.ensure_running(N1)
    learner._busy = 1  # an exec for N1 is in flight
    with pytest.raises(ContainerError, match="still running"):
        learner.ensure_running(N2)
    assert learner.bound_node() == N1 and len(learner.run_calls()) == 1
    learner.ensure_running(N1)  # the same node is fine


def test_missing_image_gives_actionable_error(learner):
    learner.image_ok = False
    with pytest.raises(ContainerError, match="build"):
        learner.ensure_running(N1)


@pytest.mark.parametrize(
    "token",
    ["", "t1", "t1/archive/nodes", "t1/archive/nodes/n1/workspace", "../archive/nodes/n1", "t1/archive/nodes/..", "t1/archive/nodes/.",
     "t1/archive/other/n1", "t1\\archive\\nodes\\n1", "/t1/archive/nodes/n1", "t1/archive/nodes/n1/", "t1//archive/nodes/n1"],
)
def test_malformed_node_tokens_are_refused(learner, token):
    with pytest.raises(PathError):
        parse_node_token(token)
    if token:  # (an empty node means "no node": nothing gets mounted)
        with pytest.raises(PathError):
            learner.ensure_running(token)
    assert learner.run_calls() == []


def test_unknown_node_is_refused_and_nothing_is_created(learner, world):
    with pytest.raises(PathError, match="no such node"):
        learner.ensure_running("t1/archive/nodes/nope")
    assert not (world / "t1" / "archive" / "nodes" / "nope").exists() and learner.run_calls() == []


def test_a_symlinked_workspace_is_refused(learner, world):
    """workspace/ -> another node's workspace would mount that node: refuse any workspace that is not a plain directory."""
    ws = world / N1 / "workspace"
    for p in sorted(ws.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    ws.rmdir()
    make_dir_link(ws, world / N2 / "workspace")
    with pytest.raises(PathError, match="plain directory"):
        learner.ensure_running(N1)
    assert learner.run_calls() == []


def test_node_token_from_a_host_node_dir(learner, world):
    assert learner.node_token(world / N1) == N1
    for bad in (world, world / "t1", world / N1 / "workspace", world / N1 / ".." / "n2" / "x", world.parent):
        with pytest.raises(PathError):
            learner.node_token(bad)


# ---- no learner-container path reaches _hidden/ or another node ----


def _spellings(world: Path, host_file: Path, node_relative: bool = True) -> list[str]:
    """Every way a caller might name `host_file` to the learner container (`node_relative`: also relative to N1's node dir)."""
    rel_tracks = host_file.relative_to(world).as_posix()
    rel_node = host_file.relative_to(world / N1).as_posix() if host_file.is_relative_to(world / N1) else None
    out = [str(host_file), str(host_file.resolve()), rel_tracks, f"/work/tracks/{rel_tracks}", f"{CONTAINER_WORKSPACE}/../{rel_tracks}"]
    out += [f"{CONTAINER_WORKSPACE}/../../tracks/{rel_tracks}", f"../../../../{rel_tracks}", f"workspace/../../{host_file.parent.name}/{host_file.name}"]
    if rel_node and node_relative:
        out += [rel_node, f"{CONTAINER_WORKSPACE}/../{rel_node}", f"./{rel_node}"]
    return out


def test_no_learner_path_reaches_hidden_or_another_node(learner, world):
    """Exhaustive over every file under the tracks root: with N1 active, only files of N1/workspace/ translate (to
    /work/workspace/...); every other file (hidden answers, checker, node.md, other nodes/tracks, profile) is refused under
    every spelling, and nothing that does translate can leave /work/workspace."""
    files = [p for p in world.rglob("*") if p.is_file()]
    assert any("_hidden" in p.parts for p in files) and any("n2" in p.parts for p in files)
    ws1 = (world / N1 / "workspace").resolve()
    reachable = 0
    for f in files:
        inside = f.resolve().is_relative_to(ws1)
        for name in _spellings(world, f):
            try:
                c = learner.to_container(N1, name)
            except PathError:
                continue  # refused, which is always allowed
            # Accepted: must be a real path of N1/workspace and stay under the single mount point.
            assert c == CONTAINER_WORKSPACE or c.startswith(CONTAINER_WORKSPACE + "/"), (name, c)
            assert ".." not in c.split("/"), (name, c)
            target = ws1.joinpath(*c[len(CONTAINER_WORKSPACE) :].strip("/").split("/")) if c != CONTAINER_WORKSPACE else ws1
            assert target.resolve().is_relative_to(ws1), (name, c)
            assert inside, f"{name!r} reached {f}, which is outside N1/workspace"
            reachable += 1
    assert reachable >= 2  # the translation does work for the node's own files
    # N1's files and every hidden file, named by host/tracks/container paths, are unreachable with N2 active
    for f in files:
        if "_hidden" in f.parts or f.is_relative_to(world / N1):
            for name in _spellings(world, f, node_relative=False):
                with pytest.raises(PathError):
                    learner.to_container(N2, name)


@pytest.mark.parametrize(
    "bad",
    [
        "_hidden/prelab_key.yaml", "_hidden", "workspace/../_hidden/prelab_key.yaml", "../n2/workspace/amp.sch", "node.md", "checker.py",
        "/work/workspace/../_hidden/prelab_key.yaml", "/work/workspace/../../n2/workspace/amp.sch", "/work/workspace/..", "/work/workspacex/a",
        "/work/tracks/t1/archive/nodes/n2/workspace/amp.sch", "/etc/passwd", "workspace/../../../../goals.md", "/work", "..",
    ],
)
def test_to_container_refuses_everything_outside_the_workspace(learner, bad):
    with pytest.raises(PathError):
        learner.to_container(N1, bad)


def test_to_container_accepts_workspace_paths_in_every_form(learner, world):
    ws = world / N1 / "workspace"
    want = f"{CONTAINER_WORKSPACE}/sub/deep.sp"
    for form in ("workspace/sub/deep.sp", str(ws / "sub" / "deep.sp"), want, "workspace/./sub/../sub/deep.sp", "workspace\\sub\\deep.sp" if os.sep == "\\" else want):
        assert learner.to_container(N1, form) == want
    assert learner.to_container(N1, "workspace") == CONTAINER_WORKSPACE == learner.to_container(N1, CONTAINER_WORKSPACE)
    assert learner.to_container(N1, "workspace/new.sch") == f"{CONTAINER_WORKSPACE}/new.sch"  # need not exist yet


def test_symlink_inside_the_workspace_cannot_lead_out(learner, world):
    ws = world / N1 / "workspace"
    make_dir_link(ws / "to_hidden", world / N1 / "_hidden")
    make_dir_link(ws / "to_n2", world / N2 / "workspace")
    for bad in ("workspace/to_hidden/prelab_key.yaml", "workspace/to_hidden", "workspace/to_n2/amp.sch", f"{CONTAINER_WORKSPACE}/to_hidden/prelab_key.yaml"):
        with pytest.raises(PathError):
            learner.to_container(N1, bad)


# ---- GUI / Jupyter / exec ----


def test_launch_gui_starts_tool_once_in_the_workspace_and_opens_novnc_url(learner, monkeypatch):
    spawned = []
    monkeypatch.setattr(learner, "_wait_desktop", lambda timeout=60: None)
    monkeypatch.setattr(learner, "_running", lambda pattern: bool(spawned))
    monkeypatch.setattr(learner, "_spawn", lambda argv, **kw: spawned.append((argv, kw)))
    url = learner.launch_gui("xschem", node=N1, pdk="sky130A")
    assert spawned == [(["xschem"], {"workdir": "/work/workspace", "pdk": "sky130A", "env": {"DISPLAY": ":1"}})]
    pw = learner._env(learner.container, "VNC_PW")
    assert url == f"http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale&password={pw}"
    assert learner.opened == [url]
    learner.launch_gui("xschem", node=N1, open_browser=False)  # already running: not started again
    assert len(spawned) == 1 and learner.opened == [url]
    learner.launch_gui("xschem", node=N1, file="workspace/amp.sch")  # explicit file always opens
    assert spawned[1][0] == ["xschem", "/work/workspace/amp.sch"]
    assert len(learner.run_calls()) == 1  # one node throughout: one container
    with pytest.raises(ContainerError):
        learner.launch_gui("rm -rf")


def test_launch_gui_for_another_node_switches_the_mount(learner, world, monkeypatch):
    monkeypatch.setattr(learner, "_wait_desktop", lambda timeout=60: None)
    monkeypatch.setattr(learner, "_running", lambda pattern: False)
    monkeypatch.setattr(learner, "_spawn", lambda argv, **kw: None)
    learner.launch_gui("klayout", node=N1, open_browser=False)
    learner.launch_gui("klayout", node=N2, open_browser=False)
    assert [_mounts(r)[0].rsplit(":", 1)[0] for r in learner.run_calls()] == [
        str(world.resolve() / "t1" / "archive" / "nodes" / n / "workspace") for n in ("n1", "n2")
    ]


@pytest.mark.parametrize("file", ["workspace/../_hidden/prelab_key.yaml", "../n2/workspace/amp.sch", "/etc/passwd", "_hidden/prelab_key.yaml"])
def test_launch_gui_refuses_files_outside_the_workspace(learner, monkeypatch, file):
    monkeypatch.setattr(learner, "_spawn", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(ContainerError, match="outside the workspace"):
        learner.launch_gui("xschem", node=N1, file=file)
    assert learner.run_calls() == []


def test_launch_gui_file_needs_a_node(learner):
    with pytest.raises(ContainerError, match="needs the node"):
        learner.launch_gui("xschem", file="workspace/a.sch")


def test_launch_gui_without_a_node_starts_an_unmounted_desktop(learner, monkeypatch):
    spawned = []
    monkeypatch.setattr(learner, "_wait_desktop", lambda timeout=60: None)
    monkeypatch.setattr(learner, "_running", lambda pattern: False)
    monkeypatch.setattr(learner, "_spawn", lambda argv, **kw: spawned.append((argv, kw)))
    learner.launch_gui("xschem", open_browser=False)
    assert _mounts(learner.run_calls()[0]) == [] and spawned[0][1]["workdir"] is None


def test_launch_jupyter_roots_at_the_workspace_only(learner, monkeypatch):
    spawned = []
    monkeypatch.setattr(learner, "_running", lambda pattern: bool(spawned))
    monkeypatch.setattr(learner, "_http_ok", staticmethod(lambda url, timeout=3: True))
    monkeypatch.setattr(learner, "_spawn", lambda argv, **kw: spawned.append((argv, kw)))
    url = learner.launch_jupyter(N1)
    tok = learner._env(learner.container, "JUPYTER_TOKEN")
    assert url == f"http://127.0.0.1:8888/lab?token={tok}"
    argv, kw = spawned[0]
    assert "--ServerApp.root_dir=/work/workspace" in argv and "--no-browser" in argv and kw["workdir"] == "/work/workspace"
    assert f"--IdentityProvider.token={tok}" in argv  # image config disables auth unless the CLI sets it
    assert not any("/work/tracks" in a for a in argv)
    learner.launch_jupyter(N1)
    assert len(spawned) == 1  # one JupyterLab per mounted node
    with pytest.raises(ContainerError, match="needs a node"):
        learner.launch_jupyter("")


def test_jupyter_for_another_node_gets_a_new_container_and_server(learner, monkeypatch):
    spawned = []
    monkeypatch.setattr(learner, "_running", lambda pattern: learner.container is not None and learner.container.get("jupyter"))
    monkeypatch.setattr(learner, "_http_ok", staticmethod(lambda url, timeout=3: True))
    monkeypatch.setattr(learner, "_spawn", lambda argv, **kw: (spawned.append(argv), learner.container.__setitem__("jupyter", True)))
    learner.launch_jupyter(N1, open_browser=False)
    learner.launch_jupyter(N2, open_browser=False)  # the container (and with it the server) was recreated: a new one is started
    assert len(spawned) == 2 and len(learner.run_calls()) == 2


def test_exec_runs_in_the_workspace_with_the_node_mounted(learner, monkeypatch):
    seen = []
    monkeypatch.setattr("domains.rfic.container.DockerContainer.exec", lambda self, argv, **kw: seen.append((argv, kw)) or "ok")
    assert learner.exec(["ls"], node=N1, timeout=5) == "ok"
    assert seen == [(["ls"], {"workdir": "/work/workspace", "timeout": 5})]
    assert learner.bound_node() == N1 and learner._busy == 0


def test_busy_count_returns_to_zero_when_exec_fails(learner, monkeypatch):
    def boom(self, argv, **kw):
        raise ContainerError("docker died")

    monkeypatch.setattr("domains.rfic.container.DockerContainer.exec", boom)
    with pytest.raises(ContainerError):
        learner.exec(["ls"], node=N1)
    assert learner._busy == 0


# ---- Run simulation ----


def test_run_commands_run_in_the_learner_container_in_order_with_the_nodes_pdk(fake_learner, world):
    class Job:
        def __init__(self):
            self.lines = []

        def log(self, s):
            self.lines.append(s)

        def flush(self):
            pass

    fake_learner.exec_lines = ["hello\n"]
    fake_learner.exec_hook = lambda argv, node: (world / N1 / "workspace" / "out.txt").write_text("x")
    job = Job()
    rec = run_commands(fake_learner, N1, ["python a.py", "python b.py"], 60, job)
    assert rec["ok"] and [s["cmd"] for s in rec["steps"]] == ["python a.py", "python b.py"]
    assert [c["argv"] for c in fake_learner.exec_calls] == [["bash", "-c", "python a.py"], ["bash", "-c", "python b.py"]]
    assert all(c["node"] == N1 and c["pdk"] == "sky130A" and c["timeout"] == 60 for c in fake_learner.exec_calls)
    assert [f["path"] for f in rec["files_changed"]] == ["out.txt"]
    assert job.lines == ["$ python a.py", "hello\n", "$ python b.py", "hello\n"]


def test_run_commands_stops_at_the_first_failure(fake_learner, world):
    from domains.rfic.container import ExecResult

    fake_learner.next_result = ExecResult(2, "", "Traceback")
    rec = run_commands(fake_learner, N1, ["bad", "never"], 60)
    assert not rec["ok"] and len(rec["steps"]) == 1 and rec["steps"][0]["exit_code"] == 2 and "Traceback" in rec["steps"][0]["stderr_tail"]


def test_run_commands_cannot_target_a_missing_node_or_a_path(fake_learner):
    with pytest.raises(PathError):
        run_commands(fake_learner, "t1/archive/nodes/ghost", ["x"], 5)
    with pytest.raises(PathError):
        run_commands(fake_learner, "t1/archive/nodes/n1/workspace", ["x"], 5)
    assert fake_learner.exec_calls == []


def test_domain_run_commands_uses_the_learner_not_the_builder_container(world, monkeypatch):
    """Lab "Run simulation" must never exec in the full-mount container."""
    import domains.rfic.domain as dom

    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(world.parent / "data"))
    seen = {}
    monkeypatch.setattr(dom, "default_tracks_root", lambda: world)
    monkeypatch.setattr(dom, "run_learner_commands", lambda learner, node, cmds, t, job: seen.update(learner=learner, node=node) or {"ok": True})
    monkeypatch.setattr("domains.rfic.container.ContainerManager.exec", lambda *a, **k: pytest.fail("builder container used"))
    assert dom.run_commands(world / N1, ["x"], 5) == {"ok": True}
    assert isinstance(seen["learner"], LearnerContainer) and seen["node"] == N1
    with pytest.raises(ContainerError):
        dom.run_commands(world.parent, ["x"], 5)
