"""Build the `--mcp-config` JSON that lets a heavy agent (`claude -p`) use the RFIC tools."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_KEY = "rfic"  # tools appear to the agent as mcp__rfic__<tool>


def tool_names() -> list[str]:
    from domains.rfic.tools_mcp.server import TOOL_SPECS

    return [f"mcp__{SERVER_KEY}__{t['name']}" for t in TOOL_SPECS]


def mcp_config(nodes_root: Path, node: str | None, data_dir: Path | None = None, call_log: Path | None = None) -> dict:
    args = ["-X", "utf8", "-m", "domains.rfic.tools_mcp", "--nodes-root", str(nodes_root)]
    if node:
        args += ["--node", node]
    if data_dir:
        args += ["--data-dir", str(data_dir)]
    if call_log:
        args += ["--call-log", str(call_log)]
    return {
        "mcpServers": {
            SERVER_KEY: {"command": sys.executable, "args": args, "env": {"PYTHONPATH": str(REPO_ROOT)}},
        }
    }


def write_mcp_config(path: Path, nodes_root: Path, node: str | None, data_dir: Path | None = None, call_log: Path | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mcp_config(nodes_root, node, data_dir, call_log), indent=2), encoding="utf-8")
    return path
