"""`rfic:1` containers (SPEC §4.2, §6.1). Two roles share the docker plumbing in `DockerContainer`:

- `ContainerManager`: the Builder/checker container. One bind mount (data/tracks, everything incl. `_hidden/`),
  one path-translation pair, `docker exec` for the MCP tools, the checker and the release gate. No ports.
- `domains.rfic.learner.LearnerContainer`: the learner tools (Jupyter, noVNC GUIs, Run simulation). It mounts ONLY the
  active node's `workspace/`.

Only the `docker` CLI is used (no SDK dependency). Everything that touches docker goes through
`DockerContainer._docker`, which tests replace with a fake.
"""

import json
import os
import posixpath
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

IMAGE = "rfic:1"
CONTAINER = "blahaj-rfic"  # Builder/checker container
CONTAINER_TRACKS = "/work/tracks"  # data/tracks is the Builder container's one bind mount (SPEC §4.2); a node lives at <track>/archive/nodes/<dir>
ROLE_LABEL = "org.blahajsphere.role"
TRACKS_LABEL = "org.blahajsphere.tracks_root"
MAX_CAPTURE = 4_000_000


class ContainerError(RuntimeError):
    pass


class PathError(ValueError):
    """A path escaped (or never was inside) the directory a container mounts."""


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    seconds: float = 0.0


@dataclass
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _noop(_line: str) -> None:
    pass


def parse_node_token(token: str) -> tuple[str, str]:
    """`<track>/archive/nodes/<dir>` -> (track, dir). PathError for anything else (extra/missing parts, `.`/`..`, backslashes)."""
    parts = token.split("/")
    if len(parts) != 4 or parts[1:3] != ["archive", "nodes"] or any(p in ("", ".", "..") or "\\" in p or "\0" in p for p in parts):
        raise PathError(f"not a node token <track>/archive/nodes/<dir>: {token!r}")
    return parts[0], parts[3]


class DockerContainer:
    """Docker plumbing shared by both roles: inspect/start/stop/remove, image build, `docker exec`, detached spawns, URL helpers."""

    ROLE = ""

    def __init__(self, *, image: str, name: str, docker: str = "docker", open_url: Callable[[str], object] | None = None):
        self.image, self.name = image, name
        self.docker_bin = docker
        self._open_url = open_url
        self._lock = threading.RLock()

    def ensure_running(self) -> None:
        raise NotImplementedError

    # ---- docker plumbing ----

    def _docker(self, *args: str, input: str | None = None, timeout: float | None = 60) -> _Proc:
        try:
            r = subprocess.run(
                [self.docker_bin, *args],
                input=input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            raise ContainerError("docker not found on PATH; install/start Docker Desktop") from None
        except subprocess.TimeoutExpired:
            raise ContainerError(f"docker {args[0]} timed out after {timeout}s") from None
        return _Proc(r.returncode, r.stdout, r.stderr)

    def image_present(self) -> bool:
        return self._docker("image", "inspect", self.image, timeout=30).returncode == 0

    def _inspect(self) -> dict | None:
        r = self._docker("inspect", self.name, timeout=30)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)[0]

    def is_running(self) -> bool:
        info = self._inspect()
        return bool(info and info["State"]["Running"])

    @staticmethod
    def _label(info: dict | None, key: str) -> str | None:
        return ((info or {}).get("Config", {}).get("Labels") or {}).get(key)

    @staticmethod
    def _env(info: dict, key: str) -> str:
        for e in info["Config"]["Env"]:
            if e.startswith(key + "="):
                return e.split("=", 1)[1]
        raise ContainerError(f"{key} missing from container env; recreate it with remove()")

    def _run_new(self, *args: str, timeout: float = 120) -> None:
        """`docker run -d` a fresh container of this role (the image must exist)."""
        if not self.image_present():
            raise ContainerError(f"image {self.image} not found; run the image build job (preflight fix)")
        r = self._docker("run", "-d", "--name", self.name, "--shm-size", "1g", "-l", f"{ROLE_LABEL}={self.ROLE}", *args, timeout=timeout)
        if r.returncode != 0:
            raise ContainerError(f"docker run failed: {r.stderr.strip()}")

    def _start_stopped(self, info: dict) -> None:
        if not info["State"]["Running"]:
            r = self._docker("start", self.name, timeout=60)
            if r.returncode != 0:
                raise ContainerError(f"docker start failed: {r.stderr.strip()}")

    def stop(self) -> None:
        with self._lock:
            self._docker("stop", "-t", "5", self.name, timeout=60)

    def remove(self) -> None:
        with self._lock:
            self._docker("rm", "-f", self.name, timeout=60)

    # ---- image build ----

    def build_image(self, dockerfile: Path, on_line: Callable[[str], None] = _noop, on_progress: Callable[[float], None] | None = None) -> None:
        """`docker build` the rfic image (pulls the IIC-OSIC-TOOLS base first if absent). Streams output.

        Progress is a heuristic: finished BuildKit steps / highest step number seen."""
        cmd = [self.docker_bin, "build", "--progress=plain", "-t", self.image, "-f", str(dockerfile), str(dockerfile.parent)]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise ContainerError("docker not found on PATH; install/start Docker Desktop") from None
        step_re = re.compile(r"^#(\d+) ")
        done_re = re.compile(r"^#(\d+) DONE")
        highest, done = 1, set()
        tail: list[str] = []
        for line in proc.stdout:  # type: ignore[union-attr]
            on_line(line)
            tail = [*tail[-30:], line.rstrip()]
            if m := step_re.match(line):
                highest = max(highest, int(m.group(1)))
            if m := done_re.match(line):
                done.add(m.group(1))
            if on_progress:
                on_progress(min(len(done) / highest, 0.95))
        if proc.wait() != 0:
            raise ContainerError("docker build failed:\n" + "\n".join(tail[-10:]))

    # ---- exec ----

    @staticmethod
    def _wrap(argv: list[str], timeout: float | None) -> list[str]:
        # A login shell loads the image's PATH/PDK setup (/etc/profile.d/iic-osic-tools-setup.sh).
        # `timeout` must run inside the container: killing the host-side `docker exec` client would
        # leave the tool running there.
        script = 'if [ -n "$BLAHAJ_PDK" ]; then source iic-pdk-script.sh "$BLAHAJ_PDK" >/dev/null 2>&1; fi; '
        script += 'exec timeout -k 10 "$BLAHAJ_TIMEOUT" "$@"' if timeout else 'exec "$@"'
        return ["bash", "-lc", script, "_", *argv]

    def _exec_cmd(
        self, flags: list[str], argv: list[str], workdir: str | None, pdk: str | None, env: dict[str, str] | None, timeout: float | None
    ) -> list[str]:
        cmd = [self.docker_bin, "exec", *flags]
        if workdir:
            cmd += ["-w", workdir]
        full_env = dict(env or {})
        if pdk:
            full_env["BLAHAJ_PDK"] = pdk
        if timeout:
            full_env["BLAHAJ_TIMEOUT"] = str(int(timeout))
        for k, v in full_env.items():
            cmd += ["-e", f"{k}={v}"]
        return [*cmd, self.name, *self._wrap(argv, timeout)]

    def exec(
        self,
        argv: list[str],
        *,
        workdir: str | None = None,
        pdk: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
        on_line: Callable[[str], None] = _noop,
    ) -> ExecResult:
        """Run a command in the container. `on_line` receives each stdout/stderr line as it arrives.

        `timeout` (seconds) is enforced inside the container; on expiry returncode is 124."""
        self.ensure_running()
        cmd = self._exec_cmd(["-i"] if input is not None else [], argv, workdir, pdk, env, timeout)
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise ContainerError("docker not found on PATH; install/start Docker Desktop") from None

        out: list[str] = []
        err: list[str] = []
        cb_lock = threading.Lock()

        def pump(stream, sink: list[str]) -> None:
            size = 0
            for line in stream:
                if size < MAX_CAPTURE:
                    sink.append(line)
                    size += len(line)
                with cb_lock:
                    on_line(line)

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err), daemon=True),
        ]
        for t in threads:
            t.start()
        if input is not None:
            try:
                proc.stdin.write(input)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        # Host-side guard only; the in-container `timeout` normally fires first.
        timed_out = False
        try:
            proc.wait(timeout=(timeout + 30) if timeout else None)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            timed_out = True
        for t in threads:
            t.join(timeout=5)
        return ExecResult(
            returncode=proc.returncode,
            stdout="".join(out),
            stderr="".join(err),
            timed_out=timed_out or proc.returncode == 124,
            seconds=round(time.monotonic() - t0, 2),
        )

    def _spawn(self, argv: list[str], *, workdir: str | None = None, pdk: str | None = None, env: dict[str, str] | None = None) -> None:
        """Start a detached process (GUI, Jupyter) via `docker exec -d`."""
        cmd = self._exec_cmd(["-d"], argv, workdir, pdk, env, None)
        r = self._docker(*cmd[1:], timeout=60)
        if r.returncode != 0:
            raise ContainerError(f"could not start {argv[0]}: {r.stderr.strip()}")

    def _running(self, pattern: str) -> bool:
        return self._docker("exec", self.name, "pgrep", "-f", pattern, timeout=20).returncode == 0

    @staticmethod
    def _http_ok(url: str, timeout: float = 3) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except (urllib.error.URLError, OSError):
            return False

    def _open(self, url: str) -> None:
        if self._open_url is not None:
            self._open_url(url)
        else:
            import webbrowser

            webbrowser.open(url)


class ContainerManager(DockerContainer):
    """The Builder/checker container: mounts the whole tracks directory (incl. every `_hidden/`). Never used for learner tools."""

    ROLE = "builder"

    def __init__(
        self,
        tracks_root: Path,
        *,
        image: str = IMAGE,
        name: str = CONTAINER,
        docker: str = "docker",
        open_url: Callable[[str], object] | None = None,
    ):
        super().__init__(image=image, name=name, docker=docker, open_url=open_url)
        self.tracks_root = Path(tracks_root)

    # ---- path translation (the only place host <-> container paths are converted) ----

    def to_container(self, path: str | os.PathLike) -> str:
        """Host path (absolute, or relative to the tracks root) or container path -> container path.

        Raises PathError if the result would not lie inside /work/tracks."""
        raw = os.fspath(path)
        if raw == CONTAINER_TRACKS or raw.startswith(CONTAINER_TRACKS + "/"):
            rel = posixpath.relpath(posixpath.normpath(raw), CONTAINER_TRACKS)
        else:
            root = self.tracks_root.resolve()
            p = Path(raw)
            full = (p if p.is_absolute() else root / p).resolve()  # resolves symlinks and ..
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                raise PathError(f"path outside the tracks directory: {raw}") from None
        if rel == ".." or rel.startswith("../"):
            raise PathError(f"path outside the tracks directory: {raw}")
        return CONTAINER_TRACKS if rel == "." else f"{CONTAINER_TRACKS}/{rel}"

    def to_host(self, container_path: str) -> Path:
        norm = posixpath.normpath(container_path)
        if norm != CONTAINER_TRACKS and not norm.startswith(CONTAINER_TRACKS + "/"):
            raise PathError(f"not a tracks path in the container: {container_path}")
        rel = posixpath.relpath(norm, CONTAINER_TRACKS)
        root = self.tracks_root.resolve()
        return root if rel == "." else root.joinpath(*PurePosixPath(rel).parts)

    @staticmethod
    def node_of(container_path: str) -> str:
        """The node a container path belongs to: `<track>/archive/nodes/<dir>` (the first four components under /work/tracks).
        This token is how every tool and job names a node."""
        rel = posixpath.relpath(container_path, CONTAINER_TRACKS)
        parts = rel.split("/")
        if rel == "." or rel.startswith("..") or len(parts) < 4 or parts[1:3] != ["archive", "nodes"]:
            raise PathError(f"not inside a node directory: {container_path}")
        return "/".join(parts[:4])

    @staticmethod
    def is_node_dir(container_path: str) -> bool:
        """True for exactly `/work/tracks/<track>/archive/nodes/<dir>`."""
        try:
            return container_path == f"{CONTAINER_TRACKS}/{ContainerManager.node_of(container_path)}"
        except PathError:
            return False

    @staticmethod
    def workspace_of(node: str) -> str:
        return f"{CONTAINER_TRACKS}/{node}/workspace"

    @staticmethod
    def job_label(node: str) -> str:
        """`<track>/archive/nodes/<dir>` -> `<track>/<dir>`, the label jobs carry (DataLayout.job_label)."""
        parts = node.split("/")
        return f"{parts[0]}/{parts[-1]}"

    def node_token(self, node_dir: str | os.PathLike) -> str:
        """Host node directory -> its node token; PathError unless it is `<tracks_root>/<track>/archive/nodes/<dir>`."""
        c = self.to_container(node_dir)
        if not self.is_node_dir(c):
            raise PathError(f"not a node directory under {self.tracks_root}: {node_dir}")
        return self.node_of(c)

    # ---- lifecycle ----

    def ensure_running(self) -> None:
        """Create/start the container; idempotent. Recreated if the tracks mount moved or it predates the two-container split
        (the old container also published the GUI/Jupyter ports, which now belong to the learner container)."""
        with self._lock:
            self.tracks_root.mkdir(parents=True, exist_ok=True)
            root = str(self.tracks_root.resolve())
            info = self._inspect()
            if info is not None and (self._label(info, TRACKS_LABEL) != root or self._label(info, ROLE_LABEL) != self.ROLE):
                self.remove()
                info = None
            if info is None:
                # No published ports; the image's desktop still starts, with a random VNC password nobody uses.
                self._run_new("-e", f"VNC_PW={secrets.token_urlsafe(9)}", "-l", f"{TRACKS_LABEL}={root}", "-v", f"{root}:{CONTAINER_TRACKS}", self.image, "--wait")
            else:
                self._start_stopped(info)

    def status(self) -> dict:
        info = self._inspect()
        return {
            "image": self.image,
            "image_present": self.image_present(),
            "container": self.name,
            "running": bool(info and info["State"]["Running"]),
            "tracks_root": str(self.tracks_root.resolve()),
        }


def default_tracks_root() -> Path:
    from app.settings import data_dir

    return data_dir() / "tracks"
