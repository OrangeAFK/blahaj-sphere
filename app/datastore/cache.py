"""SQLite cache (SPEC §4.3): everything in it is derived from the node files + events.jsonl.

`rebuild_db` deletes and recreates `cache.db`. The operational tables (llm_calls, jobs, grading queue) are NOT here:
they live in llm.db / jobs.db (DECISIONS M1, M2) and a rebuild never touches them.
"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.builder.nodefile import NodeFileError, read_node_md
from app.competency.engine import Event, score_all, tier_history

from .events import append_event, read_events
from .layout import DataLayout
from .misconceptions import fold, read_records

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE nodes (
  id INTEGER PRIMARY KEY, dir TEXT NOT NULL, slug TEXT, title TEXT, kind TEXT, domain TEXT, tier INTEGER,
  status TEXT, provisional INTEGER, size_est_min INTEGER, sim_runtime_s INTEGER, pdk TEXT, created TEXT,
  parents TEXT NOT NULL, subskills TEXT NOT NULL);
CREATE TABLE node_issues (dir TEXT PRIMARY KEY, issue TEXT NOT NULL);
CREATE TABLE subskills (name TEXT PRIMARY KEY, domain TEXT NOT NULL, tier INTEGER, n_nodes INTEGER NOT NULL, n_events INTEGER NOT NULL);
CREATE TABLE scores (
  domain TEXT PRIMARY KEY, tier INTEGER NOT NULL, mastery REAL NOT NULL, n_window INTEGER NOT NULL,
  n_since_tier INTEGER NOT NULL, subskills_evidenced TEXT NOT NULL, unlocked INTEGER NOT NULL);
CREATE TABLE tiers (domain TEXT NOT NULL, tier INTEGER NOT NULL, since_event INTEGER NOT NULL, via TEXT NOT NULL,
  PRIMARY KEY (domain, tier, since_event));
CREATE TABLE misconceptions (id INTEGER PRIMARY KEY, text TEXT NOT NULL, subskill TEXT, node_id INTEGER, resolved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE library_chunks (book_id TEXT NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB);
"""
_COMPETENCY_TABLES = ("scores", "tiers", "subskills")


@dataclass
class RebuildReport:
    nodes: int = 0
    events: int = 0
    domains: int = 0
    skipped_dirs: list[str] = field(default_factory=list)  # no node.md (e.g. bare workspace dirs)
    node_issues: dict[str, str] = field(default_factory=dict)
    bad_event_lines: list[tuple[int, str]] = field(default_factory=list)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _text(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v)


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _index_nodes(conn: sqlite3.Connection, layout: DataLayout, report: RebuildReport) -> None:
    """Read every nodes/<dir>/node.md. A malformed node is recorded in node_issues, never raised."""
    if not layout.nodes_dir.is_dir():
        return
    for d in sorted(p for p in layout.nodes_dir.iterdir() if p.is_dir()):
        md = d / "node.md"
        if not md.is_file():
            report.skipped_dirs.append(d.name)
            continue
        try:
            fm, _ = read_node_md(md)
            nid = _int(fm.get("id"))
            if nid is None:
                raise NodeFileError("frontmatter `id` must be an integer")
            subs = fm.get("subskills") or []
            parents = fm.get("parents") or []
            if not isinstance(subs, list) or not isinstance(parents, list):
                raise NodeFileError("`subskills` and `parents` must be lists")
            conn.execute(
                "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (nid, d.name, _text(fm.get("slug")), _text(fm.get("title")), _text(fm.get("kind")), _text(fm.get("domain")),
                 _int(fm.get("tier")), _text(fm.get("status")), int(bool(fm.get("provisional"))), _int(fm.get("size_est_min")),
                 _int(fm.get("sim_runtime_s")), _text(fm.get("pdk")), _text(fm.get("created")),
                 json.dumps(parents), json.dumps([str(s) for s in subs])),
            )
            report.nodes += 1
        except (NodeFileError, OSError, UnicodeDecodeError, sqlite3.IntegrityError) as e:
            msg = "duplicate node id" if isinstance(e, sqlite3.IntegrityError) else str(e)
            report.node_issues[d.name] = msg
            conn.execute("INSERT INTO node_issues VALUES (?,?)", (d.name, msg))


def _index_misconceptions(conn: sqlite3.Connection, layout: DataLayout) -> None:
    """misconceptions.jsonl is the source of truth (append-only); the table is a fold of it."""
    for m in fold(read_records(layout.misconceptions_path)):
        conn.execute("INSERT INTO misconceptions VALUES (?,?,?,?,?)", (m["id"], m["text"], m["subskill"], m["node_id"], int(m["resolved"])))


def refresh_competency(conn: sqlite3.Connection, events: Sequence[Event]) -> None:
    """Recompute scores, tier history and subskills from the event list. Used by rebuild and after each append, so
    the incremental path and a rebuild cannot disagree."""
    for t in _COMPETENCY_TABLES:
        conn.execute(f"DELETE FROM {t}")
    scores = score_all(events)
    for d, s in scores.items():
        conn.execute("INSERT INTO scores VALUES (?,?,?,?,?,?,?)",
                     (d, s.tier, s.mastery, s.n_window, s.n_since_tier, json.dumps(list(s.subskills_evidenced)), int(s.unlocked)))
        for st in tier_history(events, d):
            conn.execute("INSERT INTO tiers VALUES (?,?,?,?)", (d, st.tier, st.since, st.via))

    subs: dict[str, dict] = {}

    def note(name: str, domain: str | None, tier: int | None, *, node: bool) -> None:
        s = subs.setdefault(name, {"domain": domain or name.split(".")[0], "tier": tier, "n_nodes": 0, "n_events": 0})
        if tier is not None and (s["tier"] is None or tier < s["tier"]):
            s["tier"] = tier
        s["n_nodes" if node else "n_events"] += 1

    for row in conn.execute("SELECT subskills, domain, tier FROM nodes ORDER BY id"):
        for name in json.loads(row["subskills"]):
            note(name, row["domain"], row["tier"], node=True)
    for e in events:
        note(e.subskill, e.domain, e.tier, node=False)
    for name, s in sorted(subs.items()):
        conn.execute("INSERT INTO subskills VALUES (?,?,?,?,?)", (name, s["domain"], s["tier"], s["n_nodes"], s["n_events"]))


def rebuild_db(data_dir: Path) -> RebuildReport:
    """Drop cache.db and rebuild it from the node files and events.jsonl."""
    layout = DataLayout(data_dir)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(layout.cache_path) + suffix).unlink(missing_ok=True)
    layout.root.mkdir(parents=True, exist_ok=True)
    report = RebuildReport()
    events, report.bad_event_lines = read_events(layout.events_path)
    report.events = len(events)
    with closing(connect(layout.cache_path)) as conn, conn:
        conn.executescript(SCHEMA)
        _index_nodes(conn, layout, report)
        _index_misconceptions(conn, layout)
        refresh_competency(conn, events)
        report.domains = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        conn.execute("INSERT INTO meta VALUES ('events', ?)", (str(len(events)),))
    return report


class DataStore:
    """The data/ directory: layout, event log, and the derived cache."""

    def __init__(self, root: Path):
        self.layout = DataLayout(root)

    def ensure(self) -> None:
        self.layout.ensure()
        if not self.layout.cache_path.exists():
            rebuild_db(self.layout.root)

    def append_event(self, ev: Event) -> None:
        """Append to events.jsonl (source of truth), then refresh the cached scores."""
        self.ensure()
        append_event(self.layout.events_path, ev)
        events, _ = read_events(self.layout.events_path)
        with closing(connect(self.layout.cache_path)) as conn, conn:
            refresh_competency(conn, events)

    def events(self) -> list[Event]:
        return read_events(self.layout.events_path)[0]

    def rebuild_db(self) -> RebuildReport:
        return rebuild_db(self.layout.root)

    def scores(self) -> dict[str, dict]:
        with closing(connect(self.layout.cache_path)) as conn:
            return {r["domain"]: dict(r) for r in conn.execute("SELECT * FROM scores ORDER BY domain")}


def snapshot(path: Path) -> dict[str, list[tuple]]:
    """Every derived table as plain rows, for comparing two caches."""
    with closing(connect(path)) as conn:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in ("nodes", "node_issues", "subskills", "scores", "tiers", "misconceptions")}
