from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Non-retryable adapter failure."""


class RetryableError(LLMError):
    """429 / 5xx / timeout: the router may try the next chain entry."""


class ModelNotAllowed(LLMError):
    """Model ID rejected by the free-only allowlist."""


class GradingUnavailable(LLMError):
    """Grading backend failed; the job was queued (SPEC §3.1)."""


@dataclass
class Request:
    task_type: str
    messages: list[dict[str, str]]
    schema: dict | None = None
    cwd: str | None = None
    tools: list[str] | None = None
    node_id: int | None = None
    mcp_config: str | None = None
    light: bool = False  # light-tier task: claude_code runs with a replaced system prompt, no tools, no MCP


@dataclass
class Result:
    text: str
    json: Any
    usage: dict
    backend: str
    model: str
    latency_ms: int = 0
    attempts: list[dict] = field(default_factory=list)
