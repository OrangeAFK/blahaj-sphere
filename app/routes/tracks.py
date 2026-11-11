"""Tracks (SPEC §8.6): list, create, enter, rename, archive, reset, delete, restore, purge. Sync handlers run in the threadpool."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.settings import data_dir
from app.tracks.migrate import ensure_tracks
from app.tracks.registry import TrackError
from app.tracks.service import TrackManager

router = APIRouter(prefix="/tracks")


class CurrentNode(BaseModel):
    id: int
    title: str | None
    status: str | None


class TrackView(BaseModel):
    id: str
    name: str
    domain: str
    status: str  # active | paused | archived
    active: bool
    created: str
    updated: str
    stage: str  # onboarding: interview | framework | diagnostic | done
    nodes: int
    current_node: CurrentNode | None
    tiers: dict[str, int]
    mastery: dict[str, float]
    last_activity: str | None


class TrashView(BaseModel):
    entry: str
    track_id: str
    name: str
    domain: str
    kind: str  # deleted (restorable) | reset
    trashed_at: str
    events: int
    misconceptions: int
    restorable: bool


class TracksView(BaseModel):
    active: str
    tracks: list[TrackView]
    trash: list[TrashView]
    domains: list[str]  # installed domain plugins


class CreateBody(BaseModel):
    name: str
    domain: str = "rfic"


class RenameBody(BaseModel):
    name: str


class ResetBody(BaseModel):
    keep_goals: bool = True


class DeleteBody(BaseModel):
    confirm_name: str


class PurgeBody(BaseModel):
    confirm: str  # must be "PURGE"


class PurgeResult(BaseModel):
    purged: list[str]


def _mgr() -> TrackManager:
    root = data_dir()
    try:
        ensure_tracks(root)
    except TrackError as e:
        raise HTTPException(e.status, e.message) from None
    return TrackManager(root)


def _call(fn):
    try:
        return fn()
    except TrackError as e:
        raise HTTPException(e.status, e.message) from None


def _view(m: TrackManager) -> dict:
    tracks = m.summaries()
    return {"active": next(t.id for t in tracks if t.active), "tracks": [t.__dict__ for t in tracks], "trash": m.trash(), "domains": m.domains()}


@router.get("", response_model=TracksView, operation_id="listTracks")
def list_tracks() -> dict:
    return _call(lambda: _view(_mgr()))


@router.post("", response_model=TracksView, status_code=201, operation_id="createTrack")
def create_track(body: CreateBody) -> dict:
    """Create a track and make it the active one; onboarding (§9) then runs for it."""
    m = _mgr()
    _call(lambda: m.create(body.name, body.domain))
    return _call(lambda: _view(m))


@router.get("/trash", response_model=list[TrashView], operation_id="listTrash")
def list_trash() -> list[dict]:
    return _call(lambda: _mgr().trash())


@router.post("/trash/{entry}/restore", response_model=TracksView, operation_id="restoreTrack")
def restore_track(entry: str) -> dict:
    m = _mgr()
    _call(lambda: m.restore(entry))
    return _call(lambda: _view(m))


@router.delete("/trash", response_model=PurgeResult, operation_id="purgeTrash")
def purge_trash(body: PurgeBody) -> dict:
    """Permanently delete everything in the trash. Needs {"confirm": "PURGE"}."""
    return {"purged": _call(lambda: _mgr().purge(body.confirm))}


@router.delete("/trash/{entry}", response_model=PurgeResult, operation_id="purgeTrashEntry")
def purge_trash_entry(entry: str, body: PurgeBody) -> dict:
    return {"purged": _call(lambda: _mgr().purge(body.confirm, entry))}


@router.post("/{track_id}/activate", response_model=TracksView, operation_id="activateTrack")
def activate_track(track_id: str) -> dict:
    m = _mgr()
    _call(lambda: m.activate(track_id))
    return _call(lambda: _view(m))


@router.patch("/{track_id}", response_model=TracksView, operation_id="renameTrack")
def rename_track(track_id: str, body: RenameBody) -> dict:
    m = _mgr()
    _call(lambda: m.rename(track_id, body.name))
    return _call(lambda: _view(m))


@router.post("/{track_id}/archive", response_model=TracksView, operation_id="archiveTrack")
def archive_track(track_id: str) -> dict:
    m = _mgr()
    _call(lambda: m.archive(track_id))
    return _call(lambda: _view(m))


@router.post("/{track_id}/unarchive", response_model=TracksView, operation_id="unarchiveTrack")
def unarchive_track(track_id: str) -> dict:
    m = _mgr()
    _call(lambda: m.unarchive(track_id))
    return _call(lambda: _view(m))


@router.post("/{track_id}/reset", response_model=TracksView, operation_id="resetTrack")
def reset_track(track_id: str, body: ResetBody) -> dict:
    """Move the track's nodes, events and misconceptions to the trash (goals kept unless keep_goals=false); onboarding re-runs."""
    m = _mgr()
    _call(lambda: m.reset(track_id, body.keep_goals))
    return _call(lambda: _view(m))


@router.delete("/{track_id}", response_model=TracksView, operation_id="deleteTrack")
def delete_track(track_id: str, body: DeleteBody) -> dict:
    """Typed-name confirmation; the track goes to the trash and can be restored until the trash is purged."""
    m = _mgr()
    _call(lambda: m.delete(track_id, body.confirm_name))
    return _call(lambda: _view(m))
