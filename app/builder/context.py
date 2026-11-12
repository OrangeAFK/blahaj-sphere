"""Learner state for the Planner and Builder (SPEC §7.1, §7.7), and the gate-4 interleaving targets.

Everything is read from the ACTIVE TRACK's files plus its rows of the derived cache (SPEC §8.6: interleaving, difficulty control and the
Planner use only the active track), so it is reproducible after `rebuild_db`.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.builder.gate import GateContext
from app.datastore.cache import DataStore, connect
from app.datastore.events import read_events
from app.datastore.layout import DataLayout
from app.datastore.misconceptions import fold, read_records
from app.onboarding.framework import Framework, FrameworkStore

RECENT_NODES = 3  # §7.1 gate 4: "not evidenced in the last 3 nodes"
RECENT_KINDS = ("challenge", "remediation", "tier_exam")  # the onboarding diagnostic is not a "node" for interleaving
INACTIVE_STATUSES = ("abandoned", "rescoped")  # superseded nodes carry no evidence: they must not shorten the window (DECISIONS M6a)
INACTIVE_SQL = "(" + ", ".join(f"'{s}'" for s in INACTIVE_STATUSES) + ")"
MAX_TARGETS = 4  # the Builder gets a short, prioritized list; the gate checks against the same list
PLANNER_HISTORY = 10  # §7.7: last 10 nodes' outcomes


def load_framework(root: Path, track: str) -> Framework | None:
    store = FrameworkStore(DataLayout(root, track).framework_dir)
    return store.load() if store.is_frozen() else None


def _rows(root: Path, track: str, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    """Run `sql` against the cache; `?` placeholders in `sql` come after the track, which is always the first parameter."""
    ds = DataStore(root, track)
    ds.ensure()
    with closing(connect(ds.layout.cache_path)) as conn:
        return list(conn.execute(sql, (track, *args)))


cache_rows = _rows  # public name for the other modules that read the track's rows of the cache (outcomes, graph)


def open_misconceptions(root: Path, track: str) -> list[dict]:
    """Open misconceptions of the track, oldest first. Read from misconceptions.jsonl (the source of truth), NOT the cache: a misconception
    logged while grading an oral must already be a target of the remediation build that starts in the same call (DECISIONS M8)."""
    return [{"text": m["text"], "subskill": m["subskill"]} for m in fold(read_records(DataLayout(Path(root)).misconceptions_path, track)) if not m["resolved"]]


def domain_scores(root: Path, track: str) -> dict[str, dict]:
    return {r["domain"]: {"tier": r["tier"], "mastery": round(r["mastery"], 1)}
            for r in _rows(root, track, "SELECT domain, tier, mastery FROM scores WHERE track = ?")}


def recent_nodes(root: Path, track: str, n: int = PLANNER_HISTORY) -> list[dict]:
    """Newest first: id, title, kind, domain, subskills, tier, status (the Planner's 'outcomes')."""
    rows = _rows(root, track, "SELECT id, title, kind, domain, subskills, tier, status FROM nodes WHERE track = ? AND kind != 'diagnostic' ORDER BY id DESC LIMIT ?", (n,))
    return [{**{k: r[k] for k in ("id", "title", "kind", "domain", "tier", "status")}, "subskills": json.loads(r["subskills"])} for r in rows]


def learner_state(root: Path, track: str) -> dict:
    """The Planner/Builder view of the learner on one track (SPEC §7.1 input, §7.7 Planner input). Never contains _hidden/ content."""
    lay = DataLayout(Path(root), track)
    goals, summary = lay.goals_path, lay.summary_path
    return {
        "goals": goals.read_text(encoding="utf-8") if goals.is_file() else "",
        "summary": summary.read_text(encoding="utf-8") if summary.is_file() else "No completed nodes yet.",
        "scores": domain_scores(root, track),
        "open_misconceptions": open_misconceptions(root, track),
        "recent_nodes": recent_nodes(root, track),
    }


def interleaving_context(root: Path, track: str, option: dict, limit: int = MAX_TARGETS) -> GateContext:
    """Gate-4 targets for a node built from `option` (DECISIONS M5).

    - open misconceptions of this track (cache, resolved = 0) come first;
    - stale subskills: framework subskills with any evidence in events.jsonl (diagnostic included) but no event from the
      last 3 nodes of kind challenge/remediation/tier_exam, excluding the option's own subskills (interleaving revisits
      something else); oldest last evidence first, then the lower-mastery domain.
    At most `limit` targets in total."""
    root = Path(root)
    fw = load_framework(root, track)
    fw_subs = {s: d.id for d in fw.domains for s in d.subskills} if fw else {}
    events, _ = read_events(DataLayout(root).events_path, track)
    nodes = _rows(root, track, f"SELECT id, kind FROM nodes WHERE track = ? AND COALESCE(status, '') NOT IN {INACTIVE_SQL} ORDER BY id DESC")
    recent = {r["id"] for r in [r for r in nodes if r["kind"] in RECENT_KINDS][:RECENT_NODES]}
    evidenced_recent = {e.subskill for e in events if e.node_id in recent}
    last_seen: dict[str, int] = {}
    for i, e in enumerate(events):
        if e.subskill in fw_subs:
            last_seen[e.subskill] = i
    own = set(option.get("subskills") or [])
    mastery = {d: s["mastery"] for d, s in domain_scores(root, track).items()}
    stale = sorted(
        (s for s in last_seen if s not in evidenced_recent and s not in own),
        key=lambda s: (last_seen[s], mastery.get(fw_subs[s], 0.0), s),
    )
    miscon = open_misconceptions(root, track)[:limit]
    return GateContext(open_misconceptions=miscon, stale_subskills=stale[: max(0, limit - len(miscon))])
