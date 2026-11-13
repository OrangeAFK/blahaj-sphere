"""Local embeddings: fastembed `BAAI/bge-small-en-v1.5` (SPEC §2). Vectors are L2-normalised float32, so cosine = dot product."""

import threading
from pathlib import Path
from typing import Protocol

import numpy as np

MODEL = "BAAI/bge-small-en-v1.5"
BATCH = 32


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


def normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.where(n == 0, 1, n)


class FastEmbedder:
    """The model (~70 MB) downloads once into `data/models/fastembed` (shared across tracks, like the voice models) and loads lazily."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self._model = None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = TextEmbedding(MODEL, cache_dir=str(self.cache_dir))
            return self._model

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return normalize(np.array(list(self._get().embed(texts, batch_size=BATCH))))

    def embed_query(self, text: str) -> np.ndarray:
        return normalize(np.array(list(self._get().query_embed(text))))[0]
