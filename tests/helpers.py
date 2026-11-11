"""Shared test helpers: a data dir on the tracks layout (SPEC §8.6), a scripted `docker` CLI."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.datastore.layout import DataLayout
from app.tracks.registry import TrackRegistry
from domains.rfic.container import _Proc

TRACK = "rfic-default"


def make_data(root: Path, *tracks: str) -> Path:
    """Registry + directory tree for `tracks` (default: one track `rfic-default`); the first one is active. -> root."""
    root = Path(root)
    tracks = tracks or (TRACK,)
    DataLayout(root).ensure()
    reg = TrackRegistry(root)
    reg.seed(tracks[0], tracks[0].replace("-", " ").title(), "rfic")
    for t in tracks[1:]:
        reg.create(t.replace("-", " ").title(), "rfic", track_id=t)
    if len(tracks) > 1:
        reg.activate(tracks[0])
    for t in tracks:
        DataLayout(root, t).ensure()
    return root


class DockerScript:
    """Mixin for a DockerContainer subclass: records `docker ...` argv and answers like a docker daemon that keeps one container."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.container: dict | None = None  # what `docker inspect` returns
        self.image_ok = True
        self.opened: list[str] = []
        self._open_url = self.opened.append

    def run_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "run"]

    def _docker(self, *args, input=None, timeout=60):
        self.calls.append(args)
        if args[0] == "image":
            return _Proc(0 if self.image_ok else 1)
        if args[0] == "inspect":
            return _Proc(0, json.dumps([self.container])) if self.container else _Proc(1)
        if args[0] == "run":
            pairs = [(args[i], args[i + 1]) for i in range(len(args) - 1)]
            self.container = {
                "State": {"Running": True},
                "Config": {
                    "Labels": dict(v.split("=", 1) for f, v in pairs if f == "-l"),
                    "Env": [v for f, v in pairs if f == "-e"],
                },
            }
            return _Proc(0, "cid")
        if args[0] == "rm":
            self.container = None
            return _Proc(0)
        if args[0] == "start":
            self.container["State"]["Running"] = True
            return _Proc(0)
        return _Proc(0)


def make_dir_link(link: Path, target: Path) -> None:
    """`link` -> directory `target`: a symlink, or on Windows without the symlink privilege a junction (both are followed by resolve())."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt" or subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True).returncode != 0:
            pytest.skip("neither symlinks nor junctions are permitted on this host")
