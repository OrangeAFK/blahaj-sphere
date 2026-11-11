from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.llm.allowlist import check_free_model

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "routing.yaml"
BACKENDS = {"claude_code", "cursor_cli", "openrouter", "mock"}


@dataclass
class Entry:
    backend: str
    model: str = ""
    daily_cap: int | None = None


@dataclass
class TaskRoute:
    chain: list[Entry]
    grading: bool = False
    light: bool = False
    timeout_s: float | None = None  # overrides the backend timeout for this task (long Builder runs, DECISIONS M5)


@dataclass
class RoutingConfig:
    tasks: dict[str, TaskRoute]
    timeout_s: dict[str, float] = field(default_factory=dict)
    backend_daily_caps: dict[str, int] = field(default_factory=dict)
    default_daily_cap: int = 50
    rate_limit_cooldown_s: float = 1800  # a free model that returned 429 is skipped this long

    def timeout(self, backend: str, task_type: str | None = None) -> float:
        route = self.tasks.get(task_type) if task_type else None
        if route is not None and route.timeout_s is not None:
            return route.timeout_s
        return self.timeout_s.get(backend, 900)

    def cap_for(self, e: Entry) -> int | None:
        if e.backend != "openrouter":
            return e.daily_cap
        return e.daily_cap if e.daily_cap is not None else self.default_daily_cap


def parse_routing(data: dict) -> RoutingConfig:
    tasks: dict[str, TaskRoute] = {}
    for name, t in (data.get("tasks") or {}).items():
        chain = []
        for raw in t.get("chain") or []:
            e = Entry(raw["backend"], str(raw.get("model") or ""), raw.get("daily_cap"))
            if e.backend not in BACKENDS:
                raise ValueError(f"routing: task {name!r}: unknown backend {e.backend!r}")
            if e.backend == "openrouter":
                check_free_model(e.model)  # paid IDs are rejected at load time
            chain.append(e)
        if not chain:
            raise ValueError(f"routing: task {name!r} has an empty chain")
        to = t.get("timeout_s")
        tasks[name] = TaskRoute(chain, bool(t.get("grading", False)), bool(t.get("light", False)), None if to is None else float(to))
    return RoutingConfig(
        tasks=tasks,
        timeout_s={k: float(v) for k, v in (data.get("timeout_s") or {}).items()},
        backend_daily_caps={k: int(v) for k, v in (data.get("backend_daily_caps") or {}).items()},
        default_daily_cap=int(data.get("default_daily_cap", 50)),
        rate_limit_cooldown_s=float(data.get("rate_limit_cooldown_s", 1800)),
    )


def load_routing(path: Path | None = None) -> RoutingConfig:
    p = Path(path or DEFAULT_PATH)
    return parse_routing(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
