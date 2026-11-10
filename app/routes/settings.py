from fastapi import APIRouter

from app.settings import Settings, load_settings, save_settings

router = APIRouter()


@router.get("/settings", response_model=Settings, operation_id="getSettings")
def get_settings() -> Settings:
    return load_settings()


@router.put("/settings", response_model=Settings, operation_id="putSettings")
def put_settings(s: Settings) -> Settings:
    save_settings(s)
    return s
