import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app.llm.adapters.util import flatten_messages, parse_json
from app.llm.types import LLMError, Request, Result, RetryableError

# Must not reach the subprocess: they override the subscription login (DECISIONS M0).
STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_BEARER_TOKEN_BEDROCK",
)

# `dontAsk` still lets read-only built-ins run without being allow-listed, which gave the Builder
# read access to the host filesystem (DECISIONS M2.5). Agents reach files only through MCP tools.
DISALLOWED_TOOLS = ("Glob", "Grep", "Read")

_RATE_HINTS =("rate limit", "usage limit", "limit reached", "overloaded", "429", "529")


def _usage(u: dict) -> dict:
    """`input_tokens` is the TOTAL prompt (fresh + cache writes + cache reads); the raw CLI figure alone is ~2 for a cached prompt."""
    cc, cr = u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0
    fresh = u.get("input_tokens")
    return {
        "input_tokens": None if fresh is None else fresh + cc + cr,
        "output_tokens": u.get("output_tokens"),
        "cache_creation_tokens": cc,
        "cache_read_tokens": cr,
    }


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if k not in STRIPPED_ENV}


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(self, binary: str | None = None, default_model=lambda: ""):
        self.binary = binary
        self.default_model = default_model

    def build_cmd(self, req: Request, model: str, system_file: str | None = None) -> list[str]:
        exe = self.binary or shutil.which("claude")
        if not exe:
            raise LLMError("claude CLI not found on PATH")
        if req.light:
            return self._light_cmd(exe, model, system_file)
        cmd = [exe, "-p", "--output-format", "json", "--permission-mode", "dontAsk", "--disallowedTools", ",".join(DISALLOWED_TOOLS)]
        if req.tools and set(req.tools) & set(DISALLOWED_TOOLS):
            raise LLMError(f"tools {sorted(set(req.tools) & set(DISALLOWED_TOOLS))} are always disallowed for claude_code")
        model = model or self.default_model()
        if model:
            cmd += ["--model", model]
        if req.mcp_config:
            cmd += ["--mcp-config", req.mcp_config, "--strict-mcp-config"]
        if req.tools:
            cmd += ["--allowedTools", ",".join(req.tools)]
        return cmd

    def _light_cmd(self, exe: str, model: str, system_file: str | None) -> list[str]:
        """Minimum-overhead call for light-tier tasks: replaced system prompt, no tools, no MCP, no skills/settings/session."""
        cmd = [
            exe, "-p", "--output-format", "json", "--permission-mode", "dontAsk",
            "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--setting-sources", "",
        ]
        model = model or self.default_model()
        if model:
            cmd += ["--model", model]
        if system_file:
            cmd += ["--system-prompt-file", system_file]  # a file: multi-line args do not survive the Windows .cmd shim
        return cmd

    def complete(self, req: Request, model: str, timeout_s: float) -> Result:
        if req.light:
            return self._complete_light(req, model, timeout_s)
        return self._run(self.build_cmd(req, model), flatten_messages(req.messages, req.schema), req, model, timeout_s)

    def _complete_light(self, req: Request, model: str, timeout_s: float) -> Result:
        system = "\n\n".join(m["content"] for m in req.messages if m.get("role") == "system")
        rest = [m for m in req.messages if m.get("role") != "system"]
        with tempfile.TemporaryDirectory(prefix="blahaj-light-") as d:  # empty cwd: no CLAUDE.md / project discovery
            sf = None
            if system:
                sf = str(Path(d) / "system.txt")
                Path(sf).write_text(system, encoding="utf-8", newline="\n")
            cmd = self.build_cmd(req, model, sf)
            return self._run(cmd, flatten_messages(rest, req.schema), req, model, timeout_s, cwd=d)

    def _run(self, cmd: list[str], prompt: str, req: Request, model: str, timeout_s: float, cwd: str | None = None) -> Result:
        t0 = time.monotonic()
        try:
            p = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                cwd=cwd or req.cwd, env=clean_env(), timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise RetryableError(f"claude timed out after {timeout_s}s") from e
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            msg = (p.stderr or p.stdout or "no output").strip()[:500]
            raise LLMError(f"claude exit {p.returncode}, unparseable output: {msg}")
        text = out.get("result") or ""
        if out.get("is_error") or p.returncode != 0:
            low = text.lower()
            cls = RetryableError if any(h in low for h in _RATE_HINTS) else LLMError
            raise cls(f"claude error: {text[:500]}")
        usage = out.get("usage") or {}
        used_model = next(iter(out.get("modelUsage") or {}), model or "default")
        return Result(
            text=text, json=parse_json(text) if req.schema is not None else None,
            usage=_usage(usage),
            backend=self.name, model=used_model, latency_ms=int((time.monotonic() - t0) * 1000),
        )
