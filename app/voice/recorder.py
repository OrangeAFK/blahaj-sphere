"""Push-to-talk capture in the backend (SPEC §7.6): the UI only says start/stop; this records the default microphone with
sounddevice at 16 kHz mono float32."""

import threading

import numpy as np

from app.voice.stt import SAMPLE_RATE, VoiceUnavailable

MAX_SECONDS = 120  # a held-down key must not record forever


class Recorder:
    def __init__(self):
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return  # key auto-repeat / double click: already recording
            try:
                import sounddevice as sd

                self._chunks, self._frames = [], 0

                def cb(indata, _frames, _time, _status):
                    if self._frames < MAX_SECONDS * SAMPLE_RATE:
                        self._chunks.append(indata[:, 0].copy())
                        self._frames += len(indata)

                self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=cb)
                self._stream.start()
            except Exception as e:  # noqa: BLE001 - PortAudio raises its own error types (no device, busy, denied)
                self._stream = None
                raise VoiceUnavailable(f"cannot open the microphone: {e}") from None

    def stop(self) -> np.ndarray:
        with self._lock:
            if self._stream is None:
                raise VoiceUnavailable("not recording")
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
            return np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)

    def cancel(self) -> None:
        try:
            self.stop()
        except VoiceUnavailable:
            pass
