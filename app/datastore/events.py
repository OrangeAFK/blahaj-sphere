"""Append-only `events.jsonl` (SPEC §5.8): the source of truth for evidence."""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from app.competency.engine import EVENT_TYPES, MAX_TIER, MIN_TIER, Event


class EventError(ValueError):
    pass


def validate(ev: Event) -> None:
    if ev.type not in EVENT_TYPES:
        raise EventError(f"unknown event type {ev.type!r}")
    if not isinstance(ev.node_id, int) or isinstance(ev.node_id, bool):
        raise EventError("node_id must be an int")
    if not ev.domain or not ev.subskill:
        raise EventError("domain and subskill are required")
    if not isinstance(ev.tier, int) or not MIN_TIER <= ev.tier <= MAX_TIER:
        raise EventError(f"tier must be {MIN_TIER}..{MAX_TIER}")
    if not 0.0 <= ev.value <= 1.0:
        raise EventError("value must be in 0..1 (base value, before modifiers)")
    if ev.weight <= 0:
        raise EventError("weight must be > 0")


def new_event(**kw) -> Event:
    """Fill id/ts if absent, then validate."""
    kw.setdefault("id", str(uuid.uuid4()))
    kw.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
    ev = Event(**kw)
    validate(ev)
    return ev


def append_event(path: Path, ev: Event) -> None:
    """One `write` of one line, then fsync. Never rewrites or truncates the file."""
    validate(ev)
    line = json.dumps(ev.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as f:
        if f.seek(0, os.SEEK_END) > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line  # a torn last line must not swallow this event
        f.write(line.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


def read_events(path: Path) -> tuple[list[Event], list[tuple[int, str]]]:
    """-> (events in file order, [(1-based line number, reason)] for lines that were skipped)."""
    events: list[Event] = []
    bad: list[tuple[int, str]] = []
    if not path.exists():
        return events, bad
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            try:
                ev = Event.from_dict(json.loads(raw))
                validate(ev)
            except (json.JSONDecodeError, TypeError, EventError) as e:
                bad.append((n, str(e)))
                continue
            events.append(ev)
    return events, bad
