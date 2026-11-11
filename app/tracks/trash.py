"""Track reset / delete / restore / purge (SPEC §8.6) over `data/_trash/`.

Safety rules (the shared `models/` and `library/` must survive every operation here):
  - only paths derived from a VALIDATED track id are moved: `tracks/<id>` (or its reset items), `builds/<id>`, and that track's lines of
    the shared logs; nothing at the data root is ever enumerated or moved;
  - `purge` deletes only inside `_trash/`, and only with the explicit confirmation word;
  - the logs are rewritten atomically under `log_lock`, other tracks' lines are copied through verbatim.
"""

import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from app.datastore.cache import rebuild_db
from app.datastore.layout import SHARED_DIRS, TRACK_RESET_ITEMS, DataLayout
from app.datastore.lock import log_lock
from app.tracks.registry import Track, TrackError, TrackRegistry, valid_id

PURGE_WORD = "PURGE"
LOGS = ("events.jsonl", "misconceptions.jsonl")


@dataclass
class TrashEntry:
    entry: str  # directory name under _trash/
    track_id: str
    name: str
    domain: str
    kind: str  # "deleted" (restorable) | "reset" (kept until purged)
    trashed_at: str
    events: int
    misconceptions: int
    restorable: bool


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _checked_layout(root: Path, track_id: str) -> DataLayout:
    if not valid_id(track_id) or track_id in SHARED_DIRS:
        raise TrackError(422, f"bad track id {track_id!r}")
    return DataLayout(root, track_id)


def _within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


# --- the shared logs -------------------------------------------------------------------------------------------------------
def pull_track_lines(root: Path, track_id: str) -> dict[str, list[str]]:
    """Remove `track_id`'s lines from events.jsonl / misconceptions.jsonl (atomic rewrite) and return them verbatim, in file order.
    Lines of other tracks, and lines that are not JSON objects, stay where they are."""
    layout = DataLayout(root)
    pulled: dict[str, list[str]] = {}
    with log_lock:
        for name in LOGS:
            path = layout.root / name
            mine: list[str] = []
            keep: list[str] = []
            if path.is_file():
                with open(path, encoding="utf-8", newline="") as f:
                    for raw in f:
                        try:
                            r = json.loads(raw)
                        except ValueError:
                            keep.append(raw)
                            continue
                        (mine if isinstance(r, dict) and r.get("track") == track_id else keep).append(raw)
            pulled[name] = mine
            if mine:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text("".join(keep), encoding="utf-8", newline="")
                os.replace(tmp, path)
    return pulled


def _append_lines(root: Path, name: str, lines: list[str]) -> None:
    if not lines:
        return
    path = DataLayout(root).root / name
    with log_lock, open(path, "ab+") as f:
        if f.seek(0, os.SEEK_END) > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write("".join(l if l.endswith("\n") else l + "\n" for l in lines).encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


def _cancel_pending_grades(root: Path, track_id: str) -> None:
    db = root / "llm.db"
    if not db.is_file():
        return
    try:
        with closing(sqlite3.connect(db)) as conn, conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_grades)")]
            if "track" in cols:
                conn.execute("UPDATE pending_grades SET status='cancelled' WHERE status='pending' AND track = ?", (track_id,))
    except sqlite3.DatabaseError:
        pass


# --- entries ---------------------------------------------------------------------------------------------------------------
def _entry_dir(root: Path, entry: str) -> Path:
    base = DataLayout(root).trash_dir
    d = base / entry
    if not entry or entry != Path(entry).name or not d.is_dir() or not _within(d, base):
        raise TrackError(404, f"no trash entry {entry!r}")
    return d


def _meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TrackError(500, f"trash entry {d.name} has no readable meta.json") from None


def list_trash(root: Path) -> list[TrashEntry]:
    base = DataLayout(root).trash_dir
    out: list[TrashEntry] = []
    if base.is_dir():
        for d in sorted(base.iterdir(), reverse=True):
            if not d.is_dir() or not (d / "meta.json").is_file():
                continue
            m = _meta(d)
            t = m["track"]
            out.append(TrashEntry(d.name, t["id"], t["name"], t["domain"], m["kind"], m["trashed_at"], int(m.get("events", 0)),
                                  int(m.get("misconceptions", 0)), m["kind"] == "deleted"))
    return out


def _new_entry(root: Path, track: Track, kind: str, pulled: dict[str, list[str]], extra: dict | None = None) -> Path:
    suffix = "-reset" if kind == "reset" else ""
    d = DataLayout(root).trash_dir / f"{track.id}-{_ts()}{suffix}"
    n = 1
    while d.exists():
        n += 1
        d = DataLayout(root).trash_dir / f"{track.id}-{_ts()}-{n}{suffix}"
    d.mkdir(parents=True)
    for name, lines in pulled.items():
        (d / name).write_text("".join(lines), encoding="utf-8", newline="")
    (d / "meta.json").write_text(json.dumps({
        "track": asdict(track), "kind": kind, "trashed_at": _now(), "events": len(pulled["events.jsonl"]),
        "misconceptions": len(pulled["misconceptions.jsonl"]), **(extra or {}),
    }, indent=1), encoding="utf-8", newline="\n")
    return d


def _move_in(src: Path, dst: Path) -> None:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)


# --- operations ------------------------------------------------------------------------------------------------------------
def reset_track(root: Path, reg: TrackRegistry, track_id: str, keep_goals: bool) -> TrashEntry:
    """Move the track's nodes, framework, sessions, planner state, builds, events and misconceptions to the trash and recreate it empty.
    `profile/goals.md` stays when `keep_goals` (the diagnostic/interview state does not: onboarding re-runs from the framework)."""
    layout = _checked_layout(root, track_id)
    track = reg.get(track_id)
    if track.status == "archived":
        raise TrackError(409, "unarchive this track before resetting it")
    pulled = pull_track_lines(root, track_id)
    d = _new_entry(root, track, "reset", pulled, {"keep_goals": keep_goals})
    dest = d / "track"
    for item in TRACK_RESET_ITEMS:
        _move_in(layout.track_dir / item, dest / item)
    prof = layout.profile_dir
    if prof.is_dir():
        for f in sorted(prof.iterdir()):
            if keep_goals and f.name == "goals.md":
                continue
            _move_in(f, dest / "profile" / f.name)
    _move_in(layout.builds_dir, d / "builds")
    layout.ensure()
    _cancel_pending_grades(root, track_id)
    rebuild_db(root)
    return next(e for e in list_trash(root) if e.entry == d.name)


def delete_track(root: Path, reg: TrackRegistry, track_id: str, confirm_name: str) -> TrashEntry:
    """Typed-name confirmation, then `tracks/<id>/` + `builds/<id>/` + the track's log lines -> `_trash/<id>-<ts>/` (restorable)."""
    layout = _checked_layout(root, track_id)
    track = reg.get(track_id)
    if " ".join(confirm_name.split()) != track.name:
        raise TrackError(422, "type the track's exact name to confirm the deletion")
    reg.remove(track_id)  # refuses the only active track; nothing has moved yet
    try:
        pulled = pull_track_lines(root, track_id)
        d = _new_entry(root, track, "deleted", pulled)
        _move_in(layout.track_dir, d / "track")
        _move_in(layout.builds_dir, d / "builds")
    except Exception:
        # Best effort: put the registry entry back so the data that did not move stays reachable.
        try:
            reg.restore(track)
        except TrackError:
            pass
        raise
    _cancel_pending_grades(root, track_id)
    rebuild_db(root)
    return next(e for e in list_trash(root) if e.entry == d.name)


def restore_track(root: Path, reg: TrackRegistry, entry: str) -> Track:
    """Move a deleted track back (its original id must be free) and re-append its log lines. The track returns as `paused`."""
    d = _entry_dir(root, entry)
    m = _meta(d)
    if m["kind"] != "deleted":
        raise TrackError(409, "only deleted tracks can be restored; reset entries stay in the trash until purged")
    t = Track(**m["track"])
    layout = _checked_layout(root, t.id)
    if any(x.id == t.id for x in reg.list()) or layout.track_dir.exists() or layout.builds_dir.exists():
        raise TrackError(409, f"a track {t.id!r} exists already; delete or rename it first")
    if any(x.name.casefold() == t.name.casefold() for x in reg.list()):
        raise TrackError(409, f"a track named {t.name!r} exists already; rename it first")
    _move_in(d / "track", layout.track_dir)
    _move_in(d / "builds", layout.builds_dir)
    for name in LOGS:
        p = d / name
        _append_lines(root, name, p.read_text(encoding="utf-8").splitlines(keepends=True) if p.is_file() else [])
    restored = reg.restore(t)
    layout.ensure()
    rebuild_db(root)
    _rmtree(d)  # nothing of value is left in it: everything was moved or appended
    return restored


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # read-only files (git objects in workspaces, Windows) must not block a purge
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=onerror)


def purge_trash(root: Path, confirm: str, entry: str | None = None) -> list[str]:
    """Permanently delete one trash entry (or all). The caller must pass the confirmation word explicitly."""
    if confirm != PURGE_WORD:
        raise TrackError(422, f"purging is permanent: confirm with {PURGE_WORD!r}")
    base = DataLayout(root).trash_dir
    names = [entry] if entry is not None else ([p.name for p in sorted(base.iterdir()) if p.is_dir()] if base.is_dir() else [])
    gone = []
    for n in names:
        d = _entry_dir(root, n)
        if d.parent.resolve() != base.resolve():  # never anything outside _trash/
            raise TrackError(422, "refusing to purge outside the trash")
        _rmtree(d)
        gone.append(n)
    return gone
