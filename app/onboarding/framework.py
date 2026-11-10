"""Competency framework (SPEC §8.1): validated model, plus the draft -> frozen `competency-v1.yaml` files."""

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, field_validator, model_validator

MIN_DOMAINS, MAX_DOMAINS = 5, 8
TIERS = (1, 2, 3, 4, 5)
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
SUBSKILL = re.compile(r"^[a-z][a-z0-9_]*[.][a-z][a-z0-9_]*$")  # SPEC 5.1: matching.l_network, smith_chart.navigation
FROZEN_NAME = "competency-v1.yaml"
DRAFT_NAME = "competency-v1.draft.yaml"
HEADER = "# FROZEN competency framework v1 (SPEC §8.1). Changes need a user-approved version bump (competency-v2.yaml).\n"


class FrameworkDomain(BaseModel):
    id: str
    name: str
    scope: str = ""
    subskills: list[str] = []
    tiers: dict[int, str]

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"domain id {v!r} must be lowercase snake_case")
        return v

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("domain name is empty")
        return v.strip()

    @model_validator(mode="after")
    def _check(self) -> "FrameworkDomain":
        if set(self.tiers) != set(TIERS):
            raise ValueError(f"domain {self.id}: tiers must be exactly 1..5, got {sorted(self.tiers)}")
        for t, text in self.tiers.items():
            s = text.strip()
            if not s or "\n" in s:
                raise ValueError(f"domain {self.id} T{t}: descriptor must be one non-empty sentence")
            self.tiers[t] = s
        for s in self.subskills:
            if not SUBSKILL.match(s):
                raise ValueError(f"subskill {s!r} must look like 'group.name' (lowercase snake_case)")
        return self


class Framework(BaseModel):
    domain_pack: str
    version: int = 1
    domains: list[FrameworkDomain]

    @model_validator(mode="after")
    def _check(self) -> "Framework":
        if not MIN_DOMAINS <= len(self.domains) <= MAX_DOMAINS:
            raise ValueError(f"framework needs {MIN_DOMAINS}-{MAX_DOMAINS} domains, got {len(self.domains)}")
        ids = [d.id for d in self.domains]
        if len(set(ids)) != len(ids):
            raise ValueError("domain ids must be unique")
        self.version = 1
        return self


def validation_message(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())


class FrameworkStore:
    def __init__(self, framework_dir: Path):
        self.dir = Path(framework_dir)

    @property
    def frozen_path(self) -> Path:
        return self.dir / FROZEN_NAME

    @property
    def draft_path(self) -> Path:
        return self.dir / DRAFT_NAME

    def is_frozen(self) -> bool:
        return self.frozen_path.exists()

    def _dump(self, fw: Framework, header: str = "") -> str:
        return header + yaml.safe_dump(fw.model_dump(), sort_keys=False, allow_unicode=True, width=1000)

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    def load(self) -> Framework | None:
        """The frozen framework if approved, else the draft, else None."""
        p = self.frozen_path if self.is_frozen() else self.draft_path
        if not p.exists():
            return None
        return Framework.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))

    def save_draft(self, fw: Framework) -> None:
        self._write(self.draft_path, self._dump(fw))

    def freeze(self, fw: Framework) -> None:
        """Write competency-v1.yaml (never overwrites) and drop the draft."""
        if self.is_frozen():
            raise FileExistsError("framework v1 is already frozen")
        self._write(self.frozen_path, self._dump(fw, HEADER))
        self.draft_path.unlink(missing_ok=True)
