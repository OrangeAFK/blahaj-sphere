import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.domains import load_domain
from app.jobs.deps import manager_for
from app.routes import builder, health, jobs, lifecycle, llm, onboarding, preflight, settings, scores, tracks

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.settings import data_dir
    from app.tracks.migrate import ensure_tracks

    ensure_tracks(data_dir())  # pre-tracks data/ -> track rfic-default (snapshot first, scores verified); fresh dir -> seeded registry
    manager = manager_for(app)
    await manager.start()
    task = asyncio.create_task(_grading_loop(app)) if getattr(app.state, "grading_retry", False) else None
    try:
        yield
    finally:
        if task:
            task.cancel()
        await manager.stop()


GRADING_RETRY_S = 60


async def _grading_loop(app: FastAPI) -> None:
    """SPEC §3.1: pending grades retry on app start and then with backoff (the queue's next_try decides what is due).
    One Lifecycle per registered track: a queued grade is applied in the track it was asked for (SPEC §8.6)."""
    from app.lifecycle.service import Lifecycle
    from app.routes.builder import _llm
    from app.settings import data_dir
    from app.tracks.registry import TrackRegistry

    class _R:  # _llm only needs .app
        pass

    r = _R()
    r.app = app  # type: ignore[attr-defined]

    def retry_all() -> None:
        root = data_dir()
        for t in TrackRegistry(root).list():
            try:
                Lifecycle(root, t.id, _llm(r), app.state.domain, jobs=manager_for(app)).retry_grades()  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - one track's retry failure must neither kill the loop nor starve the other tracks
                pass

    while True:
        try:
            await asyncio.to_thread(retry_all)
        except Exception:  # noqa: BLE001 - a retry failure must never kill the app
            pass
        await asyncio.sleep(GRADING_RETRY_S)


def create_app() -> FastAPI:
    app = FastAPI(title="BlahajSphere", lifespan=lifespan)
    app.include_router(health.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")
    app.include_router(llm.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(preflight.router, prefix="/api")
    app.include_router(onboarding.router, prefix="/api")
    app.include_router(builder.router, prefix="/api")
    app.include_router(lifecycle.router, prefix="/api")
    app.include_router(scores.router, prefix="/api")
    app.include_router(tracks.router, prefix="/api")
    app.state.domain = load_domain()
    if app.state.domain and app.state.domain.router:
        app.include_router(app.state.domain.router, prefix="/api")
    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    return app


app = create_app()
