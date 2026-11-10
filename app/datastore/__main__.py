"""`uv run python -m app.datastore rebuild_db [--data-dir DIR]` / `init`."""

import argparse
from pathlib import Path

from app.settings import data_dir

from .cache import rebuild_db
from .layout import DataLayout, init_repo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.datastore")
    ap.add_argument("command", choices=["rebuild_db", "init"])
    ap.add_argument("--data-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    root = args.data_dir or data_dir()
    if args.command == "init":
        DataLayout(root).ensure()
        init_repo(root)
        print(f"initialized {root}")
        return 0
    r = rebuild_db(root)
    print(f"rebuilt {DataLayout(root).cache_path}: {r.nodes} nodes, {r.events} events, {r.domains} domains")
    for d in r.skipped_dirs:
        print(f"  skipped (no node.md): {d}")
    for d, why in r.node_issues.items():
        print(f"  node issue: {d}: {why}")
    for n, why in r.bad_event_lines:
        print(f"  events.jsonl line {n} skipped: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
