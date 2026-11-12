"""The voice stack the oral exam talks to: one recorder (there is one microphone), one shared STT model, one shared TTS voice, and a
small in-memory cache of the question audio the UI plays (nothing is written to disk)."""

import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor

from app.voice.models import piper_ready, whisper_ready
from app.voice.recorder import Recorder
from app.voice.stt import Transcriber
from app.voice.tts import Speaker

AUDIO_CACHE = 12  # question clips kept for replay
CLIP_CACHE = 16  # pre-generated clips kept, keyed by the exact text


def _default_threads() -> int:
    """Physical-core count: measured on a 6-core/12-thread Ryzen, 6 threads beat 4, 8 and 12 for small.en int8 (DECISIONS M7)."""
    return max(2, (os.cpu_count() or 4) // 2)


def microphone_present() -> bool:
    try:
        import sounddevice as sd

        return any(d["max_input_channels"] > 0 for d in sd.query_devices())
    except Exception:  # noqa: BLE001 - PortAudio import/query failures all mean "no usable microphone"
        return False


class Voice:
    def __init__(self, recorder=None, transcriber=None, speaker=None):
        self.recorder = recorder or Recorder()
        self.transcriber = transcriber or Transcriber(cpu_threads=_default_threads())
        self.speaker = speaker or Speaker()
        self._injected = transcriber is not None, speaker is not None, recorder is not None
        self._audio: OrderedDict[tuple[str, int, int], bytes] = OrderedDict()
        self._clips: OrderedDict[str, Future] = OrderedDict()
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    def status(self) -> dict:
        stt, tts, mic = self._injected
        return {"stt": stt or whisper_ready(), "tts": tts or piper_ready(), "microphone": mic or microphone_present()}

    def put_audio(self, track: str, node_id: int, n: int, wav: bytes) -> None:
        with self._lock:
            self._audio[(track, node_id, n)] = wav
            while len(self._audio) > AUDIO_CACHE:
                self._audio.popitem(last=False)

    def audio(self, track: str, node_id: int, n: int) -> bytes | None:
        with self._lock:
            return self._audio.get((track, node_id, n))

    def pregenerate(self, text: str) -> None:
        """Start synthesizing `text` in the background (one worker: Piper is CPU-bound and shared). Idempotent per text; a failure is only
        seen by the `clip` call that wanted it, which then synthesizes in the foreground."""
        with self._lock:
            if text in self._clips:
                self._clips.move_to_end(text)
                return
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-pregen")
            self._clips[text] = self._pool.submit(self.speaker.synthesize, text)
            while len(self._clips) > CLIP_CACHE:
                self._clips.popitem(last=False)

    def clip(self, text: str) -> tuple[bytes, float, bool]:
        """Audio for `text` -> (WAV bytes, seconds this call spent getting it, True if it had been pre-generated). A pre-generated clip that is
        done costs ~0 s; one still in flight is waited for; a missing or failed one is synthesized now."""
        with self._lock:
            fut = self._clips.get(text)
        t0 = time.perf_counter()
        if fut is not None:
            try:
                return fut.result()[0], time.perf_counter() - t0, True
            except Exception:  # noqa: BLE001 - fall through to a foreground synthesis, which raises the real error if it persists
                with self._lock:
                    self._clips.pop(text, None)
        wav, secs = self.speaker.synthesize(text)
        return wav, secs, False

    def warm_up(self, stt: bool = True, tts: bool = True) -> None:
        """Load the models now (first use otherwise pays ~2 s STT + ~5 s TTS load inside the first turn). Errors are left to first use."""
        for wanted, loader in ((stt, self.transcriber.load), (tts, self.speaker.load)):
            if wanted:
                try:
                    loader()
                except Exception:  # noqa: BLE001 - a missing model surfaces as VoiceUnavailable when it is actually needed
                    pass


def voice_for(app) -> Voice:
    """One Voice per app: there is one microphone, and the models are expensive to load."""
    if not hasattr(app.state, "voice"):
        app.state.voice = Voice()
    return app.state.voice
