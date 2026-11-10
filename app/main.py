from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes import health, llm, settings

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="BlahajSphere")
    app.include_router(health.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")
    app.include_router(llm.router, prefix="/api")
    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    return app


app = create_app()
