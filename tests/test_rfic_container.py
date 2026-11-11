import os

import pytest

from domains.rfic.container import CONTAINER_TRACKS, ContainerError, ContainerManager, PathError
from tests.helpers import DockerScript, make_dir_link


# ---- path translation (SPEC §4.2) ----


def test_to_container_relative_absolute_and_container_paths(tracks_root, fake):
    want = "/work/tracks/t1/archive/nodes/n1/workspace/amp.sp"
    assert fake.to_container("t1/archive/nodes/n1/workspace/amp.sp") == want
    assert fake.to_container("t1\\archive\\nodes\\n1\\workspace\\amp.sp") == want or os.sep == "/"
    assert fake.to_container(tracks_root / "t1" / "archive" / "nodes" / "n1" / "workspace" / "amp.sp") == want
    assert fake.to_container(str(tracks_root / "t1" / "archive" / "nodes" / "n1" / "workspace" / "amp.sp")) == want
    assert fake.to_container(want) == want  # idempotent
    assert fake.to_container("t1/archive/nodes/n1/./workspace/../workspace/amp.sp") == want
    assert fake.to_container("") == CONTAINER_TRACKS == fake.to_container(tracks_root)


@pytest.mark.parametrize("bad", ["../x", "n1/../../x", "/work/tracks/../etc/passwd", "/etc/passwd", "/work/tracksx/a", "/work"])
def test_to_container_rejects_escapes(fake, bad):
    with pytest.raises(PathError):
        fake.to_container(bad)


def test_to_container_rejects_other_host_dir(fake, tmp_path):
    with pytest.raises(PathError):
        fake.to_container(tmp_path / "elsewhere" / "a.sp")


def test_to_container_rejects_symlink_escape(fake, tracks_root, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    link = tracks_root / "t1" / "archive" / "nodes" / "n1" / "link"
    make_dir_link(link, outside)
    with pytest.raises(PathError):
        fake.to_container("t1/archive/nodes/n1/link/x")


def test_to_host_roundtrip_and_rejects(fake, tracks_root):
    c = fake.to_container("t1/archive/nodes/n2/workspace/a.s2p")
    assert fake.to_host(c) == tracks_root.resolve() / "t1" / "archive" / "nodes" / "n2" / "workspace" / "a.s2p"
    assert fake.to_host(CONTAINER_TRACKS) == tracks_root.resolve()
    for bad in ("/etc/passwd", "/work/tracks/../x", "relative/path", "/work/tracksx"):
        with pytest.raises(PathError):
            fake.to_host(bad)


def test_node_of_and_workspace_of():
    tok = "t1/archive/nodes/n1"
    assert ContainerManager.node_of("/work/tracks/t1/archive/nodes/n1/workspace/a.sp") == tok
    assert ContainerManager.node_of("/work/tracks/t1/archive/nodes/n1") == tok
    for bad in (CONTAINER_TRACKS, "/work/tracks/t1", "/work/tracks/t1/archive/nodes", "/work/tracks/t1/profile/goals.md"):
        with pytest.raises(PathError):
            ContainerManager.node_of(bad)
    assert ContainerManager.workspace_of(tok) == "/work/tracks/t1/archive/nodes/n1/workspace"
    assert ContainerManager.job_label(tok) == "t1/n1"
    assert ContainerManager.is_node_dir("/work/tracks/t1/archive/nodes/n1")
    assert not ContainerManager.is_node_dir("/work/tracks/t1/archive/nodes/n1/workspace")


def test_node_token_needs_a_node_dir_of_some_track(fake, tracks_root):
    assert fake.node_token(tracks_root / "t1" / "archive" / "nodes" / "n1") == "t1/archive/nodes/n1"
    for bad in (tracks_root, tracks_root / "t1", tracks_root / "t1" / "archive" / "nodes" / "n1" / "workspace"):
        with pytest.raises(PathError):
            fake.node_token(bad)


# ---- docker command construction (fake docker CLI) ----


class ScriptedDocker(DockerScript, ContainerManager):
    def __init__(self, tracks_root, **kw):
        ContainerManager.__init__(self, tracks_root, name="t-rfic", **kw)
        DockerScript.__init__(self)


def test_builder_container_has_one_full_tracks_mount_and_no_ports(tracks_root):
    d = ScriptedDocker(tracks_root)
    d.ensure_running()
    run = next(c for c in d.calls if c[0] == "run")
    assert run.count("-v") == 1
    assert f"{tracks_root.resolve()}:/work/tracks" in run
    assert "-p" not in run  # the GUI/Jupyter ports belong to the learner container
    assert "org.blahajsphere.role=builder" in run
    assert run[-2:] == ("rfic:1", "--wait")
    n_before = len(d.calls)
    d.ensure_running()  # idempotent: no second `run`
    assert [c[0] for c in d.calls[n_before:]] == ["inspect"]


def test_ensure_running_secret_is_random_per_container(tracks_root):
    a, b = ScriptedDocker(tracks_root), ScriptedDocker(tracks_root)
    a.ensure_running(), b.ensure_running()
    assert a._env(a.container, "VNC_PW") != b._env(b.container, "VNC_PW")


def test_ensure_running_starts_a_stopped_container_and_recreates_on_moved_mount(tracks_root, tmp_path):
    d = ScriptedDocker(tracks_root)
    d.ensure_running()
    d.container["State"]["Running"] = False
    d.ensure_running()
    assert any(c[0] == "start" for c in d.calls) and d.container["State"]["Running"]

    moved = ScriptedDocker(tmp_path / "other_nodes")
    moved.container = d.container  # container created for the old mount
    moved.ensure_running()
    assert any(c[0] == "rm" for c in moved.calls) and any(c[0] == "run" for c in moved.calls)


def test_ensure_running_recreates_a_container_from_before_the_split(tracks_root):
    """The old single container published 6080/8888, which the learner container now needs."""
    d = ScriptedDocker(tracks_root)
    d.container = {"State": {"Running": True}, "Config": {"Labels": {"org.blahajsphere.tracks_root": str(tracks_root.resolve())}, "Env": []}}
    d.ensure_running()
    assert [c[0] for c in d.calls if c[0] in ("rm", "run")] == ["rm", "run"]


def test_ensure_running_without_image_gives_actionable_error(tracks_root):
    d = ScriptedDocker(tracks_root)
    d.image_ok = False
    with pytest.raises(ContainerError, match="build"):
        d.ensure_running()


def test_exec_command_line_uses_login_shell_workdir_pdk_and_in_container_timeout(tracks_root):
    d = ScriptedDocker(tracks_root)
    cmd = d._exec_cmd([], ["ngspice", "-b", "x.sp"], "/work/tracks/t1/archive/nodes/n1/workspace", "sky130A", {"A": "1"}, 90)
    assert cmd[:2] == ["docker", "exec"]
    assert cmd[cmd.index("-w") + 1] == "/work/tracks/t1/archive/nodes/n1/workspace"
    envs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert set(envs) == {"A=1", "BLAHAJ_PDK=sky130A", "BLAHAJ_TIMEOUT=90"}
    tail = cmd[cmd.index("t-rfic") + 1 :]
    assert tail[:2] == ["bash", "-lc"] and "timeout -k 10" in tail[2] and "iic-pdk-script.sh" in tail[2]
    assert tail[-3:] == ["ngspice", "-b", "x.sp"] and tail[3] == "_"
    no_timeout = d._exec_cmd([], ["true"], None, None, None, None)
    assert "timeout" not in " ".join(no_timeout) and "-w" not in no_timeout and "-e" not in no_timeout


def test_user_data_is_never_interpolated_into_the_shell_script(tracks_root):
    d = ScriptedDocker(tracks_root)
    evil = "x; rm -rf /; $(reboot) `id`"
    cmd = d._exec_cmd([], ["ngspice", evil], None, "sky;130", None, 5)
    script = cmd[cmd.index("-lc") + 1]
    assert evil not in script and "sky;130" not in script  # passed as argv/env, not code
    assert evil in cmd
