"""The active track as seen by a request or a background loop (SPEC §8.6: one active track; everything else is scoped to it).

A `Scope` is captured once, when a request starts (or a job is submitted), and handed to the services, so switching the active track
while a build or a grading retry is in flight never redirects it.
"""

from dataclasses import dataclass
from pathlib import Path

from app.datastore.layout import DataLayout
from app.tracks.migrate import ensure_tracks
from app.tracks.registry import Track, TrackError, TrackRegistry


@dataclass(frozen=True)
class Scope:
    root: Path
    track: str

    @property
    def layout(self) -> DataLayout:
        return DataLayout(self.root, self.track)

    def info(self) -> Track:
        return TrackRegistry(self.root).get(self.track)


def active_scope(root: Path) -> Scope:
    """Migrates/seeds a data dir on first use, then returns the active track's scope. Raises TrackError (409) without one."""
    ensure_tracks(root)
    return Scope(Path(root), TrackRegistry(root).active_id())


def scope_of(root: Path, track: str) -> Scope:
    """An explicit track (the grading-queue retry loop works per queued row). Raises TrackError (404) for an unknown id."""
    ensure_tracks(root)
    TrackRegistry(root).get(track)
    return Scope(Path(root), track)


def current_scope() -> Scope:
    """FastAPI helper: the active track's scope for the data dir in use; TrackError becomes the matching HTTP error."""
    from fastapi import HTTPException

    from app.settings import data_dir

    try:
        return active_scope(data_dir())
    except TrackError as e:
        raise HTTPException(e.status, e.message) from None
