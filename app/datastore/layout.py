"""`data/` filesystem layout (SPEC §4.1, tracks §8.6).

Global files live at the data root (events, misconceptions, cache, tracks.yaml, shared library/models, trash); everything
else lives under `tracks/<id>/`. Track-scoped properties raise unless the layout was built for a track, so a service that
forgets its track fails loudly instead of silently reading another learner path.
"""

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
tracks/*/archive/nodes/*/workspace/**/*.raw
tracks/*/archive/nodes/*/workspace/**/*.s[0-9]p
tracks/*/archive/nodes/*/workspace/**/*.s[0-9][0-9]p
tracks/*/archive/nodes/*/workspace/**/*_sim/
tracks/*/archive/nodes/*/workspace/**/openems_sim*/
tracks/*/archive/nodes/*/.gate/
# Disposable M2/M2.5 spike material and failed builds (DECISIONS M5)
_spike_archive/
spike/
builds/*/failed/
# Deleted / reset tracks stay recoverable until purged (SPEC §8.6); they are not versioned
_trash/
_migration/
# Voice models (~520 MB, `python -m app.voice download`; DECISIONS M7)
models/
__pycache__/
*.pyc
"""

# Ignore lines the tracks layout adds (the migration appends the missing ones to an existing data repo's .gitignore).
GITIGNORE_TRACK_PATTERNS = tuple(
    ln for ln in GITIGNORE.splitlines()
    if ln.startswith(("tracks/*/", "builds/*/failed/", "_trash/", "_migration/"))
)

# Per-track directories created by `ensure` (relative to tracks/<id>/)
TRACK_DIRS = ("profile", "framework", "archive/nodes")
# Everything a track owns inside tracks/<id>/ besides what `reset` keeps (profile/goals.md is handled separately)
TRACK_RESET_ITEMS = ("archive", "framework", "sessions", "planner", "rescopes.jsonl", "remediation.jsonl")
# Shared across tracks: never moved, trashed or purged by any track operation (SPEC §8.6)
SHARED_DIRS = ("library", "models")


class LayoutError(RuntimeError):
    pass


class DataLayout:
    def __init__(self, root: Path, track: str | None = None):
        self.root = Path(root)
        self.track = track

    def for_track(self, track: str) -> "DataLayout":
        return DataLayout(self.root, track)

    # --- global -----------------------------------------------------------------------------------
    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def misconceptions_path(self) -> Path:
        return self.root / "misconceptions.jsonl"

    @property
    def cache_path(self) -> Path:
        return self.root / "cache.db"

    @property
    def tracks_path(self) -> Path:
        return self.root / "tracks.yaml"

    @property
    def tracks_root(self) -> Path:
        """`data/tracks`: what the domain container mounts (SPEC §4.2)."""
        return self.root / "tracks"

    @property
    def trash_dir(self) -> Path:
        return self.root / "_trash"

    @property
    def builds_root(self) -> Path:
        return self.root / "builds"

    @property
    def library_dir(self) -> Path:
        return self.root / "library"

    # --- track-scoped -----------------------------------------------------------------------------
    def _need_track(self) -> str:
        if not self.track:
            raise LayoutError("this path is track-scoped: build the layout with a track id")
        return self.track

    @property
    def track_dir(self) -> Path:
        return self.tracks_root / self._need_track()

    @property
    def profile_dir(self) -> Path:
        return self.track_dir / "profile"

    @property
    def goals_path(self) -> Path:
        return self.profile_dir / "goals.md"

    @property
    def framework_dir(self) -> Path:
        return self.track_dir / "framework"

    @property
    def framework_path(self) -> Path:
        return self.framework_dir / "competency-v1.yaml"

    @property
    def archive_dir(self) -> Path:
        return self.track_dir / "archive"

    @property
    def nodes_dir(self) -> Path:
        return self.archive_dir / "nodes"

    @property
    def summary_path(self) -> Path:
        return self.archive_dir / "SUMMARY.md"

    @property
    def sessions_dir(self) -> Path:
        """Per-node runtime state (tutor chat, checker runs, submissions, grading); not part of the node (§4.1)."""
        return self.track_dir / "sessions"

    @property
    def planner_dir(self) -> Path:
        return self.track_dir / "planner"

    @property
    def rescopes_path(self) -> Path:
        return self.track_dir / "rescopes.jsonl"

    @property
    def remediation_path(self) -> Path:
        return self.track_dir / "remediation.jsonl"

    @property
    def builds_dir(self) -> Path:
        """Build records, failed builds and rescope backups of this track (`builds/<track>/`)."""
        return self.builds_root / self._need_track()

    def node_dir(self, node_id: int, slug: str) -> Path:
        return self.nodes_dir / node_dir_name(node_id, slug)

    def job_label(self, node_dir: str) -> str:
        """Node label on jobs: `<track>/<dir>` (also what the track busy-guard matches)."""
        return f"{self._need_track()}/{node_dir}"

    def ensure(self) -> None:
        """Create the global files and, for a track layout, the §4.1 tree. Idempotent; never overwrites an existing file."""
        self.root.mkdir(parents=True, exist_ok=True)
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE, encoding="utf-8", newline="\n")
        self.events_path.touch(exist_ok=True)
        self.misconceptions_path.touch(exist_ok=True)
        self.library_dir.mkdir(parents=True, exist_ok=True)
        if self.track:
            for d in TRACK_DIRS:
                (self.track_dir / d).mkdir(parents=True, exist_ok=True)


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
