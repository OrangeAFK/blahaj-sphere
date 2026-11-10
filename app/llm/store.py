import json
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, task_type TEXT NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL, error TEXT, input_tokens INTEGER, output_tokens INTEGER,
  latency_ms INTEGER, node_id INTEGER
);
CREATE TABLE IF NOT EXISTS llm_counters (
  day TEXT NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, backend, model)
);
CREATE TABLE IF NOT EXISTS pending_grades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created TEXT NOT NULL, task_type TEXT NOT NULL, node_id INTEGER, request TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending', last_error TEXT
);
"""

# Retry backoff for pending grades: 1 min doubling, capped at 1 h.
BACKOFF_BASE_S = 60
BACKOFF_MAX_S = 3600


def today() -> str:
    return date.today().isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return closing_commit(c)

    # --- counters -------------------------------------------------------
    def count(self, backend: str, model: str, day: str | None = None) -> int:
        with self._conn() as c:
            r = c.execute(
                "SELECT count FROM llm_counters WHERE day=? AND backend=? AND model=?",
                (day or today(), backend, model),
            ).fetchone()
        return r["count"] if r else 0

    def backend_count(self, backend: str, day: str | None = None) -> int:
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(count),0) n FROM llm_counters WHERE day=? AND backend=?",
                (day or today(), backend),
            ).fetchone()
        return r["n"]

    def increment(self, backend: str, model: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO llm_counters(day,backend,model,count) VALUES(?,?,?,1) "
                "ON CONFLICT(day,backend,model) DO UPDATE SET count=count+1",
                (today(), backend, model),
            )

    def usage_today(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT backend, model, count FROM llm_counters WHERE day=? ORDER BY backend, model", (today(),)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- call log -------------------------------------------------------
    def log_call(self, *, task_type, backend, model, status, error=None, usage=None, latency_ms=None, node_id=None):
        usage = usage or {}
        with self._conn() as c:
            c.execute(
                "INSERT INTO llm_calls(ts,task_type,backend,model,status,error,input_tokens,output_tokens,latency_ms,node_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), task_type, backend, model, status, error,
                 usage.get("input_tokens"), usage.get("output_tokens"), latency_ms, node_id),
            )

    def calls(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- grading queue (SPEC §3.1 skeleton) -----------------------------
    def enqueue_grade(self, task_type: str, request: dict, node_id: int | None, error: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO pending_grades(created,task_type,node_id,request,last_error,next_try) VALUES(?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), task_type, node_id, json.dumps(request), error,
                 time.time() + BACKOFF_BASE_S),
            )
            return cur.lastrowid

    def due_grades(self, now: float | None = None) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pending_grades WHERE status='pending' AND next_try<=? ORDER BY id",
                (time.time() if now is None else now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_grades(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pending_grades WHERE status='pending' ORDER BY id")]

    def grade_failed(self, grade_id: int, error: str) -> None:
        with self._conn() as c:
            r = c.execute("SELECT attempts FROM pending_grades WHERE id=?", (grade_id,)).fetchone()
            n = (r["attempts"] if r else 0) + 1
            delay = min(BACKOFF_BASE_S * 2**n, BACKOFF_MAX_S)
            c.execute(
                "UPDATE pending_grades SET attempts=?, next_try=?, last_error=? WHERE id=?",
                (n, time.time() + delay, error, grade_id),
            )

    def grade_done(self, grade_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE pending_grades SET status='done' WHERE id=?", (grade_id,))


class closing_commit:
    """sqlite3 connection as a context manager that commits and always closes."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.conn.commit()
        self.conn.close()
