"""Library (SPEC §10): upload a PDF, list books with ingest state, delete, search. Shared across tracks, so nothing here is track-scoped."""

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.jobs.deps import get_running_manager
from app.jobs.manager import JobManager
from app.library.extract import ocr_available
from app.library.ingest import TASK, ingest_book, job_label
from app.library.search import TOP_K
from app.library.service import library_for
from app.library.store import Book, LibraryError, LibraryStore
from app.settings import data_dir, load_settings

router = APIRouter(prefix="/library")
MAX_UPLOAD_BYTES = 600 * 1024 * 1024
Manager = Annotated[JobManager, Depends(get_running_manager)]


class LibraryView(BaseModel):
    books: list[Book]
    ocr_available: bool  # ocrmypdf + Tesseract usable here: scanned pages are read, not skipped
    allow_library_to_free_models: bool  # SPEC §3 privacy toggle (Settings): when false, library text only goes to claude_code tasks


class Uploaded(BaseModel):
    book: Book
    job_id: str


class Hit(BaseModel):
    book_id: str
    title: str
    page: int
    text: str
    score: float


def _store() -> LibraryStore:
    return LibraryStore(data_dir())


def _err(e: LibraryError) -> HTTPException:
    return HTTPException(e.status, e.message)


@router.get("", response_model=LibraryView, operation_id="getLibrary")
def get_library() -> dict:
    return {"books": _store().books(), "ocr_available": ocr_available(),
            "allow_library_to_free_models": load_settings().allow_library_to_free_models}  # fmt: skip


@router.post("/books", response_model=Uploaded, status_code=202, operation_id="uploadBook")
async def upload_book(request: Request, m: Manager, filename: str, title: str | None = None) -> dict:
    """The PDF is the raw request body (`Content-Type: application/pdf`); `filename` names it. Ingest runs as a `library_ingest` job."""
    store = _store()
    store.dir.mkdir(parents=True, exist_ok=True)
    size = 0
    with tempfile.NamedTemporaryFile(dir=store.dir, prefix=".upload-", suffix=".pdf", delete=False) as f:
        tmp = Path(f.name)
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"the PDF is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
                f.write(chunk)
        except BaseException:
            f.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        with tmp.open("rb") as r:
            if r.read(5) != b"%PDF-":
                raise HTTPException(422, "that is not a PDF file")
        book = store.create(tmp, filename, title)
    except LibraryError as e:
        tmp.unlink(missing_ok=True)
        raise _err(e) from None
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    root = data_dir()
    embedder = library_for(root).embedder

    def run(handle) -> None:
        ingest_book(LibraryStore(root), book.id, embedder, log=handle.log, progress=handle.progress)  # type: ignore[arg-type]

    job = m.submit(TASK, run, node_id=job_label(book.id))
    return {"book": book, "job_id": job.id}


@router.delete("/books/{book_id}", status_code=204, operation_id="deleteBook")
def delete_book(book_id: str) -> None:
    store = _store()
    try:
        book = store.require(book_id)
        if book.status in ("queued", "ingesting"):
            raise HTTPException(409, "this book is still being ingested; delete it when that finishes")
        store.delete(book_id)
    except LibraryError as e:
        raise _err(e) from None


@router.get("/search", response_model=list[Hit], operation_id="searchLibrary")
def search_library(q: str, k: int = TOP_K) -> list[dict]:
    """Top-k passages for a query (cosine over local embeddings). The same retrieval Builder and the tutor use."""
    return library_for(data_dir()).search(q, min(max(k, 1), 20))
