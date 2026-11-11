"""`python -m domains.rfic.tools_mcp --nodes-root <dir> [--node <name>] [--data-dir <dir>] [--call-log <file.jsonl>]`"""

import argparse
import os
import sys
from pathlib import Path

from app.jobs.store import JobStore, default_db_path
from domains.rfic.container import ContainerManager, default_nodes_root
from domains.rfic.tools_mcp.server import McpServer
from domains.rfic.tools_mcp.tools import RficTools


def main() -> None:
    ap = argparse.ArgumentParser(prog="rfic-tools-mcp")
    ap.add_argument("--nodes-root", default=os.environ.get("BLAHAJ_NODES_ROOT"), help="host dir mounted at /work/nodes")
    ap.add_argument("--node", default=os.environ.get("BLAHAJ_NODE"), help="pin to one node directory (agent sandbox)")
    ap.add_argument("--data-dir", default=None, help="BlahajSphere data dir (for the shared jobs DB)")
    ap.add_argument("--call-log", default=None, help="append one JSON line per tool call to this file")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["BLAHAJ_DATA_DIR"] = args.data_dir
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8", newline="\n")  # protocol is UTF-8 JSON lines, not the Windows codepage

    containers = ContainerManager(Path(args.nodes_root) if args.nodes_root else default_nodes_root())
    try:
        jobs: JobStore | None = JobStore(default_db_path())
    except Exception as e:  # noqa: BLE001 - simulations still work without UI job tracking
        print(f"jobs db unavailable, continuing without job tracking: {e}", file=sys.stderr)
        jobs = None
    McpServer(RficTools(containers, jobs, node=args.node), call_log=Path(args.call_log) if args.call_log else None).serve()


if __name__ == "__main__":
    main()
