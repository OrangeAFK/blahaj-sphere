"""Append-only `misconceptions.jsonl` (DECISIONS M6a): the source of truth for misconceptions; the SQLite table is rebuilt from it.

Two record types, one JSON object per line:
  {"type": "add",     "id": 3, "ts": "...", "node_id": 7, "subskill": "matching.l_network", "text": "..."}
  {"type": "resolve", "id": 3, "ts": "...", "node_id": 9, "event_id": "uuid"}   # an interleaved item on it scored >= 2/3
Ids are sequential in file order. Nothing is ever rewritten: a resolution is a new line.
"""

import json
import os
from datetime import datetime
from pathlib import Path

RESOLVE_MIN_VALUE = 2 / 3  # §7.7: "an interleaved item on them scores >= 2" on the 0-3 scale (a 0-1 base value here)


def read_records(path: Path) -> list[dict]:
    """Records in file order; malformed lines are skipped (a torn last line must not break a rebuild)."""
    out: list[dict] = []
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("type") in ("add", "resolve") and isinstance(r.get("id"), int):
                out.append(r)
    return out


def fold(records: list[dict]) -> list[dict]:
    """-> one dict per misconception: id, text, subskill, node_id, resolved (bool), resolved_by_node. Order = id."""
    by_id: dict[int, dict] = {}
    for r in records:
        if r["type"] == "add":
            by_id.setdefault(r["id"], {"id": r["id"], "text": str(r.get("text", "")), "subskill": r.get("subskill"),
                                       "node_id": r.get("node_id"), "ts": r.get("ts"), "resolved": False, "resolved_by_node": None})  # fmt: skip
        elif r["id"] in by_id:
            by_id[r["id"]]["resolved"] = True
            by_id[r["id"]]["resolved_by_node"] = r.get("node_id")
    return [by_id[k] for k in sorted(by_id)]


def _append(path: Path, rec: dict) -> None:
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as f:
        if f.seek(0, os.SEEK_END) > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line
        f.write(line.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def add_misconception(path: Path, *, node_id: int, subskill: str | None, text: str) -> int | None:
    """Append an `add` record unless the same text is already open on that subskill. -> the new id, or None if a duplicate."""
    recs = read_records(path)
    items = fold(recs)
    norm = " ".join(text.split()).lower()
    if any(not m["resolved"] and m["subskill"] == subskill and " ".join(m["text"].split()).lower() == norm for m in items):
        return None
    nid = max((m["id"] for m in items), default=0) + 1
    _append(path, {"type": "add", "id": nid, "ts": _now(), "node_id": node_id, "subskill": subskill, "text": text.strip()})
    return nid


def resolve_misconception(path: Path, mid: int, *, node_id: int, event_id: str) -> bool:
    if not any(m["id"] == mid and not m["resolved"] for m in fold(read_records(path))):
        return False
    _append(path, {"type": "resolve", "id": mid, "ts": _now(), "node_id": node_id, "event_id": event_id})
    return True
