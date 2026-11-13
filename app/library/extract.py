"""Text per page with PyMuPDF; pages with < 50 characters go to ocrmypdf + Tesseract when both are installed (SPEC §10)."""

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

OCR_MIN_CHARS = 50
_TESSERACT_DIRS = (r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR")

# (pdf, 1-based page numbers) -> text of each page that could be recognised
OcrFn = Callable[[Path, list[int]], dict[int, str]]


def extract_pages(pdf: Path) -> list[str]:
    """Plain text of every page, in order (`result[i]` is page i + 1). Raises ValueError for a file PyMuPDF cannot open."""
    import pymupdf

    try:
        doc = pymupdf.open(pdf)
    except Exception as e:  # noqa: BLE001 - pymupdf raises several types for a bad file
        raise ValueError(f"not a readable PDF: {e}") from None
    with doc:
        if doc.needs_pass:
            raise ValueError("the PDF is password protected")
        return [page.get_text("text") for page in doc]


def low_text_pages(pages: list[str], min_chars: int = OCR_MIN_CHARS) -> list[int]:
    """1-based numbers of pages whose text (whitespace ignored) is shorter than `min_chars`."""
    return [i for i, t in enumerate(pages, start=1) if len(re.sub(r"\s+", "", t)) < min_chars]


def _tesseract_on_path() -> bool:
    if shutil.which("tesseract"):
        return True
    for d in _TESSERACT_DIRS:  # the Windows installer does not touch PATH by default
        if (Path(d) / "tesseract.exe").is_file():
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return True
    return False


def ocr_available() -> bool:
    try:
        import ocrmypdf  # noqa: F401
    except ImportError:
        return False
    return _tesseract_on_path()


def _page_ranges(pages: list[int]) -> str:
    out, start, prev = [], None, None
    for p in sorted(set(pages)):
        if start is None:
            start = prev = p
        elif p == prev + 1:  # type: ignore[operator]
            prev = p
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = p
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


def ocr_with_ocrmypdf(pdf: Path, pages: list[int]) -> dict[int, str]:
    """Recognise just `pages` (the other pages pass through) and read their new text layer back."""
    import ocrmypdf

    with tempfile.TemporaryDirectory(prefix="blahaj-ocr-") as tmp:
        out = Path(tmp) / "ocr.pdf"
        ocrmypdf.ocr(pdf, out, language="eng", pages=_page_ranges(pages), skip_text=True, output_type="pdf", optimize=0,
                     progress_bar=False, deskew=False)  # fmt: skip
        texts = extract_pages(out)
    return {p: texts[p - 1] for p in pages if p - 1 < len(texts)}
