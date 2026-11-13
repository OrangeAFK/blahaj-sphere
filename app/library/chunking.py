"""~500-token chunks that never cross a page, so every chunk has one citable page number.

No tokenizer is installed (DECISIONS M5), so a token is estimated as 4 characters (the usual English rule of thumb).
"""

import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4
TARGET_TOKENS = 500
MAX_CHARS = TARGET_TOKENS * CHARS_PER_TOKEN
OVERLAP_CHARS = 200  # carried into the next chunk when one page has to be split
MIN_CHARS = 20  # a page with less text than this (after OCR) is skipped: a running header, a blank, a figure page

_PARA = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    page: int  # 1-based
    text: str


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"-\n(?=[a-z])", "", text)  # hyphenated line break inside a word
    return text.strip()


def _split_long(text: str) -> list[str]:
    """Hard split of text with no paragraph break inside the limit: at whitespace, with an overlap."""
    out, i = [], 0
    while i < len(text):
        j = min(i + MAX_CHARS, len(text))
        if j < len(text):
            k = text.rfind(" ", i + MAX_CHARS // 2, j)
            j = k if k > 0 else j
        out.append(text[i:j].strip())
        if j >= len(text):
            break
        i = max(j - OVERLAP_CHARS, i + 1)
        k = text.find(" ", i, j)  # start the overlap on a word boundary
        i = k + 1 if 0 <= k < j else i
    return [o for o in out if o]


def chunk_page(page: int, text: str) -> list[Chunk]:
    text = _clean(text)
    if len(text) < MIN_CHARS:
        return []
    if len(text) <= MAX_CHARS:
        return [Chunk(page, text)]
    pieces: list[str] = []
    for para in _PARA.split(text):
        para = para.strip()
        if not para:
            continue
        pieces += [para] if len(para) <= MAX_CHARS else _split_long(para)
    out, cur = [], ""
    for p in pieces:  # greedy: pack paragraphs up to the limit
        if cur and len(cur) + 2 + len(p) > MAX_CHARS:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return [Chunk(page, c) for c in out]


def chunk_pages(pages: list[str]) -> list[Chunk]:
    """`pages[i]` is the text of page i + 1."""
    out: list[Chunk] = []
    for i, t in enumerate(pages, start=1):
        out += chunk_page(i, t)
    return out
