"""Onboarding endpoints (SPEC §9): interview, framework draft/approve, diagnostic. Sync handlers run in the threadpool."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.domains import DEFAULT_DOMAIN
from app.llm.router import Router
from app.llm.types import GradingUnavailable, LLMError
from app.onboarding.framework import Framework
from app.onboarding.service import Onboarding, OnboardingError
from app.tracks.scope import current_scope

router = APIRouter(prefix="/onboarding")


class OnboardingState(BaseModel):
    stage: str  # interview | framework | diagnostic | done
    goals: bool
    framework_frozen: bool
    diagnostic_done: bool


class InterviewView(BaseModel):
    done: bool
    questions_asked: int
    max_questions: int
    last_message: str | None
    goals: str | None


class InterviewTurn(BaseModel):
    answer: str | None = None


class GoalsBody(BaseModel):
    text: str


class FrameworkView(BaseModel):
    frozen: bool
    framework: Framework | None


class ApproveBody(BaseModel):
    framework: Framework | None = None  # the user's edited version; omitted = approve the saved draft


class ProbeView(BaseModel):
    domain: str
    tier: int
    subskill: str
    question: str


class ProbeResult(BaseModel):
    tier: int
    subskill: str
    score: int


class DiagnosticDomain(BaseModel):
    id: str
    name: str
    done: bool
    probes: list[ProbeResult]


class DiagnosticView(BaseModel):
    done: bool
    pending: ProbeView | None
    tiers: dict[str, int] | None
    domains: list[DiagnosticDomain]


class AnswerBody(BaseModel):
    answer: str


class AnswerResult(BaseModel):
    score: int
    view: DiagnosticView


def get_service(request: Request) -> Onboarding:
    """The Router and domain come from app.state so tests can inject the mock adapter."""
    llm = getattr(request.app.state, "llm_router", None)
    if llm is None:
        llm = request.app.state.llm_router = Router()
    domain = request.app.state.domain
    sc = current_scope()
    return Onboarding(
        sc.root, sc.track, llm, domain.name if domain else DEFAULT_DOMAIN,
        domain.framework_template if domain else None,
    )


def _call(fn, *args):
    try:
        return fn(*args)
    except OnboardingError as e:
        raise HTTPException(e.status, e.message) from None
    except GradingUnavailable as e:
        raise HTTPException(503, f"grading unavailable, try again later: {e}") from None
    except LLMError as e:
        raise HTTPException(502, str(e)) from None


@router.get("/state", response_model=OnboardingState, operation_id="getOnboardingState")
def get_state(request: Request) -> dict:
    return get_service(request).state()


@router.get("/interview", response_model=InterviewView, operation_id="getInterview")
def get_interview(request: Request) -> dict:
    return get_service(request).interview_view()


@router.post("/interview/turn", response_model=InterviewView, operation_id="interviewTurn")
def interview_turn(body: InterviewTurn, request: Request) -> dict:
    """No answer = ask the first question; otherwise submit the answer and get the next question (or the finished goals)."""
    return _call(get_service(request).interview_turn, body.answer)


@router.put("/goals", operation_id="putGoals")
def put_goals(body: GoalsBody, request: Request) -> dict:
    get_service(request).put_goals(body.text)
    return {"ok": True}


@router.get("/framework", response_model=FrameworkView, operation_id="getFramework")
def get_framework(request: Request) -> dict:
    return get_service(request).framework_view()


@router.post("/framework/draft", response_model=FrameworkView, operation_id="draftFramework")
def draft_framework(request: Request) -> dict:
    return _call(get_service(request).draft_framework)


@router.put("/framework/draft", response_model=FrameworkView, operation_id="saveFrameworkDraft")
def save_framework_draft(body: Framework, request: Request) -> dict:
    return _call(get_service(request).save_draft, body.model_dump())


@router.post("/framework/approve", response_model=FrameworkView, operation_id="approveFramework")
def approve_framework(body: ApproveBody, request: Request) -> dict:
    raw = body.framework.model_dump() if body.framework else None
    return _call(get_service(request).approve_framework, raw)


@router.get("/diagnostic", response_model=DiagnosticView, operation_id="getDiagnostic")
def get_diagnostic(request: Request) -> dict:
    return _call(get_service(request).diagnostic_view)


@router.post("/diagnostic/next", response_model=DiagnosticView, operation_id="diagnosticNext")
def diagnostic_next(request: Request) -> dict:
    return _call(get_service(request).diagnostic_next)


@router.post("/diagnostic/answer", response_model=AnswerResult, operation_id="diagnosticAnswer")
def diagnostic_answer(body: AnswerBody, request: Request) -> dict:
    return _call(get_service(request).diagnostic_answer, body.answer)
