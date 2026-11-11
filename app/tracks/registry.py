"""`data/tracks.yaml`: the track registry and the active track id (SPEC §4.1, §8.6).

    active: rfic-default
    tracks:
      - {id: rfic-default, name: RFIC, domain: rfic, status: active, created: ..., updated: ...}

Invariant: exactly one track is `active` and it is the one named by `active:`; every other track is `paused` or `archived`.
The registry never touches track data; moving or trashing files is app/tracks/trash.py.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import yaml

from app.datastore.layout import DataLayout

DEFAULT_TRACK_ID = "rfic-default"
DEFAULT_TRACK_NAME = "RFIC"
DEFAULT_DOMAIN = "rfic"
STATUSES = ("active", "paused", "archived")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}  # Windows device names
DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
_lock = threading.RLock()


class TrackError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Track:
    id: str
    name: str
    domain: str
    status: str
    created: str
    updated: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def installed_domains() -> list[str]:
    """Domain plugins present in `domains/<name>/domain.py` (v1 ships only `rfic`)."""
    if not DOMAINS_DIR.is_dir():
        return []
    return sorted(p.name for p in DOMAINS_DIR.iterdir() if p.is_dir() and (p / "domain.py").is_file())


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return s or "track"


def valid_id(track_id: str) -> bool:
    return bool(SLUG.match(track_id)) and track_id not in _RESERVED


def registry_exists(root: Path) -> bool:
    return DataLayout(root).tracks_path.is_file()


def track_ids(root: Path) -> list[str]:
    """Ids the cache indexes: the registry's tracks, else (no registry yet) the directories under tracks/."""
    layout = DataLayout(root)
    if layout.tracks_path.is_file():
        return [t.id for t in TrackRegistry(root).list()]
    return sorted(p.name for p in layout.tracks_root.iterdir() if p.is_dir()) if layout.tracks_root.is_dir() else []


class TrackRegistry:
    def __init__(self, root: Path):
        self.layout = DataLayout(root)

    # --- storage ----------------------------------------------------------------------------------
    def _read(self) -> tuple[str | None, list[Track]]:
        p = self.layout.tracks_path
        if not p.is_file():
            return None, []
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        tracks = [Track(**{k: str(t.get(k, "")) for k in ("id", "name", "domain", "status", "created", "updated")}) for t in doc.get("tracks") or []]
        return doc.get("active"), tracks

    def _write(self, active: str | None, tracks: list[Track]) -> None:
        for t in tracks:  # the invariant, enforced on every write
            if t.status not in STATUSES:
                raise TrackError(500, f"bad track status {t.status!r}")
            if (t.status == "active") != (t.id == active):
                raise TrackError(500, "exactly the active track must have status active")
        p = self.layout.tracks_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump({"active": active, "tracks": [asdict(t) for t in tracks]}, sort_keys=False, allow_unicode=True),
                       encoding="utf-8", newline="\n")
        os.replace(tmp, p)

    # --- reads ------------------------------------------------------------------------------------
    def list(self) -> list[Track]:
        return self._read()[1]

    def active_id(self) -> str:
        active, tracks = self._read()
        if not active or not any(t.id == active for t in tracks):
            raise TrackError(409, "no active track: create one on the Tracks screen")
        return active

    def get(self, track_id: str) -> Track:
        for t in self.list():
            if t.id == track_id:
                return t
        raise TrackError(404, f"no track {track_id!r}")

    def active(self) -> Track:
        return self.get(self.active_id())

    # --- writes -----------------------------------------------------------------------------------
    def seed(self, track_id: str = DEFAULT_TRACK_ID, name: str = DEFAULT_TRACK_NAME, domain: str = DEFAULT_DOMAIN) -> Track:
        """First registry of a data dir (fresh install, or the migration): one active track."""
        with _lock:
            if self.layout.tracks_path.is_file():
                raise TrackError(409, "tracks.yaml already exists")
            t = Track(track_id, name, domain, "active", _now(), _now())
            self._write(track_id, [t])
            return t

    def _unique_id(self, base: str, tracks: list[Track]) -> str:
        taken = {t.id for t in tracks}
        i, tid = 1, base
        while tid in taken or self.layout.for_track(tid).track_dir.exists() or (self.layout.builds_root / tid).exists():
            i += 1
            tid = f"{base[: 40 - len(str(i)) - 1]}-{i}"
        return tid

    @staticmethod
    def _check_name(name: str, tracks: list[Track], ignore: str | None = None) -> str:
        name = " ".join(name.split())
        if not name or len(name) > 60:
            raise TrackError(422, "give the track a name (1-60 characters)")
        if any(t.name.casefold() == name.casefold() and t.id != ignore for t in tracks):
            raise TrackError(409, f"a track named {name!r} already exists")
        return name

    def create(self, name: str, domain: str, track_id: str | None = None) -> Track:
        """New track, which becomes the active one (SPEC §8.6 Create). The caller creates its directories."""
        with _lock:
            _, tracks = self._read()
            if domain not in installed_domains():
                raise TrackError(422, f"domain plugin {domain!r} is not installed (available: {installed_domains()})")
            name = self._check_name(name, tracks)
            if track_id is not None:
                if not valid_id(track_id):
                    raise TrackError(422, "track id must be a slug of a-z, 0-9 and '-'")
                if any(t.id == track_id for t in tracks) or self.layout.for_track(track_id).track_dir.exists():
                    raise TrackError(409, f"track {track_id!r} already exists")
                tid = track_id
            else:
                base = slugify(name)
                base = base if valid_id(base) else f"track-{base}"[:40]
                tid = self._unique_id(base, tracks)
            now = _now()
            t = Track(tid, name, domain, "active", now, now)
            for x in tracks:
                if x.status == "active":
                    x.status = "paused"
            self._write(tid, [*tracks, t])
            return t

    def activate(self, track_id: str) -> Track:
        with _lock:
            _, tracks = self._read()
            t = next((x for x in tracks if x.id == track_id), None)
            if t is None:
                raise TrackError(404, f"no track {track_id!r}")
            if t.status == "archived":
                raise TrackError(409, "unarchive this track before entering it")
            for x in tracks:
                x.status = "active" if x.id == track_id else ("paused" if x.status == "active" else x.status)
            t.updated = _now()
            self._write(track_id, tracks)
            return t

    def rename(self, track_id: str, name: str) -> Track:
        with _lock:
            active, tracks = self._read()
            t = next((x for x in tracks if x.id == track_id), None)
            if t is None:
                raise TrackError(404, f"no track {track_id!r}")
            t.name = self._check_name(name, tracks, ignore=track_id)
            t.updated = _now()
            self._write(active, tracks)
            return t

    @staticmethod
    def _leave_active(track_id: str, tracks: list[Track], active: str | None, what: str) -> str | None:
        """If `track_id` is the active track, pick the first other non-archived one; refuse when there is none."""
        if track_id != active:
            return active
        other = next((x for x in tracks if x.id != track_id and x.status != "archived"), None)
        if other is None:
            raise TrackError(409, f"cannot {what} the only active track: create another track first (or use Reset)")
        other.status = "active"
        return other.id

    def archive(self, track_id: str) -> Track:
        with _lock:
            active, tracks = self._read()
            t = next((x for x in tracks if x.id == track_id), None)
            if t is None:
                raise TrackError(404, f"no track {track_id!r}")
            if t.status == "archived":
                raise TrackError(409, "already archived")
            active = self._leave_active(track_id, tracks, active, "archive")
            t.status = "archived"
            t.updated = _now()
            self._write(active, tracks)
            return t

    def unarchive(self, track_id: str) -> Track:
        with _lock:
            active, tracks = self._read()
            t = next((x for x in tracks if x.id == track_id), None)
            if t is None:
                raise TrackError(404, f"no track {track_id!r}")
            if t.status != "archived":
                raise TrackError(409, "this track is not archived")
            t.status = "paused"
            t.updated = _now()
            self._write(active, tracks)
            return t

    def remove(self, track_id: str) -> Track:
        """Drop the registry entry (track delete). Data moves are app/tracks/trash.py's job."""
        with _lock:
            active, tracks = self._read()
            t = next((x for x in tracks if x.id == track_id), None)
            if t is None:
                raise TrackError(404, f"no track {track_id!r}")
            active = self._leave_active(track_id, tracks, active, "delete")
            self._write(active, [x for x in tracks if x.id != track_id])
            return t

    def restore(self, t: Track) -> Track:
        """Put a restored (previously deleted) track back as `paused`."""
        with _lock:
            active, tracks = self._read()
            if any(x.id == t.id for x in tracks):
                raise TrackError(409, f"track {t.id!r} already exists")
            if any(x.name.casefold() == t.name.casefold() for x in tracks):
                raise TrackError(409, f"a track named {t.name!r} already exists; rename it first")
            t = Track(t.id, t.name, t.domain, "paused", t.created, _now())
            self._write(active, [*tracks, t])
            return t
