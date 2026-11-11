"""Domain plugin seam (SPEC §2): the core app only knows this interface; domain code lives in domains/<name>/."""

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import APIRouter

from app.jobs.manager import JobHandle, JobManager
from app.jobs.store import Job
from app.preflight import Check

DEFAULT_DOMAIN = "rfic"


@dataclass
class BuilderHooks:
    """What the generic Builder (app/builder/service.py) needs from a domain to run a heavy agent with its tools."""

    prompt: Path  # domains/<d>/prompts/builder.md
    tool_names: Callable[[], list[str]]  # allow-list passed to the agent (e.g. mcp__rfic__*)
    # (node_dir, config_out_path, call_log_path) -> path of the written --mcp-config file, sandboxed to that node
    mcp_config: Callable[[Path, Path, Path], Path]


@dataclass
class Domain:
    name: str
    image: str
    image_size_gb: float  # on-disk size once extracted; drives the preflight disk check
    router: APIRouter | None = None
    checks: Callable[[], list[Check]] = lambda: []  # domain-specific preflight checks
    fixes: dict[str, Callable[[JobManager], Job]] = field(default_factory=dict)  # preflight fix_action -> starts a job
    # Release-gate checker runner (app/builder/gate.py): (node_dir, work_rel, out_rel, timeout_s) -> run dict.
    run_checker: Callable[[Path, str, str, float], dict] | None = None
    # Lab "Run simulation": (node_dir, commands, per-command timeout_s, job handle) -> {ok, steps, files_changed}.
    run_commands: Callable[[Path, list[str], float, JobHandle | None], dict] | None = None
    # Default competency framework (SPEC §8.1) that onboarding's framework drafter adapts to the user's goals.
    framework_template: Path | None = None
    builder: BuilderHooks | None = None


def load_domain(name: str | None = None) -> Domain | None:
    name = name or os.environ.get("BLAHAJ_DOMAIN", DEFAULT_DOMAIN)
    try:
        return importlib.import_module(f"domains.{name}.domain").DOMAIN
    except ModuleNotFoundError as e:
        if e.name and e.name.startswith(f"domains.{name}"):
            return None
        raise
