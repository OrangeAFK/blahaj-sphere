"""Node lifecycle endpoints (SPEC §7.2-7.7): prelab, lab, tutor, proof, oral (voice + text), debrief, rescope, grading queue.
Sync handlers run in the threadpool."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.jobs.deps import get_running_manager, manager_for
from app.jobs.manager import JobManager
from app.lifecycle.nodes import LifecycleError
from app.lifecycle.service import Lifecycle
from app.llm.types import LLMError
from app.routes.builder import _llm
from app.tracks.scope import current_scope
from app.voice.service import voice_for

router = APIRouter()
Jobs = Annotated[JobManager, Depends(get_running_manager)]


def _lc(request: Request) -> Lifecycle:
    try:
        sc = current_scope()
        return Lifecycle(sc.root, sc.track, _llm(request), request.app.state.domain, voice=voice_for(request.app), jobs=manager_for(request.app))
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
    outcome: str | None = None  # "done-with-remediation" when the oral failed and a remediation child was spawned


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
    citations: list[dict[str, Any]] = []  # library passages the reply used: {book_id, page, title} (SPEC §10)
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


class OralStartBody(BaseModel):
    mode: Literal["voice", "text"] = "text"
    tts: bool = False  # read the questions aloud (Piper)


class OralAnswerBody(BaseModel):
    text: str


class RescopeBody(BaseModel):
    reason_class: str  # too_hard | wrong_direction | objection | broken_env
    reason: str


class RescopeStarted(BaseModel):
    action: str
    changes: str
    job_id: str
    node_dir: str
    node_id: int
    backup: str | None = None  # edit only: data/-relative folder holding your workspace from before the reset


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
def open_folder(node_id: int, request: Request, target: Literal["workspace", "backup"] = "workspace") -> dict:
    return _call(lambda: _lc(request).open_folder(node_id, target))


@router.post("/nodes/{node_id}/run", response_model=JobStarted, status_code=202, operation_id="runNodeSimulation")
def run_simulation(node_id: int, request: Request, m: Jobs) -> dict:
    """Run the node's `run` commands in the workspace as a job; output streams to the job log."""
    job = _call(lambda: _lc(request).start_run(m, node_id))
    return {"job_id": job.id}


@router.get("/nodes/{node_id}/run", operation_id="getNodeRun")
def get_run(node_id: int, request: Request):
    return _call(lambda: _lc(request).latest_run(node_id))


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


@router.post("/nodes/{node_id}/oral/start", operation_id="startOral")
def start_oral(node_id: int, body: OralStartBody, request: Request) -> dict:
    """Begin (or resume, switching mode) the oral exam; returns the view with the first question."""
    lc = _lc(request)
    return _call(lambda: lc.oral.start(lc.store.find(node_id), body.mode, body.tts))


@router.post("/nodes/{node_id}/oral/answer", operation_id="answerOral")
def answer_oral(node_id: int, body: OralAnswerBody, request: Request) -> dict:
    """Text fallback: one typed answer -> the examiner's next utterance."""
    lc = _lc(request)
    return _call(lambda: lc.oral.answer(lc.store.find(node_id), body.text))


@router.post("/nodes/{node_id}/oral/record/start", operation_id="startOralRecording")
def record_start(node_id: int, request: Request) -> dict:
    """Push-to-talk down: the backend starts recording the microphone."""
    lc = _lc(request)
    return _call(lambda: lc.oral.record_start(lc.store.find(node_id)))


@router.post("/nodes/{node_id}/oral/record/stop", operation_id="stopOralRecording")
def record_stop(node_id: int, request: Request) -> dict:
    """Push-to-talk up: stop, transcribe, get the examiner's reply (and its audio) in one request."""
    lc = _lc(request)
    return _call(lambda: lc.oral.record_stop(lc.store.find(node_id)))


@router.post("/nodes/{node_id}/oral/record/cancel", operation_id="cancelOralRecording")
def record_cancel(node_id: int, request: Request) -> dict:
    lc = _lc(request)
    return _call(lambda: lc.oral.record_cancel(lc.store.find(node_id)))


@router.get("/nodes/{node_id}/oral/audio/{n}", operation_id="getOralAudio")
def oral_audio(node_id: int, n: int, request: Request) -> Response:
    lc = _lc(request)
    wav = _call(lambda: lc.oral.audio(lc.store.find(node_id), n))
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.post("/nodes/{node_id}/oral/finish", operation_id="finishOral")
def finish_oral(node_id: int, request: Request) -> dict:
    """The exam is complete: grade it and move to the Debrief."""
    return _call(lambda: _lc(request).oral_finish(node_id))


@router.post("/nodes/{node_id}/oral/remediation/retry", operation_id="retryRemediation")
def retry_remediation(node_id: int, request: Request) -> dict:
    lc = _lc(request)
    return _call(lambda: lc.oral.retry_remediation(lc.store.find(node_id)))


@router.get("/nodes/{node_id}/debrief", operation_id="getDebrief")
def get_debrief(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).debrief_view(node_id))


@router.post("/nodes/{node_id}/debrief/complete", response_model=NodeSummary, operation_id="completeDebrief")
def complete_debrief(node_id: int, request: Request) -> dict:
    return _call(lambda: _lc(request).debrief_complete(node_id))


@router.post("/nodes/{node_id}/rescope", response_model=RescopeStarted, status_code=202, operation_id="rescopeNode")
def rescope(node_id: int, body: RescopeBody, request: Request, m: Jobs) -> dict:
    return _call(lambda: _lc(request).rescope(m, node_id, body.reason_class, body.reason))


class SummaryView(BaseModel):
    text: str  # archive/SUMMARY.md of the active track ("" until the first Debrief completes)
    tokens: int  # estimate (chars / 3.5)
    cap: int  # SPEC §3.2


@router.get("/summary", response_model=SummaryView, operation_id="getSummary")
def get_summary(request: Request) -> dict:
    """SUMMARY.md, the only history the Planner and Builder read (regenerated after each Debrief, at most 1,500 tokens)."""
    from app.lifecycle.summary import TOKEN_CAP, estimate_tokens

    text = _call(_lc(request).summary_text)
    return {"text": text, "tokens": estimate_tokens(text) if text else 0, "cap": TOKEN_CAP}


@router.post("/summary/regenerate", response_model=SummaryView, operation_id="regenerateSummary")
def regenerate_summary(request: Request) -> dict:
    """Rebuild SUMMARY.md from the finished nodes' entries (missing entries are built by code, no LLM call)."""
    from app.lifecycle.summary import TOKEN_CAP, estimate_tokens

    text = _call(_lc(request).regenerate_summary)
    return {"text": text, "tokens": estimate_tokens(text), "cap": TOKEN_CAP}


@router.get("/grading/pending", operation_id="getPendingGrades")
def pending_grades(request: Request) -> list[dict]:
    return _call(_lc(request).pending_grades)


@router.post("/grading/retry", operation_id="retryGrading")
def retry_grading(request: Request) -> dict:
    return {"applied": _call(lambda: _lc(request).retry_grades(force=True))}
