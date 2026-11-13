"""One shared LibraryIndex per data root, so Builder and Tutor retrieval reuse the loaded matrix and the embedding model.

The embedder is created lazily: a data dir with no ready book never loads (or downloads) the model, because search returns before
embedding the query when there is nothing to search. Tests replace `embedder_factory`.
"""

import threading
from collections.abc import Callable
from pathlib import Path

from app.library.embed import Embedder, FastEmbedder
from app.library.search import LibraryIndex, excerpt_for_prompt

CITATION = r"\(([a-z0-9][a-z0-9-]*),? p\.? ?(\d+)\)"  # `(book-id, p. 123)` as the prompts tell Builder and Tutor to write it


def default_embedder(root: Path) -> Embedder:
    return FastEmbedder(Path(root) / "models" / "fastembed")


embedder_factory: Callable[[Path], Embedder] = default_embedder
_indexes: dict[Path, LibraryIndex] = {}
_lock = threading.Lock()


def library_for(root: Path) -> LibraryIndex:
    key = Path(root).resolve()
    with _lock:
        idx = _indexes.get(key)
        if idx is None:
            idx = _indexes[key] = LibraryIndex(Path(root), embedder_factory(Path(root)))
        return idx


def reset() -> None:
    """Forget the cached indexes (tests; also after swapping `embedder_factory`)."""
    with _lock:
        _indexes.clear()


def retrieve(root: Path, query: str, k: int = 5) -> list[dict]:
    """Top-k hits for a prompt. Never raises: retrieval is an aid, a broken library must not stop a build or a tutor reply."""
    try:
        return library_for(root).search(query, k)
    except Exception:  # noqa: BLE001
        return []


__all__ = ["library_for", "retrieve", "reset", "excerpt_for_prompt", "CITATION"]
