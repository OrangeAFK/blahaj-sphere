"""Node lifecycle endpoints (SPEC §7.2-7.7): prelab, lab, tutor, proof, oral stub, debrief, rescope, grading queue.
Sync handlers run in the threadpool."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.jobs.deps import get_running_manager
from app.jobs.manager import JobManager
from app.lifecycle.nodes import LifecycleError
from app.lifecycle.service import Lifecycle
from app.llm.types import LLMError
from app.routes.builder import _llm
from app.settings import data_dir

router = APIRouter()
Jobs = Annotated[JobManager, Depends(get_running_manager)]


def _lc(request: Request) -> Lifecycle:
    try:
        return Lifecycle(data_dir(), _llm(request), request.app.state.domain)
    except LifecycleError as e:
        raise HTTPException(e.status, e.message) from None


def _call(fn):
    try:
        return fn()
    except LifecycleError as e:
        raise HTTPException(e.status, e.message) from None
    except LLMError as e:
        raise HTTPException(502, str(e)) from None


class NodeSummary(BaseModel):
    id: int
    dir: str
    title: str | None = None
    kind: str | None = None
    domain: str | None = None
    tier: int | None = None
    status: str
    provisional: bool
    next_action: str


class PrelabItemView(BaseModel):
    id: str
    kind: str
    unit: str | None = None
    prompt: str


class PrelabView(BaseModel):
    markdown: str
    items: list[PrelabItemView]
    locked: bool
    submitted: str | None = None
    answers: dict[str, Any] | None = None


class NodeView(NodeSummary):
    subskills: list[str]
    size_est_min: int | None = None
    sim_runtime_s: int | None = None
    long_run_warning: bool
    brief: str
    prelab: PrelabView
    proof_artifacts: list[str]
    workspace_unlocked: bool
    workspace: str | None = None
    rescopable: bool


class PrelabBody(BaseModel):
    answers: dict[str, str | float]


class JobStarted(BaseModel):
    job_id: str
    submission: int | None = None


class CheckTest(BaseModel):
    name: str | None = None
    pass_: bool
    measured: Any = None
    unit: str = ""


class CheckerView(BaseModel):
    ts: str | None = None
    kind: str | None = None
    verdict: str | None = None
    passed: bool | None = None
    tests: list[dict[str, Any]]
    metrics: dict[str, Any] = {}
    seconds: float | None = None
    detail: str | None = None


class TutorBody(BaseModel):
    message: str


class TutorReply(BaseModel):
    reply: str
    hint: dict[str, Any] | None = None
    redacted: bool
    model: str | None = None


class HintBody(BaseModel):
    subskill: str


class ShowMeBody(BaseModel):
    question: str


class ShowMeReply(BaseModel):
    explanation: str
    subskills: list[str]
    assist_reveal: bool
    model: str | None = None


class RemediationBody(BaseModel):
    accept: bool


class OralBody(BaseModel):
    mode: str = "text"


class RescopeBody(BaseModel):
    reason_class: str  # too_hard | wrong_direction | objection | broken_env
    reason: str


class RescopeStarted(BaseModel):
    action: str
    changes: str
    job_id: str
    node_dir: str
    node_id: int


@router.get("/nodes", response_model=list[NodeSummary], operation_id="listNodes")
def list_nodes(request: Request) -> list[dict]:
    return _call(_lc(request).list_nodes)


@router.get("/nodes/{node_id}", response_model=NodeView, operation_id="getNode")
def get_node(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).view(node_id))


@router.post("/nodes/{node_id}/prelab/submit", response_model=NodeView, operation_id="submitPrelab")
def submit_prelab(node_id: int, body: PrelabBody, request: Request) -> dict:
    """Lock the prelab answers (immutable) and unlock the lab."""
    return _call(lambda: _lc(request).prelab_submit(node_id, dict(body.answers)))


@router.post("/nodes/{node_id}/open-folder", operation_id="openNodeFolder")
def open_folder(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).open_folder(node_id))


@router.post("/nodes/{node_id}/checker/run", response_model=JobStarted, status_code=202, operation_id="runNodeChecker")
def run_checker(node_id: int, request: Request, m: Jobs) -> dict:
    job = _call(lambda: _lc(request).start_check(m, node_id))
    return {"job_id": job.id}


@router.get("/nodes/{node_id}/checker", response_model=CheckerView | None, operation_id="getNodeChecker")
def get_checker(node_id: int, request: Request):
    return _call(lambda: _lc(request).latest_check(node_id))


@router.get("/nodes/{node_id}/tutor", operation_id="getTutorHistory")
def tutor_history(node_id: int, request: Request) -> dict:
    lc = _lc(request)
    return _call(lambda: lc.tutor.history(lc.store.find(node_id)))


@router.post("/nodes/{node_id}/tutor", response_model=TutorReply, operation_id="tutorChat")
def tutor_chat(node_id: int, body: TutorBody, request: Request) -> dict:
    lc = _lc(request)
    return _call(lambda: lc.tutor.chat(lc.store.find(node_id), body.message))


@router.post("/nodes/{node_id}/hints/reveal", operation_id="revealHint")
def reveal_hint(node_id: int, body: HintBody, request: Request) -> dict:
    lc = _lc(request)
    return _call(lambda: lc.tutor.reveal_hint(lc.store.find(node_id), body.subskill))


@router.post("/nodes/{node_id}/show-me", response_model=ShowMeReply, operation_id="showMe")
def show_me(node_id: int, body: ShowMeBody, request: Request) -> dict:
    """Heavy call with _hidden/ access; marks the explained subskills assist_reveal."""
    lc = _lc(request)
    return _call(lambda: lc.tutor.show_me(lc.store.find(node_id), body.question))


@router.post("/nodes/{node_id}/proof/submit", response_model=JobStarted, status_code=202, operation_id="submitProof")
def submit_proof(node_id: int, request: Request, m: Jobs) -> dict:
    job, n = _call(lambda: _lc(request).proof_submit(m, node_id))
    return {"job_id": job.id, "submission": n}


@router.get("/nodes/{node_id}/proof", operation_id="getProof")
def get_proof(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).proof_view(node_id))


@router.post("/nodes/{node_id}/proof/remediation", operation_id="answerRemediationOffer")
def remediation(node_id: int, body: RemediationBody, request: Request) -> dict:
    return _call(lambda: _lc(request).remediation(node_id, body.accept))


@router.get("/nodes/{node_id}/oral", operation_id="getOral")
def get_oral(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).oral_info(node_id))


@router.post("/nodes/{node_id}/oral/advance", operation_id="advanceOral")
def advance_oral(node_id: int, body: OralBody, request: Request) -> dict:
    """Stub until M7: text mode moves the node to Debrief for testing."""
    return _call(lambda: _lc(request).oral_advance(node_id, body.mode))


@router.get("/nodes/{node_id}/debrief", operation_id="getDebrief")
def get_debrief(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).debrief_view(node_id))


@router.post("/nodes/{node_id}/debrief/complete", response_model=NodeSummary, operation_id="completeDebrief")
def complete_debrief(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).debrief_complete(node_id))


@router.post("/nodes/{node_id}/rescope", response_model=RescopeStarted, status_code=202, operation_id="rescopeNode")
def rescope(node_id: int, body: RescopeBody, request: Request, m: Jobs) -> dict:
    return _call(lambda: _lc(request).rescope(m, node_id, body.reason_class, body.reason))


@router.get("/grading/pending", operation_id="getPendingGrades")
def pending_grades(request: Request) -> list[dict]:
    return _call(_lc(request).pending_grades)


@router.post("/grading/retry", operation_id="retryGrading")
def retry_grading(request: Request) -> dict:
    return {"applied": _call(lambda: _lc(request).retry_grades(force=True))}
