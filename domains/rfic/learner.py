"""The learner container (SPEC §4.2, §6.1): Jupyter, noVNC GUI tools (Xschem, KLayout, Qucs-S) and Run simulation.

It mounts ONLY the active node's `workspace/` (at /work/workspace) and is recreated whenever another node becomes
active, so nothing a learner tool can reach, including a Jupyter terminal, contains any `_hidden/`, another node,
another track or the rest of `data/`. The Builder/checker container (`ContainerManager`) keeps the full tracks mount.
"""

import os
import posixpath
import secrets
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Callable

from domains.rfic.container import (
    IMAGE,
    ROLE_LABEL,
    TRACKS_LABEL,
    ContainerError,
    DockerContainer,
    ExecResult,
    PathError,
    _noop,
    parse_node_token,
)
from domains.rfic.tools_mcp.tools import _tail, node_pdk, snapshot_files

LEARNER_CONTAINER = "blahaj-rfic-learner"
CONTAINER_WORKSPACE = "/work/workspace"  # the only learner-visible path; a bind mount of <track>/archive/nodes/<dir>/workspace
NODE_LABEL = "org.blahajsphere.learner_node"
WORKSPACE_LABEL = "org.blahajsphere.learner_workspace"  # host workspace dir + its inode: a replaced workspace dir must not keep a stale mount
GUI_PORT = 6080  # host port -> noVNC (container :80)
JUPYTER_PORT = 8888
DISPLAY = ":1"

# GUI tool name -> argv. `file` (if given) is appended as a container path.
GUI_TOOLS: dict[str, list[str]] = {
    "xschem": ["xschem"],
    "klayout": ["klayout", "-e"],
    "qucs-s": ["qucs-s"],
    "desktop": [],  # just the XFCE desktop in noVNC
}


class LearnerContainer(DockerContainer):
    ROLE = "learner"

    def __init__(
        self,
        tracks_root: Path,
        *,
        image: str = IMAGE,
        name: str = LEARNER_CONTAINER,
        gui_port: int = GUI_PORT,
        jupyter_port: int = JUPYTER_PORT,
        docker: str = "docker",
        open_url: Callable[[str], object] | None = None,
    ):
        super().__init__(image=image, name=name, docker=docker, open_url=open_url)
        self.tracks_root = Path(tracks_root)
        self.gui_port, self.jupyter_port = gui_port, jupyter_port
        self._busy = 0  # execs in flight: the node cannot switch under them

    # ---- host side: which directory is mounted, and path translation (nothing outside it is ever translated) ----

    def workspace_host(self, node: str) -> Path:
        """Host `workspace/` of a node token (created if the node exists without one). PathError for a malformed token,
        a missing node, or any symlink on the way that would make the mount point somewhere else."""
        track, d = parse_node_token(node)
        root = self.tracks_root.resolve()
        node_dir = root / track / "archive" / "nodes" / d
        if not node_dir.is_dir():
            raise PathError(f"no such node: {node}")
        ws = node_dir / "workspace"
        ws.mkdir(exist_ok=True)
        if os.path.normcase(str(ws.resolve())) != os.path.normcase(str(ws)):
            raise PathError(f"workspace of {node} is not a plain directory inside the tracks directory")
        return ws

    def node_token(self, node_dir: str | os.PathLike) -> str:
        """Host node directory -> node token; PathError unless it is `<tracks_root>/<track>/archive/nodes/<dir>`."""
        try:
            rel = Path(node_dir).resolve().relative_to(self.tracks_root.resolve()).as_posix()
        except ValueError:
            raise PathError(f"not a node directory under {self.tracks_root}: {node_dir}") from None
        parse_node_token(rel)
        return rel

    def to_container(self, node: str, path: str | os.PathLike) -> str:
        """A path of `node`'s workspace -> its container path under /work/workspace. Accepts a container path, a host path,
        or a path relative to the NODE directory (`workspace/amp.sch`); anything that does not end up inside that
        workspace (`..`, `_hidden/`, another node, a symlink leading out) raises PathError."""
        ws = self.workspace_host(node)
        raw = os.fspath(path)
        if raw == CONTAINER_WORKSPACE or raw.startswith(CONTAINER_WORKSPACE + "/"):
            rel = posixpath.relpath(posixpath.normpath(raw), CONTAINER_WORKSPACE)
            if rel == ".." or rel.startswith("../"):
                raise PathError(f"path outside the workspace: {raw}")
            full = ws.joinpath(*PurePosixPath(rel).parts)
        else:
            p = Path(raw)
            full = p if p.is_absolute() else ws.parent / p
        try:
            rel = full.resolve().relative_to(ws.resolve()).as_posix()  # resolves symlinks and ..
        except ValueError:
            raise PathError(f"path outside the workspace: {raw}") from None
        return CONTAINER_WORKSPACE if rel == "." else f"{CONTAINER_WORKSPACE}/{rel}"

    # ---- lifecycle ----

    def _signature(self, ws: Path) -> str:
        try:
            return f"{ws}#{ws.stat().st_ino}"
        except OSError:
            return str(ws)

    def ensure_running(self, node: str | None = None) -> None:
        """Create/start the container; idempotent. With a node, make sure the mounted workspace is that node's (recreating the
        container on a switch); without one, keep whatever is mounted (or mount nothing at all)."""
        with self._lock:
            self.tracks_root.mkdir(parents=True, exist_ok=True)
            root = str(self.tracks_root.resolve())
            ws = self.workspace_host(node) if node else None
            info = self._inspect()
            if info is not None and (self._label(info, TRACKS_LABEL) != root or self._label(info, ROLE_LABEL) != self.ROLE):
                self._replace()
                info = None
            elif info is not None and ws is not None and (self._label(info, NODE_LABEL), self._label(info, WORKSPACE_LABEL)) != (node, self._signature(ws)):
                self._replace()
                info = None
            if info is None:
                mount = ["-l", f"{NODE_LABEL}={node}", "-l", f"{WORKSPACE_LABEL}={self._signature(ws)}", "-v", f"{ws}:{CONTAINER_WORKSPACE}"] if ws else []
                self._run_new(
                    "-p", f"127.0.0.1:{self.gui_port}:80",
                    "-p", f"127.0.0.1:{self.jupyter_port}:8888",
                    "-e", f"VNC_PW={secrets.token_urlsafe(9)}",
                    "-e", f"JUPYTER_TOKEN={secrets.token_urlsafe(24)}",
                    "-l", f"{TRACKS_LABEL}={root}",
                    *mount,
                    self.image, "--wait",
                )  # fmt: skip
            else:
                self._start_stopped(info)

    def _replace(self) -> None:
        if self._busy:
            raise ContainerError("a simulation is still running in the learner container for another node; wait for it to finish")
        self.remove()

    def bound_node(self) -> str | None:
        return self._label(self._inspect(), NODE_LABEL) or None

    def status(self) -> dict:
        info = self._inspect()
        return {
            "container": self.name,
            "running": bool(info and info["State"]["Running"]),
            "node": self._label(info, NODE_LABEL) or None,
            "gui_port": self.gui_port,
            "jupyter_port": self.jupyter_port,
        }

    def exec(self, argv: list[str], *, node: str | None = None, workdir: str | None = None, **kw) -> ExecResult:  # type: ignore[override]
        """Run a command in the learner container (workdir defaults to the mounted workspace), mounting `node`'s workspace first."""
        with self._lock:
            self.ensure_running(node)
            self._busy += 1
        try:
            return super().exec(argv, workdir=workdir or (CONTAINER_WORKSPACE if node or self.bound_node() else None), **kw)
        finally:
            with self._lock:
                self._busy -= 1

    # ---- GUI (noVNC) ----

    def gui_url(self) -> str:
        info = self._inspect()
        if info is None:
            raise ContainerError("container not created")
        pw = self._env(info, "VNC_PW")
        return f"http://127.0.0.1:{self.gui_port}/vnc.html?autoconnect=true&resize=scale&password={pw}"

    def _wait_desktop(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._running("Xtigervnc") and self._http_ok(f"http://127.0.0.1:{self.gui_port}/vnc.html"):
                return
            time.sleep(1)
        raise ContainerError(f"noVNC desktop did not come up in time (docker logs {self.name})")

    def launch_gui(
        self,
        tool: str = "xschem",
        node: str | None = None,
        file: str | None = None,
        pdk: str | None = None,
        open_browser: bool = True,
    ) -> str:
        """Start a GUI tool on the container's VNC display and open noVNC in the system browser.
        `file` (a path in the node's workspace, see `to_container`) needs `node`."""
        if tool not in GUI_TOOLS:
            raise ContainerError(f"unknown GUI tool {tool!r}; choose from {sorted(GUI_TOOLS)}")
        if file and not node:
            raise ContainerError("a file needs the node it belongs to")
        argv = list(GUI_TOOLS[tool])
        try:
            if file and node:
                argv.append(self.to_container(node, file))
        except PathError as e:
            raise ContainerError(str(e)) from None
        self.ensure_running(node)
        self._wait_desktop()
        if argv and (file or not self._running(f"^{argv[0]}")):
            self._spawn(argv, workdir=CONTAINER_WORKSPACE if self.bound_node() else None, pdk=pdk, env={"DISPLAY": DISPLAY})
        url = self.gui_url()
        if open_browser:
            self._open(url)
        return url

    # ---- JupyterLab ----

    def launch_jupyter(self, node: str, open_browser: bool = True) -> str:
        """Ensure JupyterLab (rooted at the mounted workspace, i.e. at `node`'s) is running; return its URL."""
        if not node:
            raise ContainerError("JupyterLab needs a node")
        self.ensure_running(node)
        info = self._inspect()
        token = self._env(info, "JUPYTER_TOKEN")  # type: ignore[arg-type]
        base = f"http://127.0.0.1:{self.jupyter_port}"
        if not self._running("jupyter-lab"):
            # The image's ~/.jupyter config sets token='' (auth off) and beats the JUPYTER_TOKEN env var,
            # so the token must be given on the command line, which beats config files.
            self._spawn(
                [
                    "jupyter-lab", "--ip=0.0.0.0", "--port=8888", "--no-browser",
                    f"--ServerApp.root_dir={CONTAINER_WORKSPACE}", f"--IdentityProvider.token={token}",
                ],
                workdir=CONTAINER_WORKSPACE,
            )  # fmt: skip
        deadline = time.monotonic() + 60
        while not self._http_ok(f"{base}/api"):
            if time.monotonic() > deadline:
                raise ContainerError("JupyterLab did not come up in time")
            time.sleep(1)
        url = f"{base}/lab?token={token}"
        if open_browser:
            self._open(url)
        return url


_instances: dict[str, LearnerContainer] = {}
_instances_lock = threading.Lock()


def learner_for(tracks_root: Path) -> LearnerContainer:
    """The process-wide learner container manager for a tracks root (it carries the in-flight exec count)."""
    key = str(Path(tracks_root).resolve())
    with _instances_lock:
        return _instances.setdefault(key, LearnerContainer(Path(tracks_root)))


def run_commands(learner: LearnerContainer, node: str, commands: list[str], timeout_s: float, job=None) -> dict:
    """Lab "Run simulation": the node's `run` commands (node.md), in order, in the live workspace with the node's PDK env,
    inside the learner container (which sees nothing but that workspace).

    Stops at the first non-zero exit. Output streams to the job; `files_changed` lists what the whole run wrote."""
    host_ws = learner.workspace_host(node)
    pdk = node_pdk(host_ws.parent)
    before = snapshot_files(host_ws)
    steps: list[dict] = []
    for cmd in commands:
        if job is not None:
            job.log(f"$ {cmd}")
        res = learner.exec(
            ["bash", "-c", cmd], node=node, pdk=pdk, timeout=timeout_s, env={"PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"},
            on_line=job.log if job is not None else _noop,
        )  # fmt: skip
        if job is not None:
            job.flush()
        steps.append({"cmd": cmd, "exit_code": res.returncode, "seconds": round(res.seconds, 1), "timed_out": res.timed_out,
                      "stderr_tail": _tail(res.stderr, 1500) if res.returncode else ""})  # fmt: skip
        if res.returncode != 0:
            break
    after = snapshot_files(host_ws)
    changed = sorted(k for k, v in after.items() if before.get(k) != v)
    return {"ok": bool(steps) and all(st["exit_code"] == 0 for st in steps) and len(steps) == len(commands), "steps": steps,
            "files_changed": [{"path": k, "size": after[k][1]} for k in changed[:100]]}  # fmt: skip
