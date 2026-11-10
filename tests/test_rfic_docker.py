"""Real-Docker integration tests for rfic:1. Opt-in: `BLAHAJ_DOCKER_TESTS=1 uv run pytest tests/test_rfic_docker.py`.

They use their own container name and ports, so they never touch the app's long-lived container.
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
from domains.rfic.tools_mcp.tools import RficTools

pytestmark = pytest.mark.skipif(
    os.environ.get("BLAHAJ_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="set BLAHAJ_DOCKER_TESTS=1 (needs Docker + the rfic:1 image)",
)


@pytest.fixture(scope="module")
def cm(tmp_path_factory):
    root = tmp_path_factory.mktemp("dockernodes") / "nodes"
    (root / "n1" / "workspace").mkdir(parents=True)
    m = ContainerManager(root, name="blahaj-rfic-test", gui_port=6091, jupyter_port=8899, open_url=lambda u: None)
    if not m.image_present():
        pytest.skip("rfic:1 not built")
    m.ensure_running()
    yield m
    m.remove()


@pytest.fixture
def tools(cm, tmp_path):
    return RficTools(cm, JobStore(tmp_path / "jobs.db"), node="n1")


def test_bind_mount_is_shared_in_both_directions(cm, tools):
    tools.write_file("workspace/hello.txt", "from host\n")
    r = cm.exec(["cat", "/work/nodes/n1/workspace/hello.txt"])
    assert r.stdout == "from host\n"
    cm.exec(["bash", "-c", "echo from container > /work/nodes/n1/workspace/back.txt"])
    assert tools.read_file("workspace/back.txt")["content"] == "from container\n"


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


def test_gui_desktop_comes_up_and_xschem_window_exists(cm):
    url = cm.launch_gui("xschem", node="n1", open_browser=False)
    assert url.startswith("http://127.0.0.1:6091/vnc.html")
    import time

    for _ in range(30):
        r = cm.exec(["bash", "-c", "DISPLAY=:1 xwininfo -root -tree | grep -i xschem"])
        if r.returncode == 0:
            break
        time.sleep(1)
    assert r.returncode == 0, "no xschem window on the VNC display"


def test_jupyter_serves_with_token(cm):
    import urllib.error
    import urllib.request

    url = cm.launch_jupyter("n1", open_browser=False)
    assert "/lab/tree/n1/workspace?token=" in url
    api = url.split("/lab/")[0] + "/api/contents/n1?token=" + url.split("token=")[1]
    assert urllib.request.urlopen(api, timeout=10).status == 200
    base = url.split("/lab/")[0]
    for bad in (f"{base}/api/contents/n1", f"{base}/api/contents/n1?token=wrong", f"{base}/api/kernels"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad, timeout=10)
        assert e.value.code in (401, 403)  # the image's own config disables auth; we must override it


def test_stop_and_restart_preserve_mount(cm, tools):
    cm.stop()
    assert not cm.is_running()
    assert tools.read_file("workspace/hello.txt")["content"] == "from host\n"
    cm.ensure_running()
    assert cm.exec(["cat", "/work/nodes/n1/workspace/hello.txt"]).stdout == "from host\n"
    subprocess.run(["docker", "ps", "-q"], capture_output=True)


def test_vnc_requires_a_password(cm):
    """RFB security negotiation inside the container: the desktop must never offer passwordless access."""
    probe = (
        "import socket;s=socket.create_connection(('127.0.0.1',5901),5);s.recv(12);s.sendall(b'RFB 003.008\\n');"
        "import json;n=s.recv(1)[0];print(json.dumps(sorted(s.recv(n))))"
    )
    cm.launch_gui("desktop", open_browser=False)
    r = cm.exec(["python3", "-c", probe])
    offered = set(json.loads(r.stdout.strip()))
    assert 2 in offered and 1 not in offered, r  # 1 = no auth. (19 = VeNCrypt, configured as TLS + VNC password.)
