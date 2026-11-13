"""Ingest one uploaded book (SPEC §10): text per page -> OCR for near-empty pages -> chunks -> embeddings -> index files.

Runs as a background `library_ingest` job. Progress: extraction 0-0.2, OCR 0.2-0.4, embedding 0.4-0.95, write 1.0.
"""

import time
from collections.abc import Callable

from app.library.chunking import chunk_pages
from app.library.embed import BATCH, Embedder
from app.library.extract import OcrFn, extract_pages, low_text_pages, ocr_available, ocr_with_ocrmypdf
from app.library.store import LibraryStore

TASK = "library_ingest"
MAX_PAGES = 5000


def job_label(book_id: str) -> str:
    """Node label of the job. `lib:` cannot collide with the `<track>/<dir>` labels the track busy-guard matches."""
    return f"lib:{book_id}"


def ingest_book(
    store: LibraryStore, book_id: str, embedder: Embedder, log: Callable[[str], None] = lambda _s: None,
    progress: Callable[[float], None] = lambda _p: None, ocr: OcrFn | None = None, ocr_ready: Callable[[], bool] | None = None,
) -> dict:
    """Index the book's source.pdf. Marks the book `ready` or `failed` (and re-raises, so the job fails too)."""
    store.update(book_id, status="ingesting", error=None, warnings=[])
    try:
        return _run(store, book_id, embedder, log, progress, ocr or ocr_with_ocrmypdf, ocr_ready or ocr_available)
    except Exception as e:  # noqa: BLE001 - the failure is recorded on the book, then the job fails with it
        store.update(book_id, status="failed", error=f"{e.__class__.__name__}: {e}"[:500])
        raise


def _run(store, book_id, embedder, log, progress, ocr, ocr_ready) -> dict:
    t0 = time.monotonic()
    pdf = store.pdf_path(book_id)
    pages = extract_pages(pdf)
    if not pages:
        raise ValueError("the PDF has no pages")
    if len(pages) > MAX_PAGES:
        raise ValueError(f"the PDF has {len(pages)} pages (limit {MAX_PAGES})")
    log(f"extracted text of {len(pages)} pages")
    progress(0.2)

    warnings: list[str] = []
    low = low_text_pages(pages)
    ocr_done = 0
    if low:
        if ocr_ready():
            log(f"{len(low)} pages have < 50 characters: running OCR")
            try:
                got = ocr(pdf, low)
            except Exception as e:  # noqa: BLE001 - OCR trouble must not lose the text that did extract
                warnings.append(f"OCR failed ({e.__class__.__name__}: {str(e)[:200]}); {len(low)} pages have little or no text")
                got = {}
            for p, text in got.items():
                if len(text.strip()) > len(pages[p - 1].strip()):
                    pages[p - 1] = text
                    ocr_done += 1
        else:
            warnings.append(f"{len(low)} pages have < 50 characters and OCR is not available (install Tesseract to read scanned pages)")
    progress(0.4)

    chunks = chunk_pages(pages)
    if not chunks:
        raise ValueError("no text could be extracted from this PDF" + ("" if ocr_ready() else " (it looks scanned and OCR is not available)"))
    log(f"{len(chunks)} chunks; embedding")
    import numpy as np

    vecs = []
    for i in range(0, len(chunks), BATCH):
        vecs.append(embedder.embed_documents([c.text for c in chunks[i : i + BATCH]]))
        progress(0.4 + 0.55 * min(i + BATCH, len(chunks)) / len(chunks))
    matrix = np.vstack(vecs).astype(np.float32)
    rows = [{"book_id": book_id, "page": c.page, "text": c.text} for c in chunks]
    store.write_index(book_id, rows, matrix)
    covered = {c.page for c in chunks}
    book = store.update(book_id, status="ready", pages=len(pages), chunks=len(chunks), ocr_pages=ocr_done,
                        empty_pages=len(pages) - len(covered), warnings=warnings, error=None)  # fmt: skip
    progress(1.0)
    log(f"ready: {len(pages)} pages, {len(chunks)} chunks in {time.monotonic() - t0:.0f} s")
    return book.model_dump()
