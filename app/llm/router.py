import json
import time
from dataclasses import asdict
from pathlib import Path

from app.llm.adapters import Adapter
from app.llm.adapters.claude_code import ClaudeCodeAdapter
from app.llm.adapters.cursor_cli import CursorCliAdapter
from app.llm.adapters.mock import MockAdapter
from app.llm.adapters.openrouter import OpenRouterAdapter
from app.llm.allowlist import check_free_model
from app.llm.config import RoutingConfig, load_routing
from app.llm.store import Store
from app.llm.types import GradingUnavailable, LLMError, ModelNotAllowed, RateLimited, Request, Result, RetryableError
from app.settings import load_settings, settings_path


def default_adapters() -> dict[str, Adapter]:
    return {
        "claude_code": ClaudeCodeAdapter(default_model=lambda: load_settings().claude_model),
        "cursor_cli": CursorCliAdapter(),
        "openrouter": OpenRouterAdapter(api_key=lambda: load_settings().openrouter_api_key),
        "mock": MockAdapter(),
    }


class Router:
    def __init__(
        self,
        config: RoutingConfig | None = None,
        store: Store | None = None,
        adapters: dict[str, Adapter] | None = None,
        allow_library_to_free=lambda: load_settings().allow_library_to_free_models,
        clock=time.time,
    ):
        self.config = config or load_routing()
        self.store = store or Store(settings_path().parent / "llm.db")
        self.adapters = adapters if adapters is not None else default_adapters()
        self.allow_library_to_free = allow_library_to_free
        self.clock = clock

    def complete(
        self, task_type, messages, schema=None, cwd=None, tools=None, *,
        node_id=None, mcp_config=None, library_text=False,
    ) -> Result:
        route = self.config.tasks.get(task_type)
        req = Request(task_type, messages, schema, cwd, tools, node_id, mcp_config, light=bool(route and route.light))
        return self._run(req, library_text)

    def _run(self, req: Request, library_text: bool = False, queue: bool = True) -> Result:
        route = self.config.tasks.get(req.task_type)
        if route is None:
            raise LLMError(f"no routing for task_type {req.task_type!r}")
        errors: list[str] = []
        for e in route.chain:
            if e.backend == "openrouter":
                check_free_model(e.model)  # reject paid IDs before anything is counted or sent
            if library_text and e.backend not in ("claude_code", "mock") and not self.allow_library_to_free():
                errors.append(f"{e.backend}:{e.model} skipped (library text not allowed)")
                continue
            if e.backend == "openrouter" and self.store.cooldown_until(e.backend, e.model) > self.clock():
                errors.append(f"{e.backend}:{e.model} skipped (cooling down after 429)")
                continue
            if self._capped(e):
                errors.append(f"{e.backend}:{e.model} skipped (daily cap)")
                continue
            adapter = self.adapters.get(e.backend)
            if adapter is None:
                errors.append(f"{e.backend}: no adapter")
                continue
            self.store.increment(e.backend, e.model)
            try:
                res = adapter.complete(req, e.model, self.config.timeout(e.backend, req.task_type))
            except RetryableError as ex:
                if isinstance(ex, RateLimited) and e.backend == "openrouter":
                    self.store.set_cooldown(e.backend, e.model, self.clock() + self.config.rate_limit_cooldown_s)
                self._log(req, e, "retryable_error", error=str(ex))
                errors.append(f"{e.backend}:{e.model}: {ex}")
                continue
            except LLMError as ex:
                self._log(req, e, "error", error=str(ex))
                if route.grading and queue and not isinstance(ex, ModelNotAllowed):
                    self._queue(req, str(ex))
                raise
            res.backend = e.backend
            self._log(req, e, "ok", usage=res.usage, latency_ms=res.latency_ms, model=res.model)
            return res
        msg = f"{req.task_type}: all backends failed or skipped: " + "; ".join(errors)
        if route.grading:
            if queue:
                self._queue(req, msg)
            raise GradingUnavailable(msg)
        raise LLMError(msg)

    def _capped(self, e) -> bool:
        cap = self.config.cap_for(e)
        if cap is not None and self.store.count(e.backend, e.model) >= cap:
            return True
        bcap = self.config.backend_daily_caps.get(e.backend)
        return bcap is not None and self.store.backend_count(e.backend) >= bcap

    def _log(self, req: Request, e, status, *, error=None, usage=None, latency_ms=None, model=None):
        self.store.log_call(
            task_type=req.task_type, backend=e.backend, model=model or e.model or "default", status=status,
            error=error, usage=usage, latency_ms=latency_ms, node_id=req.node_id,
        )

    def _queue(self, req: Request, error: str) -> None:
        self.store.enqueue_grade(req.task_type, asdict(req), req.node_id, error)

    # --- grading queue (SPEC §3.1): retried with backoff and on app start ----
    def retry_pending_grades(self, handler=None, now: float | None = None) -> list[tuple[int, Result]]:
        """Retry due grades. `handler(row, result)` receives successes (M6 will write events)."""
        done = []
        for row in self.store.due_grades(now):
            req = Request(**json.loads(row["request"]))
            try:
                res = self._run(req, queue=False)
            except LLMError as ex:
                self.store.grade_failed(row["id"], str(ex))
                continue
            self.store.grade_done(row["id"])
            if handler:
                handler(row, res)
            done.append((row["id"], res))
        return done
