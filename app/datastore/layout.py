"""`data/` filesystem layout (SPEC §4.1)."""

import subprocess
from pathlib import Path

GITIGNORE = """\
# Local state, never versioned (settings.json holds API keys)
settings.json
*.db
*.db-wal
*.db-shm
*.db-journal
# Workspace sim outputs. Git cannot ignore by size, so every touchstone file is ignored.
archive/nodes/*/workspace/**/*.raw
archive/nodes/*/workspace/**/*.s[0-9]p
archive/nodes/*/workspace/**/*.s[0-9][0-9]p
archive/nodes/*/workspace/**/*_sim/
archive/nodes/*/workspace/**/openems_sim*/
archive/nodes/*/.gate/
# Disposable M2/M2.5 spike material and failed builds (DECISIONS M5)
_spike_archive/
spike/
builds/failed/
__pycache__/
*.pyc
"""

DIRS = ("profile", "framework", "archive/nodes", "library")


class DataLayout:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def misconceptions_path(self) -> Path:
        return self.root / "misconceptions.jsonl"

    @property
    def sessions_dir(self) -> Path:
        """Per-node runtime state (tutor chat, checker runs, submissions, grading); not part of the node (§4.1)."""
        return self.root / "sessions"

    @property
    def nodes_dir(self) -> Path:
        return self.root / "archive" / "nodes"

    @property
    def cache_path(self) -> Path:
        return self.root / "cache.db"

    @property
    def framework_path(self) -> Path:
        return self.root / "framework" / "competency-v1.yaml"

    def node_dir(self, node_id: int, slug: str) -> Path:
        return self.nodes_dir / node_dir_name(node_id, slug)

    def ensure(self) -> None:
        """Create the §4.1 tree. Idempotent; never overwrites an existing file."""
        for d in DIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE, encoding="utf-8", newline="\n")
        self.events_path.touch(exist_ok=True)
        self.misconceptions_path.touch(exist_ok=True)


def node_dir_name(node_id: int, slug: str) -> str:
    return f"{node_id:04d}-{slug}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=blahaj", "-c", "user.email=blahaj@localhost", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )


def init_repo(root: Path) -> None:
    """`data/` is its own git repo (the parent repo ignores it)."""
    if not (Path(root) / ".git").exists():
        _git(Path(root), "init", "-q")
    # Node files run inside the Linux container: never let Windows git rewrite them to CRLF (DECISIONS M5).
    _git(Path(root), "config", "core.autocrlf", "false")


def commit_all(root: Path, message: str) -> bool:
    """Stage everything and commit; False if there was nothing to commit. For stage transitions (M6)."""
    _git(Path(root), "add", "-A")
    if not _git(Path(root), "status", "--porcelain").stdout.strip():
        return False
    _git(Path(root), "commit", "-q", "-m", message)
    return True
