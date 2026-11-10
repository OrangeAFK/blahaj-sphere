from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.jobs.deps import get_manager
from app.jobs.manager import JobManager
from app.jobs.store import Job

router = APIRouter()

Manager = Annotated[JobManager, Depends(get_manager)]


@router.get("/jobs", response_model=list[Job], operation_id="listJobs")
def list_jobs(m: Manager, active: bool = False, limit: int = 50) -> list[Job]:
    return m.store.list_jobs(active_only=active, limit=min(max(limit, 1), 200))


@router.get("/jobs/stream", operation_id="streamJobs", response_class=StreamingResponse)
async def stream_jobs(m: Manager) -> StreamingResponse:
    """Server-sent events: `event: job` with a Job JSON payload, first a snapshot then every change."""

    async def gen():
        yield "retry: 2000\n\n"
        async for job in m.events():
            if job is None:
                yield ": keepalive\n\n"
            else:
                yield f"event: job\ndata: {job.model_dump_json()}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}", response_model=Job, operation_id="getJob")
def get_job(job_id: str, m: Manager) -> Job:
    job = m.store.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job
