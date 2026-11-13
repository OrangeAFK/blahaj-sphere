"""Planner options (SPEC §7.7, minimal) and Builder jobs (SPEC §7.1). Sync handlers run in the threadpool."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.builder.service import BuildError, BuildService
from app.domains import DEFAULT_DOMAIN
from app.jobs.deps import get_running_manager
from app.jobs.manager import JobManager
from app.llm.router import Router
from app.llm.types import LLMError
from app.planner.service import Planner, PlannerError
from app.tracks.scope import current_scope

router = APIRouter()
_NODE_DIR = re.compile(r"^\d{4,}-[a-z0-9-]+$")


class BranchOption(BaseModel):
    """SPEC §5.6"""

    title: str
    why_now: str
    kind: str
    domain: str
    subskills: list[str]
    tier: int
    size_est_min: int


class PlannerOptions(BaseModel):
    options: list[BranchOption]
    model: str
    ts: str
    difficulty: dict | None = None  # {mode, success_rate, rated_nodes} the options were planned under


class BuildBody(BaseModel):
    option_index: int


class InterleavingTargets(BaseModel):
    open_misconceptions: list[dict]
    stale_subskills: list[str]


class BuildStarted(BaseModel):
    job_id: str
    node_id: int
    node_dir: str
    interleaving_targets: InterleavingTargets


def _llm(request: Request) -> Router:
    llm = getattr(request.app.state, "llm_router", None)
    if llm is None:
        llm = request.app.state.llm_router = Router()
    return llm


def _planner(request: Request) -> Planner:
    domain = request.app.state.domain
    sc = current_scope()
    return Planner(sc.root, sc.track, _llm(request), domain.name if domain else DEFAULT_DOMAIN)


def _builder(request: Request) -> BuildService:
    domain = request.app.state.domain
    if domain is None:
        raise HTTPException(501, "no active domain")
    try:
        sc = current_scope()
        return BuildService(sc.root, sc.track, _llm(request), domain)
    except BuildError as e:
        raise HTTPException(e.status, e.message) from None


@router.post("/planner/options", response_model=PlannerOptions, operation_id="generatePlannerOptions")
def generate_options(request: Request) -> dict:
    """Heavy LLM call: 2-4 next-node options from goals, scores and history; stored for POST /build."""
    try:
        return _planner(request).generate()
    except PlannerError as e:
        raise HTTPException(e.status, e.message) from None
    except LLMError as e:
        raise HTTPException(502, str(e)) from None


@router.get("/planner/difficulty", operation_id="getPlannerDifficulty")
def get_difficulty(request: Request) -> dict:
    """The difficulty-control state the next Planner call will use (SPEC §7.7): mode, rolling success rate over the last 8 challenge nodes, sizing hints."""
    try:
        inp = _planner(request).planner_input()
    except PlannerError as e:
        raise HTTPException(e.status, e.message) from None
    return {"difficulty": inp["difficulty"], "sizing": inp["sizing"]}


@router.get("/planner/options", response_model=PlannerOptions, operation_id="getPlannerOptions")
def get_options(request: Request) -> dict:
    out = _planner(request).last_options()
    if out is None:
        raise HTTPException(404, "no planner options yet")
    return out


@router.post("/build", response_model=BuildStarted, status_code=202, operation_id="startBuild")
def start_build(body: BuildBody, request: Request, m: Annotated[JobManager, Depends(get_running_manager)]) -> dict:
    """Build the chosen option as a background `build_challenge` job (progress via /api/jobs/stream)."""
    opts = _planner(request).last_options()
    if opts is None:
        raise HTTPException(409, "generate planner options first")
    if not 0 <= body.option_index < len(opts["options"]):
        raise HTTPException(422, f"option_index must be 0..{len(opts['options']) - 1}")
    option = opts["options"][body.option_index]
    if option.get("kind") == "tier_exam":  # offered by code (§8.4); an exam is not built, it is written and starts at its oral
        raise HTTPException(422, f"option {body.option_index} is a tier exam: start it with POST /api/competency/{option['domain']}/exam")
    job, plan = _builder(request).start(m, option)
    return {"job_id": job.id, "node_id": plan.node_id, "node_dir": plan.name,
            "interleaving_targets": {"open_misconceptions": plan.ctx.open_misconceptions, "stale_subskills": plan.ctx.stale_subskills}}  # fmt: skip


@router.get("/build/{node_dir}", operation_id="getBuildRecord")
def get_build(node_dir: str, request: Request) -> dict:
    """Build record: attempts, agent usage, tool calls, gate results (no answer-key content)."""
    if not _NODE_DIR.match(node_dir):
        raise HTTPException(422, "bad node dir")
    rec = _builder(request).load_record(node_dir)
    if rec is None:
        raise HTTPException(404, "no build record")
    return rec
