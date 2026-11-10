from fastapi import APIRouter, HTTPException, Request

from app.jobs.deps import get_running_manager
from app.jobs.store import Job
from app.preflight import Check, run_preflight
from app.settings import data_dir, load_settings

router = APIRouter()


@router.get("/preflight", response_model=list[Check], operation_id="getPreflight")
def get_preflight(request: Request) -> list[Check]:
    """Run all preflight checks (first run + Settings screen)."""
    return run_preflight(request.app.state.domain, load_settings(), data_dir())


@router.post("/preflight/fix/{action}", response_model=Job, operation_id="runPreflightFix")
def run_fix(action: str, request: Request) -> Job:
    """Start the job behind a check's `fix_action` (e.g. build the toolchain image)."""
    domain = request.app.state.domain
    fix = domain.fixes.get(action) if domain else None
    if fix is None:
        raise HTTPException(404, f"unknown fix action: {action}")
    return fix(get_running_manager(request))
