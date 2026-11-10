import json
import os
import shutil
import subprocess
import time

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

_RATE_HINTS = ("rate limit", "usage limit", "limit reached", "overloaded", "429", "529")


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if k not in STRIPPED_ENV}


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(self, binary: str | None = None, default_model=lambda: ""):
        self.binary = binary
        self.default_model = default_model

    def build_cmd(self, req: Request, model: str) -> list[str]:
        exe = self.binary or shutil.which("claude")
        if not exe:
            raise LLMError("claude CLI not found on PATH")
        cmd = [exe, "-p", "--output-format", "json", "--permission-mode", "dontAsk"]
        model = model or self.default_model()
        if model:
            cmd += ["--model", model]
        if req.mcp_config:
            cmd += ["--mcp-config", req.mcp_config, "--strict-mcp-config"]
        if req.tools:
            cmd += ["--allowedTools", ",".join(req.tools)]
        return cmd

    def complete(self, req: Request, model: str, timeout_s: float) -> Result:
        cmd = self.build_cmd(req, model)
        prompt = flatten_messages(req.messages, req.schema)
        t0 = time.monotonic()
        try:
            p = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                cwd=req.cwd, env=clean_env(), timeout=timeout_s,
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
            usage={"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")},
            backend=self.name, model=used_model, latency_ms=int((time.monotonic() - t0) * 1000),
        )
