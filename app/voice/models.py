"""Where the voice models live and how to fetch them. `python -m app.voice download` fetches both (~520 MB, public Hugging Face repos)."""

import os
import shutil
from pathlib import Path

from app.settings import data_dir

WHISPER_REPO = "Systran/faster-whisper-small.en"
WHISPER_NAME = "small.en"
PIPER_REPO = "rhasspy/piper-voices"
PIPER_VOICE = "en/en_US/lessac/medium/en_US-lessac-medium"  # path inside the repo, without extension


def models_dir() -> Path:
    """BLAHAJ_MODELS_DIR overrides, so a throwaway copy of data/ (acceptance runs) can reuse the real download."""
    env = os.environ.get("BLAHAJ_MODELS_DIR")
    return Path(env) if env else data_dir() / "models"


def whisper_dir() -> Path:
    return models_dir() / "whisper" / WHISPER_NAME


def piper_model() -> Path | None:
    d = models_dir() / "piper"
    found = sorted(d.glob("*.onnx")) if d.is_dir() else []
    return found[0] if found else None


def whisper_ready() -> bool:
    d = whisper_dir()
    return (d / "model.bin").is_file() and (d / "config.json").is_file()


def piper_ready() -> bool:
    m = piper_model()
    return m is not None and m.with_name(m.name + ".json").is_file()


def download(log=print) -> None:
    from huggingface_hub import hf_hub_download, snapshot_download

    if not whisper_ready():
        log(f"downloading {WHISPER_REPO} -> {whisper_dir()}")
        snapshot_download(WHISPER_REPO, local_dir=str(whisper_dir()))
    if not piper_ready():
        dest = models_dir() / "piper"
        dest.mkdir(parents=True, exist_ok=True)
        for ext in (".onnx", ".onnx.json"):
            log(f"downloading {PIPER_VOICE}{ext}")
            src = Path(hf_hub_download(PIPER_REPO, PIPER_VOICE + ext, local_dir=str(dest / "_dl")))
            src.replace(dest / src.name)
        shutil.rmtree(dest / "_dl", ignore_errors=True)
