"""The voice stack the oral exam talks to: one recorder (there is one microphone), one shared STT model, one shared TTS voice, and a
small in-memory cache of the question audio the UI plays (nothing is written to disk)."""

import os
import threading
from collections import OrderedDict

from app.voice.models import piper_ready, whisper_ready
from app.voice.recorder import Recorder
from app.voice.stt import Transcriber
from app.voice.tts import Speaker

AUDIO_CACHE = 12  # question clips kept for replay


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
