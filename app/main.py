from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.domains import load_domain
from app.jobs.deps import manager_for
from app.routes import health, jobs, llm, onboarding, preflight, settings

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = manager_for(app)
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="BlahajSphere", lifespan=lifespan)
    app.include_router(health.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")
    app.include_router(llm.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(preflight.router, prefix="/api")
    app.include_router(onboarding.router, prefix="/api")
    app.state.domain = load_domain()
    if app.state.domain and app.state.domain.router:
        app.include_router(app.state.domain.router, prefix="/api")
    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    return app


app = create_app()
