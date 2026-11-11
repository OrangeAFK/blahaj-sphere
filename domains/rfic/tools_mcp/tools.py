"""Sim tools exposed over MCP (SPEC §6.2). Plain Python; `server.py` only adapts them to the protocol.

Paths: tools accept host-relative node paths. When the server is pinned to one node (`--node`),
relative paths are relative to that node's directory (the agent's cwd) and nothing outside it is
reachable; otherwise they are relative to the nodes root. All translation goes through
`ContainerManager.to_container`/`to_host`.
"""

import json
import os
import posixpath
import re
import shutil
from pathlib import Path
from typing import Callable

from app.builder.nodefile import NodeFileError, read_node_md
from app.jobs.store import JobHandle, JobStore
from domains.rfic.container import CONTAINER_NODES, ContainerError, ContainerManager, ExecResult, PathError

TAIL_CHARS = 8000
READ_MAX = 200_000
WRITE_MAX = 2_000_000
SNAPSHOT_MAX_FILES = 5000
PDK_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_OPENEMS_MAX = re.compile(r"Max\. number of timesteps:\s*(\d+)")
_OPENEMS_STEP = re.compile(r"Timestep:\s*(\d+)")
_OPENEMS_ENERGY_DB = re.compile(r"Energy:.*\(\s*-\s*([\d.]+)\s*dB\)")
# openEMS stops on an energy-decay criterion (-50 dB default; tutorials use -60). Its step limit defaults
# to 1e9, so step/limit says nothing; below this limit the user set a real one and steps are meaningful.
OPENEMS_REAL_STEP_LIMIT = 10_000_000
OPENEMS_ASSUMED_END_DB = 60.0


CHECKER_TARGETS = {"workspace": "workspace", "reference": "_hidden/reference_solution"}  # SPEC §4.1
# Reference metrics are the prelab answers, so that result must stay under _hidden/ (DECISIONS M2.5).
CHECKER_OUT = {"workspace": ".checker/workspace_result.json", "reference": "_hidden/.checker/reference_result.json"}
# node.md `pdk` (SPEC §5.1) -> PDK name in the container; `none` = no PDK env.
PDK_ALIASES = {"sky130": "sky130A", "ihp-sg13g2": "ihp-sg13g2", "none": None}


class ToolError(Exception):
    """Reported to the agent as an `isError` tool result, never as a protocol error."""


def node_pdk(node_host: Path) -> str | None:
    """Container PDK for a node, from its node.md frontmatter (None when absent or `none`)."""
    md = node_host / "node.md"
    if not md.is_file():
        return None
    try:
        pdk = read_node_md(md)[0].get("pdk")
    except NodeFileError as e:
        raise ToolError(str(e)) from None
    if pdk is None:
        return None
    if str(pdk) not in PDK_ALIASES:
        raise ToolError(f"node.md pdk must be one of {sorted(PDK_ALIASES)}, got {pdk!r}")
    return PDK_ALIASES[str(pdk)]


def _tail(text: str, n: int = TAIL_CHARS) -> str:
    return text if len(text) <= n else "[...truncated...]\n" + text[-n:]


_PDK_SCAN = r'''
import json, os, re, sys
pdk, pattern, limit = sys.argv[1], sys.argv[2].lower(), int(sys.argv[3])
root = os.path.join(os.environ["PDK_ROOT"], pdk)
if not os.path.isdir(root):
    avail = sorted(d for d in os.listdir(os.environ["PDK_ROOT"]) if d != "ciel")
    print(json.dumps({"error": f"unknown pdk {pdk!r}", "available": avail})); sys.exit(0)
scan_dirs = [os.path.join(root, "libs.tech", "ngspice")]
ref = os.path.join(root, "libs.ref")
if os.path.isdir(ref):
    for lib in sorted(os.listdir(ref)):
        d = os.path.join(ref, lib, "spice")
        if os.path.isdir(d):
            scan_dirs.append(d)
sect = re.compile(r"^\s*\.lib\s+(\S+)\s*$", re.I)
sub = re.compile(r"^\s*\.subckt\s+(\S+)", re.I)
mod = re.compile(r"^\s*\.model\s+(\S+)\s+(\S+)", re.I)
main_libs, devices, seen = [], [], set()
for base in scan_dirs:
    for dp, _, files in os.walk(base):
        for fn in sorted(files):
            if not fn.endswith((".spice", ".lib", ".mod", ".cir")):
                continue
            path = os.path.join(dp, fn)
            sections = []
            try:
                with open(path, errors="replace") as f:
                    for line in f:
                        if m := sect.match(line):
                            sections.append(m.group(1))
                        elif m := sub.match(line):
                            key = ("subckt", m.group(1))
                            if key not in seen:
                                seen.add(key); devices.append({"name": m.group(1), "kind": "subckt", "file": path})
                        elif m := mod.match(line):
                            key = ("model", m.group(1))
                            if key not in seen:
                                seen.add(key); devices.append({"name": m.group(1), "kind": "model " + m.group(2), "file": path})
            except OSError:
                continue
            if sections:
                main_libs.append({"path": path, "sections": sections[:40]})
if pattern:
    devices = [d for d in devices if pattern in d["name"].lower()]
print(json.dumps({"pdk": pdk, "pdk_root": root, "usage": ".lib <path> <section>", "libs": main_libs[:60],
                  "n_devices": len(devices), "devices": devices[:limit], "truncated": len(devices) > limit}))
'''

_TOUCHSTONE = r'''
import json, sys
import numpy as np, skrf
nw = skrf.Network(sys.argv[1])
n, k = len(nw.f), max(2, int(sys.argv[2]))
idx = np.unique(np.linspace(0, n - 1, min(n, k)).astype(int))
cx = lambda z: [float(np.real(z)), float(np.imag(z))]
print(json.dumps({
    "n_ports": int(nw.nports), "n_freq": int(n), "returned": int(len(idx)),
    "z0_ohm": [cx(z) for z in np.atleast_1d(nw.z0[0])],
    "f_hz": [float(x) for x in nw.f[idx]],
    "S": [[[cx(c) for c in row] for row in m] for m in nw.s[idx]],
    "S_layout": "S[freq][row][col] = [re, im]",
}))
'''


class RficTools:
    def __init__(self, containers: ContainerManager, jobs: JobStore | None = None, node: str | None = None):
        self.containers = containers
        self.jobs = jobs
        self.node = node

    # ---- path handling ----

    def _cpath(self, path: str) -> str:
        """Agent-supplied path -> container path, enforcing the node sandbox."""
        if not path or "\0" in path:
            raise ToolError("empty or invalid path")
        raw = path
        if self.node:
            if raw[:1] in ("/", "\\") and not raw.startswith(CONTAINER_NODES):
                raise ToolError(f"absolute path outside the node directory: {path}")  # (Windows doesn't call "/x" absolute)
            is_abs = raw.startswith(CONTAINER_NODES) or Path(raw).is_absolute()
            if not is_abs:
                root = self.containers.nodes_root
                # Tolerate nodes-root-relative paths ("<node>/workspace/x") when unambiguous.
                if raw.replace("\\", "/").split("/")[0] == self.node and not (root / self.node / self.node).exists():
                    pass
                else:
                    raw = f"{self.node}/{raw}"
        try:
            c = self.containers.to_container(raw)
            node = self.containers.node_of(c)
        except PathError as e:
            raise ToolError(str(e)) from None
        if self.node and node != self.node:
            raise ToolError(f"path is outside this node ({self.node}): {path}")
        return c

    def _existing_file(self, path: str, what: str) -> tuple[str, Path]:
        c = self._cpath(path)
        host = self.containers.to_host(c)
        if not host.is_file():
            raise ToolError(f"{what} not found: {path}")
        return c, host

    def _rel_to_node(self, host: Path) -> str:
        c = self.containers.to_container(host)
        node = self.containers.node_of(c)
        return posixpath.relpath(c, f"{CONTAINER_NODES}/{node}")

    def _snapshot(self, workspace: Path) -> dict[str, tuple[int, int]]:
        snap: dict[str, tuple[int, int]] = {}
        for dp, _, files in os.walk(workspace):
            for fn in files:
                p = Path(dp) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                snap[p.relative_to(workspace).as_posix()] = (st.st_mtime_ns, st.st_size)
                if len(snap) >= SNAPSHOT_MAX_FILES:
                    return snap
        return snap

    def _run(
        self,
        type_: str,
        node: str,
        argv: list[str],
        *,
        timeout_s: float,
        pdk: str | None = None,
        env: dict[str, str] | None = None,
        job: JobHandle | None = None,
        on_line: Callable[[str], None] | None = None,
        workdir: str | None = None,
    ) -> tuple[ExecResult, list[str]]:
        workspace = self.containers.workspace_of(node)
        host_ws = self.containers.to_host(workspace)
        before = self._snapshot(host_ws) if host_ws.is_dir() else {}
        # With no job from the app's worker, record an external one in the shared DB (best effort).
        own: JobHandle | None = None
        if job is None and self.jobs is not None:
            try:
                own = self.jobs.begin_external(type_, node)
            except Exception:  # bookkeeping must never break a simulation
                own = None
        h = job or own

        def line(s: str) -> None:
            if h is not None:
                h.log(s)
            if on_line:
                on_line(s)

        if h is not None:
            h.log(f"$ {' '.join(argv)}")
        try:
            res = self.containers.exec(argv, workdir=workdir or workspace, pdk=pdk, env=env, timeout=timeout_s, on_line=line)
        except BaseException as e:
            if own is not None:
                own.fail(f"{e.__class__.__name__}: {e}")
            raise
        if h is not None:
            h.flush()
        if own is not None:
            if res.timed_out:
                own.fail(f"timed out after {int(timeout_s)} s")
            elif res.returncode != 0:
                own.fail(f"exit code {res.returncode}")
            else:
                own.done()
        after = self._snapshot(host_ws) if host_ws.is_dir() else {}
        changed = sorted(k for k, v in after.items() if before.get(k) != v)
        return res, changed

    def _workdir_for(self, c: str) -> str | None:
        """Files in the reference solution run there, so their relative outputs never land in the
        learner's workspace; everything else runs in workspace/ (SPEC §4.2)."""
        ref = f"{CONTAINER_NODES}/{self.containers.node_of(c)}/{CHECKER_TARGETS['reference']}"
        return ref if c.startswith(ref + "/") else None

    @staticmethod
    def _timeout_s(timeout_min: float | None, default_min: float) -> float:
        m = default_min if timeout_min is None else timeout_min
        if not 0 < m <= 240:
            raise ToolError("timeout must be between 0 and 240 minutes")
        return m * 60

    # ---- tools ----

    def run_ngspice(self, netlist: str, pdk: str | None = None, timeout_min: float | None = None, job: JobHandle | None = None) -> dict:
        """Batch-run ngspice (`ngspice -b -o <netlist>.log`) in the node workspace."""
        c, host = self._existing_file(netlist, "netlist")
        node = self.containers.node_of(c)
        pdk = self._check_pdk(pdk)
        log_c = posixpath.splitext(c)[0] + ".log"
        res, changed = self._run(
            "sim.ngspice", node, ["ngspice", "-b", "-o", log_c, c], timeout_s=self._timeout_s(timeout_min, 10), pdk=pdk, job=job,
            workdir=self._workdir_for(c),
        )
        log_host = self.containers.to_host(log_c)
        log_text = log_host.read_text(encoding="utf-8", errors="replace") if log_host.is_file() else ""
        problems = [ln.strip() for ln in (log_text + "\n" + res.stderr).splitlines() if re.search(r"\b(Error|error:|fatal)\b", ln)]
        return {
            "ok": res.returncode == 0 and not problems,
            "exit_code": res.returncode,
            "seconds": res.seconds,
            "problems": problems[:20],
            "log_file": self._rel_to_node(log_host),
            "log_tail": _tail(log_text),
            "stdout_tail": _tail(res.stdout, 2000),
            "stderr_tail": _tail(res.stderr, 2000),
            "files_changed": changed[:100],
        }

    def run_openems(self, script: str, timeout_min: float | None = None, job: JobHandle | None = None) -> dict:
        """Run an openEMS Python script (`python3 -u script.py`) in the node workspace."""
        c, _ = self._existing_file(script, "script")
        node = self.containers.node_of(c)
        state = {"max": 0, "best": 0.0}

        def on_line(s: str) -> None:
            if job is None:
                return
            if m := _OPENEMS_MAX.search(s):
                state["max"] = int(m.group(1))
                return
            frac = None
            if (m := _OPENEMS_STEP.search(s)) and 0 < state["max"] < OPENEMS_REAL_STEP_LIMIT:
                frac = int(m.group(1)) / state["max"]
            elif m := _OPENEMS_ENERGY_DB.search(s):
                frac = float(m.group(1)) / OPENEMS_ASSUMED_END_DB  # dB below the energy peak -> end criterion
            if frac is not None and frac > state["best"]:  # monotone: energy rings up before it decays
                state["best"] = min(frac, 0.99)
                job.progress(state["best"])

        res, changed = self._run(
            "sim.openems", node, ["python3", "-u", c], timeout_s=self._timeout_s(timeout_min, 15),
            env={"PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"}, job=job, on_line=on_line, workdir=self._workdir_for(c),
        )  # fmt: skip
        return {
            "ok": res.returncode == 0,
            "exit_code": res.returncode,
            "seconds": res.seconds,
            "stdout_tail": _tail(res.stdout),
            "stderr_tail": _tail(res.stderr, 3000),
            "files_changed": changed[:100],
        }

    def run_python(self, path: str, args: list[str] | None = None, timeout_min: float | None = None, job: JobHandle | None = None) -> dict:
        """Run a Python script (`python3 path args...`) in the node workspace (matplotlib is headless)."""
        c, _ = self._existing_file(path, "script")
        node = self.containers.node_of(c)
        res, changed = self._run(
            "sim.python", node, ["python3", "-u", c, *(args or [])], timeout_s=self._timeout_s(timeout_min, 10),
            env={"PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"}, job=job, workdir=self._workdir_for(c),
        )  # fmt: skip
        return {
            "ok": res.returncode == 0,
            "exit_code": res.returncode,
            "seconds": res.seconds,
            "stdout_tail": _tail(res.stdout),
            "stderr_tail": _tail(res.stderr, 3000),
            "files_changed": changed[:100],
        }

    def run_checker(self, node: str | None = None, target: str = "workspace", timeout_min: float | None = None, job: JobHandle | None = None) -> dict:
        """Run the node's checker (SPEC §5.2) against `workspace` (the learner's work) or `reference`
        (`_hidden/reference_solution`). Output goes to CHECKER_OUT; the gate-owned
        `_hidden/ref_result.json` is written only by the release gate."""
        if target not in CHECKER_TARGETS:
            raise ToolError("target must be 'workspace' or 'reference'")
        node = node or self.node
        if not node:
            raise ToolError("node is required")
        if self.node and node != self.node:
            raise ToolError(f"this server is pinned to node {self.node}")
        out = self.run_checker_at(node, CHECKER_TARGETS[target], CHECKER_OUT[target], self._timeout_s(timeout_min, 30), job=job)
        return {"ok": out["verdict"] == "pass", "target": target, **out}

    def run_checker_at(self, node: str, work_rel: str, out_rel: str, timeout_s: float, job: JobHandle | None = None) -> dict:
        """`python3 checker.py --work <work> --out <out>` from the node dir with the node's PDK env.

        Paths are relative to the node dir. Exit 0/1/2 -> verdict pass/fail/error (§5.2); a timeout
        or a missing/unparseable result file is also `error`."""
        node_c = self._cpath(node)
        if posixpath.dirname(node_c) != CONTAINER_NODES:
            raise ToolError(f"not a node directory: {node}")
        node_host = self.containers.to_host(node_c)
        if not (node_host / "checker.py").is_file():
            raise ToolError(f"no checker.py in {node}")
        work_c, out_c = (posixpath.normpath(f"{node_c}/{r}") for r in (work_rel, out_rel))
        if not all(c.startswith(node_c + "/") for c in (work_c, out_c)):
            raise ToolError("work and out paths must stay inside the node directory")
        work_host, out_host = self.containers.to_host(work_c), self.containers.to_host(out_c)
        if not work_host.is_dir():
            raise ToolError(f"work dir not found: {work_rel}")
        out_host.parent.mkdir(parents=True, exist_ok=True)
        out_host.unlink(missing_ok=True)
        pdk = node_pdk(node_host)
        res, _ = self._run(
            "checker", self.containers.node_of(node_c),
            ["python3", "-u", f"{node_c}/checker.py", "--work", work_c, "--out", out_c],
            timeout_s=timeout_s, pdk=pdk, env={"PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"}, job=job, workdir=node_c,
        )  # fmt: skip
        result, problem = None, None
        if out_host.is_file():
            try:
                result = json.loads(out_host.read_text(encoding="utf-8"))
            except ValueError as e:
                problem = f"result file is not valid JSON: {e}"
        elif res.returncode in (0, 1):
            problem = f"checker exited {res.returncode} but wrote no result file ({out_rel})"
        if res.timed_out:
            verdict = "error"
            problem = f"checker timed out after {int(timeout_s)} s"
        elif res.returncode in (0, 1) and problem is None:
            verdict = "pass" if res.returncode == 0 else "fail"
        else:
            verdict = "error"
        return {
            "verdict": verdict,
            "exit_code": res.returncode,
            "seconds": res.seconds,
            "pdk": pdk,
            "result": result,
            "problem": problem,
            "stdout_tail": _tail(res.stdout, 3000),
            "stderr_tail": _tail(res.stderr, 3000),
        }

    def run_commands(self, node: str, commands: list[str], timeout_s: float, job: JobHandle | None = None) -> dict:
        """Lab "Run simulation": the node's `run` commands (node.md), in order, in the live workspace with the node's PDK env.

        Stops at the first non-zero exit. Output streams to the job; `files_changed` lists what the whole run wrote."""
        node_c = self._cpath(node)
        if posixpath.dirname(node_c) != CONTAINER_NODES:
            raise ToolError(f"not a node directory: {node}")
        name = self.containers.node_of(node_c)
        pdk = node_pdk(self.containers.to_host(node_c))
        host_ws = self.containers.to_host(self.containers.workspace_of(name))
        before = self._snapshot(host_ws) if host_ws.is_dir() else {}
        steps: list[dict] = []
        for cmd in commands:
            res, _ = self._run(
                "sim.run", name, ["bash", "-c", cmd], timeout_s=timeout_s, pdk=pdk, job=job,
                env={"PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"},
            )  # fmt: skip
            steps.append({"cmd": cmd, "exit_code": res.returncode, "seconds": round(res.seconds, 1), "timed_out": res.timed_out,
                          "stderr_tail": _tail(res.stderr, 1500) if res.returncode else ""})  # fmt: skip
            if res.returncode != 0:
                break
        after = self._snapshot(host_ws) if host_ws.is_dir() else {}
        changed = sorted(k for k, v in after.items() if before.get(k) != v)
        return {"ok": bool(steps) and all(st["exit_code"] == 0 for st in steps) and len(steps) == len(commands), "steps": steps,
                "files_changed": [{"path": k, "size": after[k][1]} for k in changed[:100]]}  # fmt: skip

    def parse_touchstone(self, path: str, max_points: int = 500) -> dict:
        """Read an .sNp file with scikit-rf. Returns {f_hz, S, ...}; S[freq][row][col] = [re, im]. Evenly decimated to max_points."""
        c, _ = self._existing_file(path, "touchstone file")
        res = self.containers.exec(["python3", "-", c, str(int(max_points))], input=_TOUCHSTONE, timeout=120)
        if res.returncode != 0:
            raise ToolError(f"could not parse touchstone file: {_tail(res.stderr, 1500)}")
        return json.loads(res.stdout.strip().splitlines()[-1])

    def list_pdk_models(self, pdk: str, pattern: str = "", limit: int = 150) -> dict:
        """List ngspice library files (with `.lib` sections) and device subckts/models of a PDK."""
        pdk = self._check_pdk(pdk)
        if pdk is None:
            raise ToolError("pdk is required")
        res = self.containers.exec(["python3", "-", pdk, pattern, str(min(max(limit, 1), 1000))], input=_PDK_SCAN, timeout=120)
        if res.returncode != 0:
            raise ToolError(f"pdk scan failed: {_tail(res.stderr, 1500)}")
        out = json.loads(res.stdout.strip().splitlines()[-1])
        if "error" in out:
            raise ToolError(f"{out['error']}; available: {', '.join(out['available'])}")
        return out

    @staticmethod
    def _check_pdk(pdk: str | None) -> str | None:
        if pdk is not None and not PDK_NAME.match(pdk):
            raise ToolError(f"invalid pdk name: {pdk!r}")
        return pdk

    def write_file(self, path: str, content: str) -> dict:
        if len(content.encode("utf-8")) > WRITE_MAX:
            raise ToolError(f"content too large (> {WRITE_MAX} bytes)")
        host = self.containers.to_host(self._cpath(path))
        host.parent.mkdir(parents=True, exist_ok=True)
        with open(host, "w", encoding="utf-8", newline="\n") as f:  # LF: scripts run in Linux
            f.write(content)
        return {"written": self._rel_to_node(host), "bytes": host.stat().st_size}

    def delete_file(self, path: str, recursive: bool = False) -> dict:
        """Delete a file (or, with recursive=True, a directory tree) inside the node directory.
        The node directory itself and its top-level layout dirs (workspace/, _hidden/, the reference) are protected."""
        c = self._cpath(path)
        node_c = f"{CONTAINER_NODES}/{self.containers.node_of(c)}"
        rel = posixpath.relpath(c, node_c)
        if rel in (".", "workspace", "_hidden", CHECKER_TARGETS["reference"]):
            raise ToolError(f"refusing to delete a protected node directory: {rel}")
        host = self.containers.to_host(c)
        if host.is_symlink() or host.is_file():
            host.unlink()
            return {"deleted": rel, "files": 1}
        if host.is_dir():
            if not recursive:
                raise ToolError(f"{rel} is a directory; pass recursive=true to delete it")
            n = sum(1 for p in host.rglob("*") if p.is_file())
            shutil.rmtree(host)
            return {"deleted": rel, "files": n}
        raise ToolError(f"not found: {path}")

    def read_file(self, path: str, max_bytes: int = READ_MAX) -> dict:
        _, host = self._existing_file(path, "file")
        n = min(max(max_bytes, 1), READ_MAX)
        data = host.read_bytes()
        text = data[:n].decode("utf-8", errors="replace")
        return {"path": self._rel_to_node(host), "bytes": len(data), "truncated": len(data) > n, "content": text}


TOOL_ERRORS = (ToolError, ContainerError, PathError)
