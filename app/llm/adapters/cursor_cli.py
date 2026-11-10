import shutil
import subprocess
import time

from app.llm.adapters.claude_code import clean_env
from app.llm.adapters.util import flatten_messages, parse_json
from app.llm.types import LLMError, Request, Result, RetryableError

# DECISIONS M0: CLI binary is `agent` (docs); older installs use `cursor-agent`. Flags unverified live.
BINARIES = ("agent", "cursor-agent")


class CursorCliAdapter:
    name = "cursor_cli"

    def __init__(self, binary: str | None = None):
        self.binary = binary

    def find_binary(self) -> str:
        if self.binary:
            return self.binary
        for b in BINARIES:
            p = shutil.which(b)
            if p:
                return p
        raise LLMError("cursor agent CLI not found (tried: agent, cursor-agent)")

    def complete(self, req: Request, model: str, timeout_s: float) -> Result:
        cmd = [self.find_binary(), "-p", flatten_messages(req.messages, req.schema), "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        t0 = time.monotonic()
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", cwd=req.cwd,
                env=clean_env(), timeout=timeout_s, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            raise RetryableError(f"cursor agent timed out after {timeout_s}s") from e
        if p.returncode != 0:
            raise LLMError(f"cursor agent exit {p.returncode}: {(p.stderr or p.stdout).strip()[:500]}")
        text = p.stdout.strip()
        return Result(
            text=text, json=parse_json(text) if req.schema is not None else None, usage={},
            backend=self.name, model=model or "default", latency_ms=int((time.monotonic() - t0) * 1000),
        )
