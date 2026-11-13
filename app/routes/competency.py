"""Competency screen + tier exams (SPEC §8.4, §11) for the ACTIVE track."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.domains import DEFAULT_DOMAIN
from app.lifecycle.exam import TierExams
from app.lifecycle.nodes import LifecycleError, NodeStore
from app.routes.builder import _llm
from app.tracks.scope import current_scope

router = APIRouter()


class TierDescriptor(BaseModel):
    tier: int
    descriptor: str
    state: str  # passed | current | locked


class UnlockView(BaseModel):
    mastery_needed: float
    subskills_needed: int
    subskills_evidenced: int
    mastery_ok: bool
    subskills_ok: bool


class SubskillView(BaseModel):
    name: str
    evidenced: bool  # evidence at the current tier


class ExamAttempt(BaseModel):
    node_id: int
    dir: str
    tier: int | None = None
    status: str
    passed: bool | None = None
    mean: float | None = None
    tier_up: bool


class TierUp(BaseModel):
    tier: int
    via: str


class ExamView(BaseModel):
    state: str  # locked | available | in_progress | max_tier
    missing: list[str]  # what is still needed (locked / max_tier)
    node_id: int | None = None  # the exam in progress
    pass_mean: float
    questions: int
    attempts: list[ExamAttempt]
    tier_ups: list[TierUp]


class DomainCompetency(BaseModel):
    domain: str
    name: str
    scope: str
    tier: int
    mastery: float
    n_window: int
    tiers: list[TierDescriptor]
    unlock: UnlockView
    subskills: list[SubskillView]
    exam: ExamView


class CompetencyView(BaseModel):
    track: str
    domains: list[DomainCompetency]


class ExamStarted(BaseModel):
    node_id: int
    dir: str
    reused: bool  # an exam of that domain was already in progress


def _exams(request: Request) -> TierExams:
    sc = current_scope()
    domain = request.app.state.domain
    return TierExams(NodeStore(sc.root, sc.track), _llm(request), domain.name if domain else DEFAULT_DOMAIN)


@router.get("/competency", response_model=CompetencyView, operation_id="getCompetency")
def get_competency(request: Request) -> dict:
    """Domains x tiers of the ACTIVE track: descriptors, mastery, the unlock condition, subskills and the tier-exam state."""
    try:
        return _exams(request).competency()
    except LifecycleError as e:
        raise HTTPException(e.status, e.message) from None


@router.post("/competency/{domain}/exam", response_model=ExamStarted, operation_id="startTierExam")
def start_exam(domain: str, request: Request) -> dict:
    """Create the tier-exam node of an unlocked domain (heavy call writes its 6 questions); an exam already in progress is returned instead."""
    try:
        return _exams(request).start(domain)
    except LifecycleError as e:
        raise HTTPException(e.status, e.message) from None
