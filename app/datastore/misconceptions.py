"""Append-only `misconceptions.jsonl` (DECISIONS M6a): the source of truth for misconceptions; the SQLite table is rebuilt from it.

Two record types, one JSON object per line:
  {"type": "add",     "track": "rfic-default", "id": 3, "ts": "...", "node_id": 7, "subskill": "matching.l_network", "text": "..."}
  {"type": "resolve", "track": "rfic-default", "id": 3, "ts": "...", "node_id": 9, "event_id": "uuid"}   # an interleaved item on it scored >= 2/3
Ids are sequential PER TRACK in file order (so a track's lines can be trashed and restored without id collisions).
Nothing is rewritten while the app runs: a resolution is a new line (only track delete/reset removes a track's lines, SPEC §8.6).
"""

import json
import os
from datetime import datetime
from pathlib import Path

from .lock import log_lock

RESOLVE_MIN_VALUE = 2 / 3  # §7.7: "an interleaved item on them scores >= 2" on the 0-3 scale (a 0-1 base value here)


def read_records(path: Path, track: str | None = None) -> list[dict]:
    """Records in file order (one track's, or all with track=None); malformed lines and lines without a track are skipped
    (a torn last line must not break a rebuild)."""
    out: list[dict] = []
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if (isinstance(r, dict) and r.get("type") in ("add", "resolve") and isinstance(r.get("id"), int)
                    and isinstance(r.get("track"), str) and (track is None or r["track"] == track)):
                out.append(r)
    return out


def fold(records: list[dict]) -> list[dict]:
    """-> one dict per misconception: track, id, text, subskill, node_id, resolved (bool), resolved_by_node. Order = (track, id).
    Ids are per track, so records of several tracks fold side by side."""
    by_key: dict[tuple[str, int], dict] = {}
    for r in records:
        key = (r["track"], r["id"])
        if r["type"] == "add":
            by_key.setdefault(key, {"track": r["track"], "id": r["id"], "text": str(r.get("text", "")), "subskill": r.get("subskill"),
                                    "node_id": r.get("node_id"), "ts": r.get("ts"), "resolved": False, "resolved_by_node": None})  # fmt: skip
        elif key in by_key:
            by_key[key]["resolved"] = True
            by_key[key]["resolved_by_node"] = r.get("node_id")
    return [by_key[k] for k in sorted(by_key)]


def _append(path: Path, rec: dict) -> None:
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with log_lock, open(path, "ab+") as f:
        if f.seek(0, os.SEEK_END) > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line
        f.write(line.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def add_misconception(path: Path, *, track: str, node_id: int, subskill: str | None, text: str) -> int | None:
    """Append an `add` record unless the same text is already open on that subskill of this track. -> the new id, or None if a duplicate."""
    with log_lock:
        return _add(path, track, node_id, subskill, text)


def _add(path: Path, track: str, node_id: int, subskill: str | None, text: str) -> int | None:
    items = fold(read_records(path, track))
    norm = " ".join(text.split()).lower()
    if any(not m["resolved"] and m["subskill"] == subskill and " ".join(m["text"].split()).lower() == norm for m in items):
        return None
    nid = max((m["id"] for m in items), default=0) + 1
    _append(path, {"type": "add", "track": track, "id": nid, "ts": _now(), "node_id": node_id, "subskill": subskill, "text": text.strip()})
    return nid


def resolve_misconception(path: Path, mid: int, *, track: str, node_id: int, event_id: str) -> bool:
    if not any(m["id"] == mid and not m["resolved"] for m in fold(read_records(path, track))):
        return False
    _append(path, {"type": "resolve", "track": track, "id": mid, "ts": _now(), "node_id": node_id, "event_id": event_id})
    return True
