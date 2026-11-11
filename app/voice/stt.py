"""faster-whisper small.en, int8, CPU (SPEC §2, §7.6). The model loads once (lazily) and is shared across turns."""

import threading
import time
from dataclasses import dataclass

import numpy as np

from app.voice.models import whisper_dir, whisper_ready

SAMPLE_RATE = 16_000  # Whisper's native rate; the recorder captures at this rate so no resampling is needed


class VoiceUnavailable(Exception):
    pass


@dataclass
class Transcript:
    text: str
    audio_s: float
    seconds: float  # wall time of the transcription (model load excluded)


class Transcriber:
    def __init__(self, model_dir=None, cpu_threads: int = 0, beam_size: int = 1):
        self.model_dir = model_dir
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        self._model = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._model is None:
                d = self.model_dir or whisper_dir()
                if self.model_dir is None and not whisper_ready():
                    raise VoiceUnavailable("the speech-to-text model is not downloaded (run: uv run python -m app.voice download)")
                from faster_whisper import WhisperModel

                self._model = WhisperModel(str(d), device="cpu", compute_type="int8", cpu_threads=self.cpu_threads)
            return self._model

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> Transcript:
        """audio: float32 mono, 16 kHz. `prompt` biases the decoder toward the node and domain vocabulary (initial_prompt)."""
        model = self.load()
        audio_s = len(audio) / SAMPLE_RATE
        if audio_s < 0.3:
            return Transcript("", audio_s, 0.0)
        t0 = time.perf_counter()
        segs, _ = model.transcribe(
            audio.astype(np.float32, copy=False), language="en", beam_size=self.beam_size, temperature=0.0, initial_prompt=prompt or None,
            condition_on_previous_text=False, vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        )  # fmt: skip
        text = " ".join(s.text.strip() for s in segs).strip()  # the generator is lazy: the decode happens here
        return Transcript(text, audio_s, time.perf_counter() - t0)


def build_prompt(node_vocab: list[str], domain_vocab: list[str], max_chars: int = 900) -> str:
    """initial_prompt = node vocab + domain vocab.txt (SPEC §7.6). Whisper reads ~224 tokens of prompt, so the node's own terms go
    first and the list is cut at max_chars; duplicates (case-insensitive) are dropped."""
    seen, out = set(), []
    for term in [*node_vocab, *domain_vocab]:
        t = str(term).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    prompt = ""
    for t in out:
        nxt = f"{prompt}, {t}" if prompt else t
        if len(nxt) > max_chars:
            break
        prompt = nxt
    return prompt
