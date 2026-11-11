"""Tracks screen operations (SPEC §8.6): the registry plus the data moves, with the busy-guard and per-track summaries."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.datastore.cache import connect, rebuild_db
from app.datastore.events import read_events
from app.datastore.layout import DataLayout
from app.jobs.store import JobStore, default_db_path
from app.tracks import trash
from app.tracks.registry import Track, TrackError, TrackRegistry, installed_domains

DONE_STATUSES = ("done", "rescoped", "abandoned")


@dataclass
class TrackSummary:
    id: str
    name: str
    domain: str
    status: str  # active | paused | archived
    active: bool
    created: str
    updated: str
    stage: str  # onboarding stage: interview | framework | diagnostic | done
    nodes: int
    current_node: dict | None  # newest unfinished node {id, title, status}
    tiers: dict[str, int] = field(default_factory=dict)  # domain -> tier
    mastery: dict[str, float] = field(default_factory=dict)  # domain -> 0-100
    last_activity: str | None = None


def onboarding_stage(layout: DataLayout) -> str:
    """Cheap file check (same rule as Onboarding.state)."""
    if not layout.goals_path.is_file():
        return "interview"
    if not layout.framework_path.is_file():
        return "framework"
    try:
        done = bool(json.loads((layout.profile_dir / "diagnostic.json").read_text(encoding="utf-8")).get("done"))
    except (OSError, ValueError):
        done = False
    return "done" if done else "diagnostic"


class TrackManager:
    def __init__(self, root: Path, busy=None):
        """`busy(track_id) -> list of active job descriptions`; default = the app's job store."""
        self.root = Path(root)
        self.layout = DataLayout(self.root)
        self.reg = TrackRegistry(self.root)
        self._busy = busy

    # --- views ------------------------------------------------------------------------------------
    def summaries(self) -> list[TrackSummary]:
        if not self.layout.cache_path.is_file():
            rebuild_db(self.root)
        active = self.reg.active_id()
        out: list[TrackSummary] = []
        with closing(connect(self.layout.cache_path)) as conn:
            for t in self.reg.list():
                lay = self.layout.for_track(t.id)
                nodes = conn.execute("SELECT id, title, status, kind FROM nodes WHERE track = ? AND kind != 'diagnostic' ORDER BY id", (t.id,)).fetchall()
                cur = next((n for n in reversed(nodes) if n["status"] not in DONE_STATUSES), None)
                scores = conn.execute("SELECT domain, tier, mastery FROM scores WHERE track = ? ORDER BY domain", (t.id,)).fetchall()
                events, _ = read_events(self.layout.events_path, t.id)
                out.append(TrackSummary(
                    id=t.id, name=t.name, domain=t.domain, status=t.status, active=t.id == active, created=t.created, updated=t.updated,
                    stage=onboarding_stage(lay), nodes=len(nodes),
                    current_node={"id": cur["id"], "title": cur["title"], "status": cur["status"]} if cur else None,
                    tiers={r["domain"]: r["tier"] for r in scores}, mastery={r["domain"]: round(r["mastery"], 1) for r in scores},
                    last_activity=max([e.ts for e in events], default=None) or t.updated,
                ))
        return out

    def trash(self) -> list[dict]:
        return [asdict(e) for e in trash.list_trash(self.root)]

    def domains(self) -> list[str]:
        return installed_domains()

    # --- busy guard -------------------------------------------------------------------------------
    def _active_jobs(self, track_id: str) -> list[str]:
        if self._busy is not None:
            return list(self._busy(track_id))
        try:
            return [f"{j.type} ({j.node_id})" for j in JobStore(default_db_path()).active_for_track(track_id)]
        except Exception:  # noqa: BLE001 - no job db yet = nothing running
            return []

    def _require_idle(self, track_id: str, what: str) -> None:
        jobs = self._active_jobs(track_id)
        if jobs:
            raise TrackError(409, f"cannot {what} while jobs are running for this track: {', '.join(jobs[:3])}")

    # --- operations -------------------------------------------------------------------------------
    def create(self, name: str, domain: str) -> Track:
        t = self.reg.create(name, domain)
        self.layout.for_track(t.id).ensure()
        rebuild_db(self.root)
        return t

    def activate(self, track_id: str) -> Track:
        return self.reg.activate(track_id)

    def rename(self, track_id: str, name: str) -> Track:
        return self.reg.rename(track_id, name)

    def archive(self, track_id: str) -> Track:
        self._require_idle(track_id, "archive the track")
        return self.reg.archive(track_id)

    def unarchive(self, track_id: str) -> Track:
        return self.reg.unarchive(track_id)

    def reset(self, track_id: str, keep_goals: bool) -> dict:
        self.reg.get(track_id)
        self._require_idle(track_id, "reset the track")
        return asdict(trash.reset_track(self.root, self.reg, track_id, keep_goals))

    def delete(self, track_id: str, confirm_name: str) -> dict:
        self.reg.get(track_id)
        self._require_idle(track_id, "delete the track")
        return asdict(trash.delete_track(self.root, self.reg, track_id, confirm_name))

    def restore(self, entry: str) -> Track:
        return trash.restore_track(self.root, self.reg, entry)

    def purge(self, confirm: str, entry: str | None = None) -> list[str]:
        return trash.purge_trash(self.root, confirm, entry)
