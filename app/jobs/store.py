"""Job rows (SPEC §6.3) in stdlib sqlite, shared by every process that works on jobs.

The app's asyncio worker and out-of-process writers (the MCP server, which `claude -p` spawns)
use the same WAL-mode database file, so a sim started by an agent shows up in the UI tray.
This module imports only the stdlib + pydantic so the MCP server can use it without FastAPI.
"""

import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.settings import data_dir

Status = Literal["queued", "running", "done", "failed"]
ACTIVE: tuple[str, ...] = ("queued", "running")

LOG_TAIL_LINES = 40
LOG_LINE_MAX = 400
FLUSH_INTERVAL_S = 0.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  node_id TEXT,
  status TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0,
  log_tail TEXT NOT NULL DEFAULT '',
  error TEXT,
  external INTEGER NOT NULL DEFAULT 0,
  created_ns INTEGER NOT NULL,
  started_ns INTEGER,
  finished_ns INTEGER,
  updated_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_ns);
"""


class Job(BaseModel):
    id: str
    type: str
    node_id: str | None = None
    status: Status
    progress: float = 0.0
    log_tail: list[str] = []
    error: str | None = None
    external: bool = False
    created_ns: int
    started_ns: int | None = None
    finished_ns: int | None = None
    updated_ns: int


def default_db_path() -> Path:
    return data_dir() / "jobs.db"


def _row_to_job(r: sqlite3.Row) -> Job:
    d = dict(r)
    d["log_tail"] = d["log_tail"].split("\n") if d["log_tail"] else []
    return Job(**d)


class JobStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _write(self, sql: str, args: tuple = ()) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute(sql, args)

    def _read(self, sql: str, args: tuple = ()) -> list[Job]:
        with closing(self._conn()) as c:
            return [_row_to_job(r) for r in c.execute(sql, args)]

    def create(
        self, type: str, node_id: str | None = None, status: Status = "queued", external: bool = False
    ) -> Job:
        now = time.time_ns()
        job_id = uuid.uuid4().hex[:12]
        started = now if status == "running" else None
        self._write(
            "INSERT INTO jobs (id, type, node_id, status, external, created_ns, started_ns, updated_ns) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, type, node_id, status, int(external), now, started, now),
        )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> Job | None:
        rows = self._read("SELECT * FROM jobs WHERE id=?", (job_id,))
        return rows[0] if rows else None

    def list_jobs(self, active_only: bool = False, limit: int = 50) -> list[Job]:
        where = "WHERE status IN ('queued','running')" if active_only else ""
        return self._read(f"SELECT * FROM jobs {where} ORDER BY created_ns DESC LIMIT ?", (limit,))

    def active_for_track(self, track: str) -> list[Job]:
        """Queued/running jobs whose node label is `<track>/<dir>` (see DataLayout.job_label): the track busy-guard."""
        return [j for j in self.list_jobs(active_only=True, limit=1000) if (j.node_id or "").startswith(f"{track}/")]

    def changed_since(self, cursor_ns: int) -> list[Job]:
        """Jobs touched at or after `cursor_ns`, oldest change first."""
        return self._read("SELECT * FROM jobs WHERE updated_ns >= ? ORDER BY updated_ns", (cursor_ns,))

    def set_status(self, job_id: str, status: Status, error: str | None = None) -> None:
        now = time.time_ns()
        if status == "running":
            self._write("UPDATE jobs SET status=?, started_ns=?, updated_ns=? WHERE id=?", (status, now, now, job_id))
        elif status == "done":
            self._write(
                "UPDATE jobs SET status=?, progress=1.0, finished_ns=?, updated_ns=? WHERE id=?",
                (status, now, now, job_id),
            )
        else:
            self._write(
                "UPDATE jobs SET status=?, error=?, finished_ns=?, updated_ns=? WHERE id=?",
                (status, error, now, now, job_id),
            )

    def update(self, job_id: str, progress: float | None = None, log_tail: str | None = None) -> None:
        sets, args = ["updated_ns=?"], [time.time_ns()]
        if progress is not None:
            sets.append("progress=?")
            args.append(min(max(progress, 0.0), 1.0))
        if log_tail is not None:
            sets.append("log_tail=?")
            args.append(log_tail)
        self._write(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, job_id))

    def fail_interrupted(self, stale_external_s: float = 3600) -> int:
        """Called once at app start. App-owned jobs left active by a dead process can never finish.
        External (MCP-tracked) jobs may belong to a live `claude -p` run, so only reap quiet ones."""
        now = time.time_ns()
        stale = now - int(stale_external_s * 1e9)
        with self._lock, closing(self._conn()) as c:
            with c:
                cur = c.execute(
                    "UPDATE jobs SET status='failed', error='interrupted (no live worker)', finished_ns=?, updated_ns=? "
                    "WHERE status IN ('queued','running') AND (external=0 OR updated_ns < ?)",
                    (now, now, stale),
                )
                return cur.rowcount

    def begin_external(self, type: str, node_id: str | None = None) -> "JobHandle":
        """Record work done outside the app's worker (e.g. an MCP tool call) as a running job."""
        return JobHandle(self, self.create(type, node_id, status="running", external=True).id)


class JobHandle:
    """Thread-safe progress/log sink for one job. Log lines are coalesced to limit DB writes."""

    def __init__(self, store: JobStore, job_id: str):
        self.store = store
        self.id = job_id
        self._lines: list[str] = []
        self._dirty = False
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def progress(self, value: float) -> None:
        self.store.update(self.id, progress=value)

    def log(self, line: str) -> None:
        line = line.rstrip("\r\n")[:LOG_LINE_MAX]
        with self._lock:
            self._lines.append(line)
            del self._lines[:-LOG_TAIL_LINES]
            self._dirty = True
            due = time.monotonic() - self._last_flush >= FLUSH_INTERVAL_S
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            text, self._dirty, self._last_flush = "\n".join(self._lines), False, time.monotonic()
        self.store.update(self.id, log_tail=text)

    def done(self) -> None:
        self.flush()
        self.store.set_status(self.id, "done")

    def fail(self, error: str) -> None:
        self.flush()
        self.store.set_status(self.id, "failed", error=error)

    def fail_with_exception(self, e: BaseException) -> None:
        self.log(f"{e.__class__.__name__}: {e}")
        self.fail(f"{e.__class__.__name__}: {e}")
