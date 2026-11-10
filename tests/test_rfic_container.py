import json
import os

import pytest

from domains.rfic.container import CONTAINER_NODES, ContainerError, ContainerManager, PathError, _Proc


# ---- path translation (SPEC §4.2) ----


def test_to_container_relative_absolute_and_container_paths(nodes_root, fake):
    want = "/work/nodes/n1/workspace/amp.sp"
    assert fake.to_container("n1/workspace/amp.sp") == want
    assert fake.to_container("n1\\workspace\\amp.sp") == want or os.sep == "/"
    assert fake.to_container(nodes_root / "n1" / "workspace" / "amp.sp") == want
    assert fake.to_container(str(nodes_root / "n1" / "workspace" / "amp.sp")) == want
    assert fake.to_container(want) == want  # idempotent
    assert fake.to_container("n1/./workspace/../workspace/amp.sp") == want
    assert fake.to_container("") == CONTAINER_NODES == fake.to_container(nodes_root)


@pytest.mark.parametrize("bad", ["../x", "n1/../../x", "/work/nodes/../etc/passwd", "/etc/passwd", "/work/nodesx/a", "/work"])
def test_to_container_rejects_escapes(fake, bad):
    with pytest.raises(PathError):
        fake.to_container(bad)


def test_to_container_rejects_other_host_dir(fake, tmp_path):
    with pytest.raises(PathError):
        fake.to_container(tmp_path / "elsewhere" / "a.sp")


def test_to_container_rejects_symlink_escape(fake, nodes_root, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    link = nodes_root / "n1" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")
    with pytest.raises(PathError):
        fake.to_container("n1/link/x")


def test_to_host_roundtrip_and_rejects(fake, nodes_root):
    c = fake.to_container("n2/workspace/a.s2p")
    assert fake.to_host(c) == nodes_root.resolve() / "n2" / "workspace" / "a.s2p"
    assert fake.to_host(CONTAINER_NODES) == nodes_root.resolve()
    for bad in ("/etc/passwd", "/work/nodes/../x", "relative/path", "/work/nodesx"):
        with pytest.raises(PathError):
            fake.to_host(bad)


def test_node_of_and_workspace_of():
    assert ContainerManager.node_of("/work/nodes/n1/workspace/a.sp") == "n1"
    assert ContainerManager.node_of("/work/nodes/n1") == "n1"
    with pytest.raises(PathError):
        ContainerManager.node_of(CONTAINER_NODES)
    assert ContainerManager.workspace_of("n1") == "/work/nodes/n1/workspace"


# ---- docker command construction (fake docker CLI) ----


class ScriptedDocker(ContainerManager):
    """Records `docker ...` argv and answers from a scripted table."""

    def __init__(self, nodes_root, **kw):
        super().__init__(nodes_root, name="t-rfic", **kw)
        self.calls: list[tuple[str, ...]] = []
        self.container: dict | None = None  # what `docker inspect` returns
        self.image_ok = True
        self.opened: list[str] = []
        self._open_url = self.opened.append

    def _docker(self, *args, input=None, timeout=60):
        self.calls.append(args)
        if args[0] == "image":
            return _Proc(0 if self.image_ok else 1)
        if args[0] == "inspect":
            return _Proc(0, json.dumps([self.container])) if self.container else _Proc(1)
        if args[0] == "run":
            env = [a.split("=", 1)[1] for a in args if a.startswith(("VNC_PW=", "JUPYTER_TOKEN="))]
            label = next(a for a in args if a.startswith("org.blahajsphere.nodes_root="))
            self.container = {
                "State": {"Running": True},
                "Config": {"Labels": {"org.blahajsphere.nodes_root": label.split("=", 1)[1]}, "Env": [f"VNC_PW={env[0]}", f"JUPYTER_TOKEN={env[1]}"]},
            }
            return _Proc(0, "cid")
        if args[0] == "rm":
            self.container = None
            return _Proc(0)
        if args[0] == "start":
            self.container["State"]["Running"] = True
            return _Proc(0)
        return _Proc(0)


def test_ensure_running_creates_with_single_mount_and_loopback_ports(nodes_root):
    d = ScriptedDocker(nodes_root)
    d.ensure_running()
    run = next(c for c in d.calls if c[0] == "run")
    assert run.count("-v") == 1
    assert f"{nodes_root.resolve()}:/work/nodes" in run
    assert "127.0.0.1:6080:80" in run and "127.0.0.1:8888:8888" in run  # never exposed beyond loopback
    assert run[-2:] == ("rfic:1", "--wait")
    n_before = len(d.calls)
    d.ensure_running()  # idempotent: no second `run`
    assert [c[0] for c in d.calls[n_before:]] == ["inspect"]


def test_ensure_running_secrets_are_random_per_container(nodes_root):
    a, b = ScriptedDocker(nodes_root), ScriptedDocker(nodes_root)
    a.ensure_running(), b.ensure_running()
    assert a._env(a.container, "VNC_PW") != b._env(b.container, "VNC_PW")
    assert len(a._env(a.container, "JUPYTER_TOKEN")) >= 24


def test_ensure_running_starts_a_stopped_container_and_recreates_on_moved_mount(nodes_root, tmp_path):
    d = ScriptedDocker(nodes_root)
    d.ensure_running()
    d.container["State"]["Running"] = False
    d.ensure_running()
    assert any(c[0] == "start" for c in d.calls) and d.container["State"]["Running"]

    moved = ScriptedDocker(tmp_path / "other_nodes")
    moved.container = d.container  # container created for the old mount
    moved.ensure_running()
    assert any(c[0] == "rm" for c in moved.calls) and any(c[0] == "run" for c in moved.calls)


def test_ensure_running_without_image_gives_actionable_error(nodes_root):
    d = ScriptedDocker(nodes_root)
    d.image_ok = False
    with pytest.raises(ContainerError, match="build"):
        d.ensure_running()


def test_exec_command_line_uses_login_shell_workdir_pdk_and_in_container_timeout(nodes_root):
    d = ScriptedDocker(nodes_root)
    cmd = d._exec_cmd([], ["ngspice", "-b", "x.sp"], "/work/nodes/n1/workspace", "sky130A", {"A": "1"}, 90)
    assert cmd[:2] == ["docker", "exec"]
    assert cmd[cmd.index("-w") + 1] == "/work/nodes/n1/workspace"
    envs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert set(envs) == {"A=1", "BLAHAJ_PDK=sky130A", "BLAHAJ_TIMEOUT=90"}
    tail = cmd[cmd.index("t-rfic") + 1 :]
    assert tail[:2] == ["bash", "-lc"] and "timeout -k 10" in tail[2] and "iic-pdk-script.sh" in tail[2]
    assert tail[-3:] == ["ngspice", "-b", "x.sp"] and tail[3] == "_"
    no_timeout = d._exec_cmd([], ["true"], None, None, None, None)
    assert "timeout" not in " ".join(no_timeout) and "-w" not in no_timeout and "-e" not in no_timeout


def test_user_data_is_never_interpolated_into_the_shell_script(nodes_root):
    d = ScriptedDocker(nodes_root)
    evil = "x; rm -rf /; $(reboot) `id`"
    cmd = d._exec_cmd([], ["ngspice", evil], None, "sky;130", None, 5)
    script = cmd[cmd.index("-lc") + 1]
    assert evil not in script and "sky;130" not in script  # passed as argv/env, not code
    assert evil in cmd


def test_launch_gui_starts_tool_once_and_opens_novnc_url(nodes_root, monkeypatch):
    d = ScriptedDocker(nodes_root)
    spawned = []
    monkeypatch.setattr(d, "_wait_desktop", lambda timeout=60: None)
    monkeypatch.setattr(d, "_running", lambda pattern: bool(spawned))
    monkeypatch.setattr(d, "_spawn", lambda argv, **kw: spawned.append((argv, kw)))
    url = d.launch_gui("xschem", node="n1", pdk="sky130A")
    assert spawned == [(["xschem"], {"workdir": "/work/nodes/n1/workspace", "pdk": "sky130A", "env": {"DISPLAY": ":1"}})]
    pw = d._env(d.container, "VNC_PW")
    assert url == f"http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale&password={pw}"
    assert d.opened == [url]
    d.launch_gui("xschem", node="n1", open_browser=False)  # already running: not started again
    assert len(spawned) == 1 and d.opened == [url]
    d.launch_gui("xschem", node="n1", file="n1/workspace/amp.sch")  # explicit file always opens
    assert spawned[1][0] == ["xschem", "/work/nodes/n1/workspace/amp.sch"]
    with pytest.raises(ContainerError):
        d.launch_gui("rm -rf")


def test_launch_jupyter_roots_at_nodes_and_returns_token_url(nodes_root, monkeypatch):
    d = ScriptedDocker(nodes_root)
    spawned = []
    monkeypatch.setattr(d, "_running", lambda pattern: bool(spawned))
    monkeypatch.setattr(d, "_http_ok", staticmethod(lambda url, timeout=3: True))
    monkeypatch.setattr(d, "_spawn", lambda argv, **kw: spawned.append(argv))
    url = d.launch_jupyter("n1")
    tok = d._env(d.container, "JUPYTER_TOKEN")
    assert url == f"http://127.0.0.1:8888/lab/tree/n1/workspace?token={tok}"
    assert "--ServerApp.root_dir=/work/nodes" in spawned[0] and "--no-browser" in spawned[0]
    assert f"--IdentityProvider.token={tok}" in spawned[0]  # image config disables auth unless the CLI sets it
    d.launch_jupyter("n2")
    assert len(spawned) == 1  # one JupyterLab shared by all nodes
