"""Piper TTS (SPEC §7.6): question text -> WAV bytes, played by the UI. Loaded lazily and shared."""

import io
import threading
import time
import wave

from app.voice.models import piper_model, piper_ready
from app.voice.stt import VoiceUnavailable


class Speaker:
    def __init__(self, model_path=None):
        self.model_path = model_path
        self._voice = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._voice is None:
                path = self.model_path or piper_model()
                if path is None or (self.model_path is None and not piper_ready()):
                    raise VoiceUnavailable("the text-to-speech voice is not downloaded (run: uv run python -m app.voice download)")
                from piper import PiperVoice

                self._voice = PiperVoice.load(path)
            return self._voice

    def synthesize(self, text: str) -> tuple[bytes, float]:
        """-> (WAV bytes, synthesis seconds). Model load excluded from the time."""
        voice = self.load()
        t0 = time.perf_counter()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w)
        return buf.getvalue(), time.perf_counter() - t0
