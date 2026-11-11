"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), SPEC §6.2.

Hand-rolled instead of the `mcp` SDK: the protocol subset needed (initialize, ping, tools/list,
tools/call) is tiny and this avoids a dependency outside SPEC §2. Requests are handled one at a
time; stdout carries protocol messages only (logs go to stderr).
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO

from domains.rfic.tools_mcp.tools import TOOL_ERRORS, RficTools

SERVER_NAME = "rfic-tools"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

_PATH = "Path relative to the node directory (e.g. workspace/amp.sp)."

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "run_ngspice",
        "description": "Run ngspice in batch mode (`ngspice -b -o <netlist>.log`) inside the RFIC container, cwd = the node workspace. "
        "Returns exit code, the log tail, detected errors and files changed. Set `pdk` (e.g. sky130A, ihp-sg13g2) to select the PDK environment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "netlist": {"type": "string", "description": _PATH},
                "pdk": {"type": "string", "description": "PDK to activate, e.g. sky130A or ihp-sg13g2 (optional)."},
                "timeout_min": {"type": "number", "description": "Timeout in minutes (default 10)."},
            },
            "required": ["netlist"],
        },
        "method": "run_ngspice",
    },
    {
        "name": "run_openems",
        "description": "Run an openEMS Python script (`python3 script.py`) inside the container, cwd = the node workspace. Can be slow; set timeout_min.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": _PATH},
                "timeout_min": {"type": "number", "description": "Timeout in minutes (default 15)."},
            },
            "required": ["script"],
        },
        "method": "run_openems",
    },
    {
        "name": "run_python",
        "description": "Run a Python 3 script in the container (numpy, scikit-rf, matplotlib with a headless backend), cwd = the node workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _PATH},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Command-line arguments."},
                "timeout_min": {"type": "number", "description": "Timeout in minutes (default 10)."},
            },
            "required": ["path"],
        },
        "method": "run_python",
    },
    {
        "name": "run_checker",
        "description": "Run the node's checker.py (`--work <dir> --out <json>`, node PDK env) against the learner `workspace` or the "
        "`reference` (_hidden/reference_solution). Returns verdict pass|fail|error (exit 0/1/2), the parsed result JSON, and output tails.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node directory name (optional when the server is pinned to a node)."},
                "target": {"type": "string", "enum": ["workspace", "reference"]},
                "timeout_min": {"type": "number"},
            },
            "required": ["target"],
        },
        "method": "run_checker",
    },
    {
        "name": "parse_touchstone",
        "description": "Parse a Touchstone (.sNp) file with scikit-rf. Returns f_hz and S[freq][row][col]=[re,im], evenly decimated to max_points.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _PATH},
                "max_points": {"type": "integer", "description": "Max frequency points returned (default 500)."},
            },
            "required": ["path"],
        },
        "method": "parse_touchstone",
    },
    {
        "name": "list_pdk_models",
        "description": "List a PDK's ngspice library files (with their `.lib` sections, for `.lib <path> <section>`) and device subckts/models. "
        "PDKs: sky130A, ihp-sg13g2 (also gf180mcuD, ihp-sg13cmos5l). Use `pattern` to filter device names.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdk": {"type": "string"},
                "pattern": {"type": "string", "description": "Case-insensitive substring filter on device names."},
                "limit": {"type": "integer", "description": "Max devices returned (default 150)."},
            },
            "required": ["pdk"],
        },
        "method": "list_pdk_models",
    },
    {
        "name": "write_file",
        "description": "Write a UTF-8 text file (LF line endings) inside the node directory; creates parent directories, overwrites existing files.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": _PATH}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        "method": "write_file",
    },
    {
        "name": "delete_file",
        "description": "Delete a file inside the node directory (scratch files, stale simulation outputs). Directories need recursive=true. "
        "The node dir, workspace/, _hidden/ and _hidden/reference_solution/ themselves cannot be deleted.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": _PATH}, "recursive": {"type": "boolean", "description": "Delete a directory tree."}},
            "required": ["path"],
        },
        "method": "delete_file",
    },
    {
        "name": "read_file",
        "description": "Read a text file inside the node directory (up to 200 kB).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": _PATH}, "max_bytes": {"type": "integer"}},
            "required": ["path"],
        },
        "method": "read_file",
    },
]
_BY_NAME = {t["name"]: t for t in TOOL_SPECS}


class McpServer:
    def __init__(self, tools: RficTools, log: Callable[[str], None] | None = None, call_log: Path | None = None):
        self.tools = tools
        self._log = log or (lambda s: print(s, file=sys.stderr, flush=True))
        self.call_log = Path(call_log) if call_log else None  # JSONL audit of tool calls (Builder records, DECISIONS M5)

    # ---- protocol ----

    def handle(self, msg: dict) -> dict | None:
        """Handle one JSON-RPC message; return the response, or None for notifications."""
        method, mid = msg.get("method"), msg.get("id")
        is_request = "id" in msg and method is not None
        if method is None:  # a response to something we never sent
            return None
        try:
            result = self._dispatch(method, msg.get("params") or {})
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}} if is_request else None
        except Exception as e:  # noqa: BLE001 - a server bug must not kill the stdio loop
            self._log(f"internal error in {method}: {e!r}")
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal error: {e}"}} if is_request else None
        return {"jsonrpc": "2.0", "id": mid, "result": result} if is_request else None

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return {
                "protocolVersion": wanted if wanted in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{k: v for k, v in t.items() if k != "method"} for t in TOOL_SPECS]}
        if method == "tools/call":
            return self._call(params.get("name"), params.get("arguments") or {})
        if method.startswith("notifications/"):
            return {}
        raise _RpcError(-32601, f"method not found: {method}")

    def _call(self, name: str | None, args: dict) -> dict:
        spec = _BY_NAME.get(name or "")
        if spec is None:
            raise _RpcError(-32602, f"unknown tool: {name}")
        allowed = set(spec["inputSchema"]["properties"])
        unknown = set(args) - allowed
        if unknown:
            return _text({"error": f"unexpected arguments: {sorted(unknown)}; allowed: {sorted(allowed)}"}, is_error=True)
        t0 = time.monotonic()
        try:
            out = getattr(self.tools, spec["method"])(**args)
        except TOOL_ERRORS as e:
            self._audit(name, args, t0, str(e))
            return _text({"error": str(e)}, is_error=True)
        except TypeError as e:  # missing/ill-typed arguments
            self._audit(name, args, t0, f"bad arguments: {e}")
            return _text({"error": f"bad arguments: {e}"}, is_error=True)
        self._audit(name, args, t0, None)
        return _text(out, is_error=False)

    def _audit(self, name: str | None, args: dict, t0: float, error: str | None) -> None:
        """One JSONL line per tool call: arguments without file contents. Never breaks the call."""
        if self.call_log is None:
            return
        brief = {k: (f"<{len(v)} chars>" if k == "content" and isinstance(v, str) else v) for k, v in args.items()}
        line = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "tool": name, "args": brief,
                "is_error": error is not None, "error": (error or "")[:300] or None, "seconds": round(time.monotonic() - t0, 2)}
        try:
            self.call_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.call_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            self._log(f"call log write failed: {e}")

    # ---- stdio loop ----

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for raw in stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                resp: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                if isinstance(msg, list):  # batches were removed from MCP; reject rather than guess
                    resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batch requests unsupported"}}
                else:
                    resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _text(payload: Any, *, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1, ensure_ascii=False)}], "isError": is_error}
