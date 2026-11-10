"""Per-app job store/manager, created lazily so importing the app never touches `data/`."""

from fastapi import FastAPI, HTTPException, Request

from app.jobs.manager import JobManager
from app.jobs.store import JobStore, default_db_path


def manager_for(app: FastAPI) -> JobManager:
    if not hasattr(app.state, "job_manager"):
        app.state.job_manager = JobManager(JobStore(default_db_path()))
    return app.state.job_manager


def get_manager(request: Request) -> JobManager:
    return manager_for(request.app)


def get_running_manager(request: Request) -> JobManager:
    m = manager_for(request.app)
    if not m.started:
        raise HTTPException(503, "job worker not running")
    return m
