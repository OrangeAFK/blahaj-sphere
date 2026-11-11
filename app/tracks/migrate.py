"""Pre-tracks `data/` -> track `rfic-default` (SPEC §8.6 Migration, DECISIONS M7.5).

Idempotent and resumable: every step skips what is already done and `tracks.yaml` is written last, so a crash simply re-runs.
Only an explicit allow-list of paths is ever moved; `models/`, `library/`, the databases and `settings.json` are not touched.

    uv run python -m app.datastore migrate_tracks [--data-dir DIR]
"""

import json
import os
import shutil
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from app.builder.nodefile import NodeFileError, read_node_md, set_frontmatter_scalar
from app.competency.engine import Event, score_all
from app.datastore.cache import connect, rebuild_db
from app.datastore.layout import GITIGNORE_TRACK_PATTERNS, DataLayout, commit_all
from app.datastore.lock import log_lock
from app.tracks.registry import DEFAULT_DOMAIN, DEFAULT_TRACK_ID, DEFAULT_TRACK_NAME, TrackRegistry, registry_exists

LEGACY_ITEMS = ("profile", "framework", "archive", "sessions", "planner", "rescopes.jsonl", "remediation.jsonl")  # -> tracks/<id>/
_lock = threading.Lock()


class MigrationError(RuntimeError):
    pass


@dataclass
class MigrationReport:
    migrated: bool = False
    track: str = DEFAULT_TRACK_ID
    moved: list[str] = field(default_factory=list)
    events_stamped: int = 0
    misconceptions_stamped: int = 0
    nodes_stamped: int = 0
    scores_before: dict = field(default_factory=dict)
    scores_after: dict = field(default_factory=dict)
    legacy_cache_matches: bool | None = None  # the pre-migration cache.db agrees with the events (None = there was none)


def needs_migration(root: Path) -> bool:
    """No registry yet, and something from the single-learner layout (or an event without a track) is on disk."""
    root = Path(root)
    if registry_exists(root):
        return False
    if any((root / n).exists() for n in LEGACY_ITEMS) or (root / "builds").is_dir():
        return True
    return any(_untracked_lines(root / name) for name in ("events.jsonl", "misconceptions.jsonl"))


def _untracked_lines(path: Path) -> bool:
    if not path.is_file():
        return False
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if isinstance(r, dict) and "track" not in r:
                return True
    return False


def ensure_tracks(root: Path) -> None:
    """App start / first request: migrate a legacy data dir, or seed the registry of a fresh one. No-op once tracks.yaml exists."""
    root = Path(root)
    if registry_exists(root):
        return
    with _lock:
        if registry_exists(root):
            return
        if needs_migration(root):
            migrate_to_tracks(root)
            return
        DataLayout(root).ensure()
        TrackRegistry(root).seed()
        DataLayout(root, DEFAULT_TRACK_ID).ensure()


# --- steps -----------------------------------------------------------------------------------------------------------------
def _scores_view(rows: dict[str, object]) -> dict:
    return {d: {"tier": s.tier, "mastery": s.mastery, "n_window": s.n_window, "n_since_tier": s.n_since_tier,
                "unlocked": bool(s.unlocked), "subskills_evidenced": list(s.subskills_evidenced)} for d, s in rows.items()}


def _legacy_events(path: Path, track: str) -> list[Event]:
    """Events in file order exactly as the engine will see them after stamping (unparseable lines are skipped, as read_events does)."""
    out: list[Event] = []
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            for raw in f:
                try:
                    d = json.loads(raw)
                    out.append(Event.from_dict({**d, "track": d.get("track", track)}))
                except (ValueError, TypeError):
                    continue
    return out


def _rewrite_jsonl(path: Path, track: str, key_after: str) -> int:
    """Add `track` to every JSON-object line that lacks it, keeping line order, the compact format and unparseable lines verbatim.
    Atomic (temp file + replace). -> number of lines stamped."""
    if not path.is_file():
        return 0
    with log_lock:
        return _stamp(path, track, key_after)


def _stamp(path: Path, track: str, key_after: str) -> int:
    n, out = 0, []
    with open(path, encoding="utf-8", newline="") as f:
        for raw in f:
            body = raw.rstrip("\r\n")
            try:
                r = json.loads(body)
            except ValueError:
                out.append(raw)
                continue
            if isinstance(r, dict) and "track" not in r:
                new: dict = {}
                for k, v in r.items():
                    new[k] = v
                    if k == key_after:
                        new["track"] = track
                new.setdefault("track", track)
                n += 1
                out.append(json.dumps(new, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                out.append(raw)
    if n:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(out), encoding="utf-8", newline="")
        os.replace(tmp, path)
    return n


def _prefix_builds(obj, track: str):
    """Stored data-relative paths `builds/<dir>/...` -> `builds/<track>/<dir>/...` (recursive over JSON values)."""
    if isinstance(obj, str):
        return f"builds/{track}/{obj[len('builds/'):]}" if obj.startswith("builds/") and not obj.startswith(f"builds/{track}/") else obj
    if isinstance(obj, list):
        return [_prefix_builds(x, track) for x in obj]
    if isinstance(obj, dict):
        return {k: _prefix_builds(v, track) for k, v in obj.items()}
    return obj


def _fix_json_file(path: Path, track: str) -> None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    new = _prefix_builds(doc, track)
    if new != doc:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(new, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
        os.replace(tmp, path)


def _fix_rescopes_jsonl(path: Path, track: str) -> None:
    if not path.is_file():
        return
    out, changed = [], False
    for raw in path.read_text(encoding="utf-8").splitlines(keepends=True):
        try:
            r = json.loads(raw)
        except ValueError:
            out.append(raw)
            continue
        new = _prefix_builds(r, track)
        changed |= new != r
        out.append(json.dumps(new, ensure_ascii=False, separators=(",", ":")) + "\n" if new != r else raw)
    if changed:
        path.write_text("".join(out), encoding="utf-8", newline="")


def _move(src: Path, dst: Path, report: MigrationReport) -> None:
    if not src.exists():
        return
    if dst.exists():
        raise MigrationError(f"cannot move {src.name}: {dst} already exists (a half-finished migration? compare both, then remove one)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    report.moved.append(f"{src.name} -> {dst.parent.name}/{dst.name}")


def _gitignore_update(root: Path) -> None:
    gi = root / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    have = set(text.splitlines())
    add = [ln for ln in GITIGNORE_TRACK_PATTERNS if ln not in have]
    if add:
        gi.write_text(text.rstrip("\n") + "\n# tracks layout (M7.5)\n" + "\n".join(add) + "\n", encoding="utf-8", newline="\n")


def _backfill_pending_grades(root: Path, track: str) -> None:
    db = root / "llm.db"
    if not db.is_file():
        return
    with closing(sqlite3.connect(db)) as conn, conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_grades)")]
        if not cols:
            return
        if "track" not in cols:
            conn.execute("ALTER TABLE pending_grades ADD COLUMN track TEXT")
        conn.execute("UPDATE pending_grades SET track = ? WHERE track IS NULL", (track,))


def _legacy_cache_scores(root: Path) -> dict | None:
    """The pre-migration cache.db `scores` table (no track column), if there is one: an independent 'before'."""
    p = root / "cache.db"
    if not p.is_file():
        return None
    try:
        with closing(connect(p)) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(scores)")]
            if not cols or "track" in cols:
                return None
            return {r["domain"]: {"tier": r["tier"], "mastery": r["mastery"], "n_window": r["n_window"], "n_since_tier": r["n_since_tier"],
                                  "unlocked": bool(r["unlocked"]), "subskills_evidenced": json.loads(r["subskills_evidenced"])}
                    for r in conn.execute("SELECT * FROM scores")}
    except (sqlite3.DatabaseError, ValueError):
        return None


def _same(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    return all(a[d]["tier"] == b[d]["tier"] and a[d]["n_window"] == b[d]["n_window"] and a[d]["n_since_tier"] == b[d]["n_since_tier"]
               and a[d]["unlocked"] == b[d]["unlocked"] and a[d]["subskills_evidenced"] == b[d]["subskills_evidenced"]
               and abs(a[d]["mastery"] - b[d]["mastery"]) < 1e-9 for d in a)


def migrate_to_tracks(root: Path, track: str = DEFAULT_TRACK_ID, name: str = DEFAULT_TRACK_NAME, domain: str = DEFAULT_DOMAIN) -> MigrationReport:
    """Everything existing becomes track `rfic-default` ("RFIC"). Scores must match before and after (else MigrationError)."""
    root = Path(root)
    layout = DataLayout(root, track)
    rep = MigrationReport(track=track)
    if registry_exists(root):
        return rep

    # 1. the independent "before": scores of the legacy events (and of the old cache.db, when there is one)
    rep.scores_before = _scores_view(score_all(_legacy_events(layout.events_path, track)))
    legacy_cache = _legacy_cache_scores(root)
    if legacy_cache is not None:
        rep.legacy_cache_matches = _same(legacy_cache, rep.scores_before)

    # 2. safety net: pre-migration copies of the append-only logs + a git snapshot of the data repo
    pre = root / "_migration" / "pre-tracks"
    if not pre.exists():
        pre.mkdir(parents=True)
        for f in ("events.jsonl", "misconceptions.jsonl"):
            if (root / f).is_file():
                shutil.copy2(root / f, pre / f)
    if (root / ".git").exists():
        try:
            commit_all(root, "pre-tracks snapshot (M7.5 migration)")
        except Exception:  # noqa: BLE001 - the snapshot is a convenience; the copies above are the hard guarantee
            pass

    # 3. moves (explicit allow-list)
    layout.tracks_root.mkdir(parents=True, exist_ok=True)
    layout.track_dir.mkdir(parents=True, exist_ok=True)
    for item in LEGACY_ITEMS:
        _move(root / item, layout.track_dir / item, rep)
    builds = layout.builds_root
    if builds.is_dir():
        dest = layout.builds_dir
        dest.mkdir(parents=True, exist_ok=True)
        for entry in sorted(builds.iterdir()):
            if entry.name != track:
                _move(entry, dest / entry.name, rep)

    # 4. stamp the track everywhere
    rep.events_stamped = _rewrite_jsonl(layout.events_path, track, "ts")
    rep.misconceptions_stamped = _rewrite_jsonl(layout.misconceptions_path, track, "type")
    if layout.nodes_dir.is_dir():
        for d in sorted(p for p in layout.nodes_dir.iterdir() if p.is_dir() and (p / "node.md").is_file()):
            try:
                fm, _ = read_node_md(d / "node.md")
            except (NodeFileError, OSError, UnicodeDecodeError):
                continue
            if fm.get("track") != track:
                set_frontmatter_scalar(d / "node.md", "track", track)
                rep.nodes_stamped += 1
    if layout.sessions_dir.is_dir():
        for f in sorted(layout.sessions_dir.glob("*/rescope.json")):
            _fix_json_file(f, track)
    _fix_rescopes_jsonl(layout.rescopes_path, track)
    if layout.builds_dir.is_dir():
        for f in sorted(layout.builds_dir.glob("*/*.json")):
            _fix_json_file(f, track)
    _gitignore_update(root)
    _backfill_pending_grades(root, track)

    # 5. registry last, then the cache and the check
    TrackRegistry(root).seed(track, name, domain)
    layout.ensure()
    rebuild_db(root)
    with closing(connect(layout.cache_path)) as conn:
        rep.scores_after = {r["domain"]: {"tier": r["tier"], "mastery": r["mastery"], "n_window": r["n_window"], "n_since_tier": r["n_since_tier"],
                                          "unlocked": bool(r["unlocked"]), "subskills_evidenced": json.loads(r["subskills_evidenced"])}
                            for r in conn.execute("SELECT * FROM scores WHERE track = ?", (track,))}
    if not _same(rep.scores_before, rep.scores_after):
        raise MigrationError("scores differ after the migration; restore data/ from the pre-tracks git snapshot (or _migration/pre-tracks) and report this")
    rep.migrated = True
    return rep
