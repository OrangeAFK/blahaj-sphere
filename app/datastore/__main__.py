"""`uv run python -m app.datastore rebuild_db|init|migrate_tracks [--data-dir DIR]`."""

import argparse
from pathlib import Path

from app.settings import data_dir

from .cache import rebuild_db
from .layout import DataLayout, init_repo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.datastore")
    ap.add_argument("command", choices=["rebuild_db", "init", "migrate_tracks"])
    ap.add_argument("--data-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    root = args.data_dir or data_dir()
    if args.command == "init":
        DataLayout(root).ensure()
        init_repo(root)
        print(f"initialized {root}")
        return 0
    if args.command == "migrate_tracks":
        from app.tracks.migrate import migrate_to_tracks, needs_migration
        from app.tracks.registry import registry_exists

        if registry_exists(root):
            print("already on tracks (tracks.yaml exists); nothing to do")
            return 0
        if not needs_migration(root):
            print("nothing to migrate (no pre-tracks data); the registry is seeded on first app start")
            return 0
        rep = migrate_to_tracks(root)
        print(f"migrated to track {rep.track}: {len(rep.moved)} items moved, {rep.events_stamped} events, "
              f"{rep.misconceptions_stamped} misconceptions, {rep.nodes_stamped} nodes stamped; scores identical "
              f"({len(rep.scores_after)} domains); old cache agreed: {rep.legacy_cache_matches}")
        return 0
    r = rebuild_db(root)
    print(f"rebuilt {DataLayout(root).cache_path}: {r.tracks} tracks, {r.nodes} nodes, {r.events} events, {r.domains} domain scores")
    for d in r.skipped_dirs:
        print(f"  skipped (no node.md): {d}")
    for d, why in r.node_issues.items():
        print(f"  node issue: {d}: {why}")
    for n, why in r.bad_event_lines:
        print(f"  events.jsonl line {n} skipped: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
