"""Retrieval (SPEC §10): brute-force cosine over every ready book's vectors, top-k = 5, each hit citable as {book_id, page}."""

import json
import threading
from pathlib import Path

import numpy as np

from app.library.embed import Embedder
from app.library.store import LibraryStore

TOP_K = 5
MAX_QUERY_CHARS = 2000
EXCERPT_CHARS = 2200  # one chunk is at most ~2000 characters


class LibraryIndex:
    """All ready books in one matrix, reloaded when a book is added, removed or re-ingested (keyed on book.json mtimes)."""

    def __init__(self, data_root: Path, embedder: Embedder | None):
        self.store = LibraryStore(data_root)
        self.embedder = embedder
        self._sig: tuple | None = None
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def _signature(self) -> tuple:
        out = []
        for b in self.store.books():
            if b.status == "ready":
                out.append((b.id, (self.store.book_dir(b.id) / "book.json").stat().st_mtime_ns))
        return tuple(out)

    def _load(self) -> None:
        sig = self._signature()
        if sig == self._sig:
            return
        rows: list[dict] = []
        mats = []
        for book_id, _ in sig:
            try:
                emb = np.load(self.store.embeddings_path(book_id))
                lines = self.store.chunks_path(book_id).read_text(encoding="utf-8").splitlines()
                got = [json.loads(ln) for ln in lines if ln.strip()]
            except (OSError, ValueError):
                continue  # a half-deleted book: skip it
            if len(got) != len(emb):
                continue
            title = self.store.get(book_id).title  # type: ignore[union-attr]
            for r in got:
                r["title"] = title
            rows += got
            mats.append(emb)
        self._rows = rows
        self._matrix = np.vstack(mats) if mats else np.zeros((0, 0), dtype=np.float32)
        self._sig = sig

    def __len__(self) -> int:
        with self._lock:
            self._load()
            return len(self._rows)

    def search(self, query: str, k: int = TOP_K, book_ids: set[str] | None = None) -> list[dict]:
        """-> [{book_id, title, page, text, score}] best first. [] for an empty library or an empty query."""
        query = (query or "").strip()[:MAX_QUERY_CHARS]
        if not query or self.embedder is None:
            return []
        with self._lock:
            self._load()
            if not self._rows:
                return []
            q = self.embedder.embed_query(query)
            scores = self._matrix @ q
            order = np.argsort(-scores)
            out = []
            for i in order:
                r = self._rows[int(i)]
                if book_ids is not None and r["book_id"] not in book_ids:
                    continue
                out.append({"book_id": r["book_id"], "title": r["title"], "page": r["page"], "text": r["text"], "score": round(float(scores[i]), 4)})
                if len(out) >= k:
                    break
            return out


def excerpt_for_prompt(hits: list[dict]) -> list[dict]:
    """The shape prompts carry: the citation key, the passage, nothing else."""
    return [{"book_id": h["book_id"], "page": h["page"], "text": h["text"][:EXCERPT_CHARS]} for h in hits]
