import json
import os
from pathlib import Path

from app.llm.types import LLMError, Request, Result, RetryableError

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm"


class MockAdapter:
    """Canned fixtures from tests/fixtures/llm/<task_type>/*.json.

    A fixture is {"text": str, "json": any, "usage": {...}} or {"error": "retryable"|"fatal", "message": str}.
    A model-specific file `<model-slug>.json` wins; otherwise the first fixture (sorted) is returned.
    """

    name = "mock"

    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.environ.get("BLAHAJ_LLM_FIXTURES", FIXTURES))
        self.calls: list[tuple[str, str]] = []

    def complete(self, req: Request, model: str, timeout_s: float) -> Result:
        self.calls.append((req.task_type, model))
        d = self.root / req.task_type
        slug = model.replace("/", "_").replace(":", "_")
        path = d / f"{slug}.json"
        if not path.exists():
            files = sorted(d.glob("*.json")) if d.exists() else []
            if not files:
                raise LLMError(f"no mock fixture for task_type {req.task_type!r} in {d}")
            path = files[0]
        fx = json.loads(path.read_text(encoding="utf-8"))
        if "error" in fx:
            cls = RetryableError if fx["error"] == "retryable" else LLMError
            raise cls(fx.get("message", "mock error"))
        text = fx.get("text", json.dumps(fx.get("json")))
        return Result(text=text, json=fx.get("json"), usage=fx.get("usage", {}), backend=self.name, model=model)
