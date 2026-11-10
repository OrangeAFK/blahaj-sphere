"""Domain plugin seam (SPEC §2): the core app only knows this interface; domain code lives in domains/<name>/."""

import importlib
import os
from dataclasses import dataclass, field
from typing import Callable

from fastapi import APIRouter

from app.jobs.manager import JobManager
from app.jobs.store import Job
from app.preflight import Check

DEFAULT_DOMAIN = "rfic"


@dataclass
class Domain:
    name: str
    image: str
    image_size_gb: float  # on-disk size once extracted; drives the preflight disk check
    router: APIRouter | None = None
    checks: Callable[[], list[Check]] = lambda: []  # domain-specific preflight checks
    fixes: dict[str, Callable[[JobManager], Job]] = field(default_factory=dict)  # preflight fix_action -> starts a job


def load_domain(name: str | None = None) -> Domain | None:
    name = name or os.environ.get("BLAHAJ_DOMAIN", DEFAULT_DOMAIN)
    try:
        return importlib.import_module(f"domains.{name}.domain").DOMAIN
    except ModuleNotFoundError as e:
        if e.name and e.name.startswith(f"domains.{name}"):
            return None
        raise
