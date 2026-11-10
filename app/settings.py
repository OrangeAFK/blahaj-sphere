import json
import os
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    openrouter_api_key: str = ""
    allow_library_to_free_models: bool = False
    claude_model: str = ""


def settings_path() -> Path:
    root = Path(os.environ.get("BLAHAJ_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    return root / "settings.json"


def load_settings() -> Settings:
    p = settings_path()
    if p.exists():
        return Settings.model_validate_json(p.read_text(encoding="utf-8"))
    return Settings()


def save_settings(s: Settings) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s.model_dump(), indent=2), encoding="utf-8")
    os.replace(tmp, p)
