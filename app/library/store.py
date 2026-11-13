"""`data/library/<book-id>/` (SPEC §4.1, shared across tracks): source.pdf, book.json, chunks.jsonl, embeddings.npy.

The files are the source of truth and nothing in them is track-scoped, so no track operation touches this directory (SPEC §8.6).
`library/.gitignore` keeps the PDFs and vectors out of the data repo.
"""

import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

BOOK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
Status = Literal["queued", "ingesting", "ready", "failed"]
_lock = threading.Lock()


class LibraryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Book(BaseModel):
    id: str
    title: str
    filename: str
    size_bytes: int
    sha256: str
    status: Status
    pages: int = 0
    chunks: int = 0
    ocr_pages: int = 0  # pages whose text came from OCR
    empty_pages: int = 0  # pages with no usable text left after extraction (and OCR, when it ran)
    warnings: list[str] = []
    error: str | None = None
    created: str
    updated: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].strip("-")
    return s or "book"


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class LibraryStore:
    def __init__(self, data_root: Path):
        self.dir = Path(data_root) / "library"

    # --- paths -------------------------------------------------------------------------------------
    def book_dir(self, book_id: str) -> Path:
        if not BOOK_ID.match(book_id):
            raise LibraryError(404, "no such book")
        return self.dir / book_id

    def pdf_path(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "source.pdf"

    def chunks_path(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "chunks.jsonl"

    def embeddings_path(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "embeddings.npy"

    # --- books -------------------------------------------------------------------------------------
    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8", newline="\n")

    def get(self, book_id: str) -> Book | None:
        p = self.book_dir(book_id) / "book.json"
        if not p.is_file():
            return None
        try:
            return Book.model_validate_json(p.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def require(self, book_id: str) -> Book:
        b = self.get(book_id)
        if b is None:
            raise LibraryError(404, "no such book")
        return b

    def books(self) -> list[Book]:
        if not self.dir.is_dir():
            return []
        books = [b for d in self.dir.iterdir() if d.is_dir() and BOOK_ID.match(d.name) and (b := self.get(d.name))]
        return sorted(books, key=lambda b: (b.created, b.id))

    def save(self, book: Book) -> Book:
        book.updated = _now()
        _atomic(self.book_dir(book.id) / "book.json", book.model_dump_json(indent=2).encode("utf-8"))
        return book

    def update(self, book_id: str, **fields) -> Book:
        with _lock:
            return self.save(self.require(book_id).model_copy(update=fields))

    def create(self, src: Path, filename: str, title: str | None = None) -> Book:
        """Adopt the uploaded PDF at `src` (moved into place) as a new queued book."""
        with _lock:
            self._ensure()
            sha = _sha256(src)
            for b in self.books():
                if b.sha256 == sha:
                    raise LibraryError(409, f"this PDF is already in the library as {b.title!r} ({b.id})")
            title = (title or Path(filename).stem).strip() or "Untitled"
            base = slugify(title)
            book_id, n = base, 1
            while (self.dir / book_id).exists():
                n += 1
                book_id = f"{base}-{n}"
            d = self.dir / book_id
            d.mkdir()
            shutil.move(src, d / "source.pdf")
            now = _now()
            book = Book(id=book_id, title=title, filename=Path(filename).name, size_bytes=(d / "source.pdf").stat().st_size, sha256=sha,
                        status="queued", created=now, updated=now)  # fmt: skip
            return self.save(book)

    def delete(self, book_id: str) -> None:
        with _lock:
            d = self.book_dir(book_id)
            if not d.is_dir():
                raise LibraryError(404, "no such book")
            shutil.rmtree(d)

    def fail_interrupted(self) -> list[str]:
        """Books left queued/ingesting by a previous run have no worker any more (the job store marks their jobs failed too)."""
        out = []
        for b in self.books():
            if b.status in ("queued", "ingesting"):
                self.update(b.id, status="failed", error="interrupted: the app stopped during ingest; delete the book and upload it again")
                out.append(b.id)
        return out

    # --- ingest output -----------------------------------------------------------------------------
    def write_index(self, book_id: str, rows: list[dict], embeddings) -> None:
        import io

        import numpy as np

        _atomic(self.chunks_path(book_id), "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8"))
        buf = io.BytesIO()
        np.save(buf, np.asarray(embeddings, dtype=np.float32))
        _atomic(self.embeddings_path(book_id), buf.getvalue())


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
