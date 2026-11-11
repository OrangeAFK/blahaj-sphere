"""Real-Docker integration tests for rfic:1. Opt-in: `BLAHAJ_DOCKER_TESTS=1 uv run pytest tests/test_rfic_docker.py`.

They use their own container names and ports, so they never touch the app's long-lived containers.
No LLM is involved.
"""

import json
import os
import shutil
import subprocess
import textwrap

import pytest

from app.jobs.store import JobStore
from domains.rfic.container import ContainerManager
from domains.rfic.learner import LearnerContainer
from domains.rfic.tools_mcp.tools import RficTools

pytestmark = pytest.mark.skipif(
    os.environ.get("BLAHAJ_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="set BLAHAJ_DOCKER_TESTS=1 (needs Docker + the rfic:1 image)",
)

N1, N2, N3 = "t1/archive/nodes/n1", "t1/archive/nodes/n2", "t1/archive/nodes/n3"  # n3: scratch node of the builder tests, so n1/n2 stay pristine
C1, C3 = f"/work/tracks/{N1}", f"/work/tracks/{N3}"


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("dockertracks") / "tracks"
    for n in ("n1", "n2", "n3"):
        node = root / "t1" / "archive" / "nodes" / n
        (node / "workspace").mkdir(parents=True)
        (node / "_hidden").mkdir()
        (node / "_hidden" / f"BLAHAJ_HIDDEN_{n}.txt").write_text(f"hidden answer of {n}\n")
        (node / f"BLAHAJ_NODEFILE_{n}.txt").write_text("node-level file (checker, node.md, ...)")
        (node / "workspace" / "marker.txt").write_text(f"{n}\n")
    return root


@pytest.fixture(scope="module")
def cm(root):
    m = ContainerManager(root, name="blahaj-rfic-test", open_url=lambda u: None)
    if not m.image_present():
        pytest.skip("rfic:1 not built")
    m.ensure_running()
    yield m
    m.remove()


@pytest.fixture(scope="module")
def learner(root):
    m = LearnerContainer(root, name="blahaj-rfic-test-learner", gui_port=6091, jupyter_port=8899, open_url=lambda u: None)
    if not m.image_present():
        pytest.skip("rfic:1 not built")
    yield m
    m.remove()


@pytest.fixture
def tools(cm, tmp_path):
    return RficTools(cm, JobStore(tmp_path / "jobs.db"), node=N3)


def test_bind_mount_is_shared_in_both_directions(cm, tools):
    tools.write_file("workspace/hello.txt", "from host\n")
    r = cm.exec(["cat", f"{C3}/workspace/hello.txt"])
    assert r.stdout == "from host\n"
    cm.exec(["bash", "-c", f"echo from container > {C3}/workspace/back.txt"])
    assert tools.read_file("workspace/back.txt")["content"] == "from container\n"


def test_builder_container_has_no_published_ports(cm):
    r = subprocess.run(["docker", "port", cm.name], capture_output=True, text=True)
    assert r.stdout.strip() == ""


def test_login_shell_env_has_tools_and_quiet_output(cm):
    r = cm.exec(["bash", "-c", "command -v ngspice xschem klayout openEMS qucs-s jupyter-lab; echo $PDK_ROOT"])
    assert r.returncode == 0 and r.stdout.count("/") >= 6 and "[INFO]" not in r.stdout


def test_pdk_switch(cm):
    assert cm.exec(["bash", "-c", "echo $PDK"], pdk="sky130A").stdout.strip() == "sky130A"
    assert cm.exec(["bash", "-c", "echo $PDK"], pdk="ihp-sg13g2").stdout.strip() == "ihp-sg13g2"


def test_timeout_kills_the_process_inside_the_container(cm):
    r = cm.exec(["sleep", "300"], timeout=2)
    assert r.timed_out and r.returncode == 124 and r.seconds < 20
    assert cm.exec(["pgrep", "-x", "sleep"]).returncode != 0  # really gone, not orphaned


def test_ngspice_rc_lowpass_matches_theory(tools):
    tools.write_file(
        "workspace/rc.sp",
        textwrap.dedent("""\
            * RC low-pass
            V1 in 0 DC 0 AC 1
            R1 in out 1k
            C1 out 0 1n
            .control
            ac dec 20 1k 100meg
            meas ac f3db when vdb(out)=-3.0103 fall=1
            echo "f3db_hz = $&f3db"
            .endc
            .end
            """),
    )
    out = tools.run_ngspice("workspace/rc.sp")
    assert out["ok"], out
    f3db = float(next(ln for ln in out["log_tail"].splitlines() if ln.startswith("f3db_hz")).split("=")[1])
    assert abs(f3db - 159155) / 159155 < 0.01  # 1/(2*pi*1k*1n)


def test_sky130_models_are_reachable_from_ngspice(tools):
    libs = tools.list_pdk_models("sky130A", pattern="nfet_01v8")
    lib = next(item for item in libs["libs"] if item["path"].endswith("sky130.lib.spice"))
    assert "tt" in lib["sections"] and any(d["name"] == "sky130_fd_pr__nfet_01v8" for d in libs["devices"])
    tools.write_file(
        "workspace/nfet.sp",
        f""".lib {lib['path']} tt
Vds d 0 1.8
Vgs g 0 1.0
XM1 d g 0 0 sky130_fd_pr__nfet_01v8 W=1 L=0.15
.control
op
let idd = -i(vds)
echo "id_a = $&idd"
.endc
.end
""",
    )
    out = tools.run_ngspice("workspace/nfet.sp", pdk="sky130A")
    assert out["ok"], out
    idd = float(next(ln for ln in out["log_tail"].splitlines() if ln.startswith("id_a")).split("=")[1])
    assert 1e-5 < idd < 1e-3  # sanity bound: a 1 um / 0.15 um nfet at Vgs=1.0 V, Vds=1.8 V is in the tens of uA


def test_ihp_pdk_is_listed(tools):
    r = tools.list_pdk_models("ihp-sg13g2", pattern="sg13_lv_nmos")
    assert r["n_devices"] >= 1


def test_touchstone_parse(tools):
    tools.write_file("workspace/thru.s2p", "# GHz S RI R 50\n1 0 0 1 0 1 0 0 0\n2 0 0 0.5 0.5 0.5 0.5 0 0\n")
    r = tools.parse_touchstone("workspace/thru.s2p")
    assert r["n_ports"] == 2 and r["f_hz"] == [1e9, 2e9] and r["S"][0][1][0] == [1.0, 0.0]


def test_python_runs_with_scikit_rf_and_headless_matplotlib(tools):
    tools.write_file("workspace/p.py", "import skrf, numpy, matplotlib.pyplot as plt\nplt.plot([1,2]); plt.savefig('o.png'); print('ok', skrf.__version__)\n")
    out = tools.run_python("workspace/p.py")
    assert out["ok"] and "o.png" in out["files_changed"] and out["stdout_tail"].startswith("ok")


def test_stop_and_restart_preserve_mount(cm, tools):
    cm.stop()
    assert not cm.is_running()
    assert tools.read_file("workspace/hello.txt")["content"] == "from host\n"
    cm.ensure_running()
    assert cm.exec(["cat", f"{C3}/workspace/hello.txt"]).stdout == "from host\n"


# ---- the learner container: ONLY the active node's workspace/ ----


def test_learner_container_sees_only_the_active_nodes_workspace(learner, root, cm):
    """The point of the split: nothing in the learner container (a Jupyter terminal included) reaches a `_hidden/`, another node,
    another track or any other part of data/. Checked on the real filesystem, not on the docker argv."""
    learner.ensure_running(N1)

    def sh(script: str):
        return learner.exec(["bash", "-c", script], node=N1)

    r = sh(r"find / \( -path /proc -o -path /sys \) -prune -o \( -name _hidden -o -name 'BLAHAJ_*' -o -name n2 \) -print 2>/dev/null; echo done")
    assert r.stdout.strip() == "done", r.stdout  # no hidden file, no node-level file, no other node anywhere (the find crosses mounts)
    control = cm.exec(["bash", "-c", r"find / \( -path /proc -o -path /sys \) -prune -o \( -name _hidden -o -name 'BLAHAJ_*' \) -print 2>/dev/null"])
    assert "BLAHAJ_HIDDEN_n1.txt" in control.stdout and "_hidden" in control.stdout  # the same probe does find them in the Builder container
    assert sh("ls -A /work/workspace").stdout.split() == ["marker.txt"]
    assert sh("cat /work/workspace/marker.txt").stdout == "n1\n"
    mounts = sh("grep -E ' /work' /proc/self/mountinfo | awk '{print $5}'").stdout.split()
    assert mounts == ["/work/workspace"], mounts  # the single bind mount
    assert sh("test -e /work/tracks").returncode != 0
    for escape in ("/work/workspace/../tracks", "/work/workspace/../../work/tracks/t1", "/work/workspace/../_hidden", f"{C1}/_hidden", "/work/workspace/../n2"):
        assert sh(f"ls {escape}").returncode != 0, escape
    # a symlink made inside the container to a "hidden" location dangles: the target does not exist there
    sh("ln -sfn ../_hidden /work/workspace/lnk; ln -sfn /work/tracks /work/workspace/lnk2")
    assert sh("ls /work/workspace/lnk/ /work/workspace/lnk2/").returncode != 0
    sh("rm -f /work/workspace/lnk /work/workspace/lnk2")
    # what the learner writes lands in the node's workspace on the host (and the Builder container sees it there)
    sh("echo learner > /work/workspace/out.txt")
    assert (root / N1 / "workspace" / "out.txt").read_text() == "learner\n"
    assert cm.exec(["cat", f"{C1}/workspace/out.txt"]).stdout == "learner\n"
    assert cm.exec(["cat", f"{C1}/_hidden/BLAHAJ_HIDDEN_n1.txt"]).stdout == "hidden answer of n1\n"  # the Builder container does see it
    (root / N1 / "workspace" / "out.txt").unlink()


def test_switching_nodes_swaps_the_mount(learner):
    first = learner.exec(["cat", "/work/workspace/marker.txt"], node=N1).stdout
    r = learner.exec(["bash", "-c", r"cat /work/workspace/marker.txt; find / \( -path /proc -o -path /sys \) -prune -o -name 'BLAHAJ_HIDDEN_*' -print 2>/dev/null"], node=N2)
    assert first == "n1\n" and r.stdout == "n2\n"  # n2's workspace, and no hidden file of either node
    assert learner.bound_node() == N2


def test_gui_desktop_comes_up_and_xschem_window_exists(learner):
    url = learner.launch_gui("xschem", node=N1, open_browser=False)
    assert url.startswith("http://127.0.0.1:6091/vnc.html")
    import time

    for _ in range(30):
        r = learner.exec(["bash", "-c", "DISPLAY=:1 xwininfo -root -tree | grep -i xschem"])
        if r.returncode == 0:
            break
        time.sleep(1)
    assert r.returncode == 0, "no xschem window on the VNC display"


def test_jupyter_serves_only_the_workspace_with_token(learner):
    import urllib.error
    import urllib.request

    url = learner.launch_jupyter(N1, open_browser=False)
    base, token = url.split("/lab?token=")
    listing = json.load(urllib.request.urlopen(f"{base}/api/contents?token={token}", timeout=10))
    assert sorted(c["name"] for c in listing["content"]) == ["marker.txt"]  # the root IS the workspace
    for escape in ("../tracks", "..%2F_hidden", "%2E%2E", N1.replace("/", "%2F")):
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/api/contents/{escape}?token={token}", timeout=10)
    for bad in (f"{base}/api/contents", f"{base}/api/contents?token=wrong", f"{base}/api/kernels"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad, timeout=10)
        assert e.value.code in (401, 403)  # the image's own config disables auth; we must override it


def test_vnc_requires_a_password(learner):
    """RFB security negotiation inside the container: the desktop must never offer passwordless access."""
    probe = (
        "import socket;s=socket.create_connection(('127.0.0.1',5901),5);s.recv(12);s.sendall(b'RFB 003.008\\n');"
        "import json;n=s.recv(1)[0];print(json.dumps(sorted(s.recv(n))))"
    )
    learner.launch_gui("desktop", open_browser=False)
    r = learner.exec(["python3", "-c", probe])
    offered = set(json.loads(r.stdout.strip()))
    assert 2 in offered and 1 not in offered, r  # 1 = no auth. (19 = VeNCrypt, configured as TLS + VNC password.)
