"""M9: library ingest (PyMuPDF text, OCR fallback, chunks, embeddings), retrieval, API + job, Builder and tutor wiring. Mock LLM, fake embedder."""

import hashlib
import json
import re
import time
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.builder.service import BuildService
from app.library import service as libsvc
from app.library.chunking import MAX_CHARS, MIN_CHARS, chunk_page, chunk_pages
from app.library.embed import normalize
from app.library.extract import _page_ranges, extract_pages, low_text_pages
from app.library.ingest import ingest_book
from app.library.search import LibraryIndex
from app.library.store import LibraryError, LibraryStore
from app.llm.types import LLMError
from app.main import create_app
from tests.helpers import TRACK as T, make_data
from tests.test_builder_m5 import FakeBuilder, OPTION, _build_router, data, fake_domain  # noqa: F401 - `data` is a fixture
from tests.test_lifecycle import Checker, env, make, to_lab  # noqa: F401 - `env` is a fixture

DIM = 128


class FakeEmbedder:
    """Hashed bag of words: texts sharing words are close, so retrieval is meaningful without the real model."""

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
        return v

    def embed_documents(self, texts):
        return normalize(np.array([self._vec(t) for t in texts]))

    def embed_query(self, text):
        return normalize(self._vec(text))


TOPICS = {
    1: "Low noise amplifier noise figure cascade Friis formula first stage gain dominates the system noise figure.",
    2: "Inductor quality factor Q substrate losses skin effect spiral inductors on silicon at millimetre wave frequencies.",
    3: "L-network impedance matching Smith chart series inductor shunt capacitor transforms a load resistance downward.",
    4: "Bipolar transistor biasing collector current density fT peak SiGe HBT transit frequency versus current.",
    5: "Phase noise in voltage controlled oscillators Leeson model tank quality factor flicker corner upconversion.",
}


def make_pdf(path: Path, pages: dict[int, str] | list[str], blank: tuple[int, ...] = ()) -> Path:
    texts = list(pages.values()) if isinstance(pages, dict) else pages
    doc = pymupdf.open()
    for i, t in enumerate(texts, start=1):
        page = doc.new_page()
        if i not in blank:
            page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), t, fontsize=10)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(libsvc, "embedder_factory", lambda root: emb)
    libsvc.reset()
    yield emb
    libsvc.reset()


_n = 0


def add_book(root: Path, tmp_path: Path, pages, title="RF Design Notes", blank=(), **ingest_kw):
    global _n
    _n += 1
    src = make_pdf(tmp_path / f"book{_n}.pdf", pages, blank)
    store = LibraryStore(root)
    book = store.create(src, f"{title}.pdf", title)
    ingest_book(store, book.id, libsvc.library_for(root).embedder, ocr_ready=ingest_kw.pop("ocr_ready", lambda: False), **ingest_kw)
    return store.require(book.id)


# ---- chunking --------------------------------------------------------------------------------------


def test_short_page_is_one_chunk_with_its_page_number():
    assert [(c.page, c.text) for c in chunk_page(7, "  A short page about noise figure.  ")] == [(7, "A short page about noise figure.")]


def test_nearly_empty_pages_are_skipped():
    assert chunk_page(1, "12") == [] and chunk_page(1, " " * 100) == [] and MIN_CHARS == 20


def test_long_page_splits_within_the_limit_and_never_crosses_a_page():
    paras = [f"Paragraph {i} " + "word " * 120 for i in range(8)]
    chunks = chunk_pages(["intro text for page one", "\n\n".join(paras), "closing text on page three"])
    assert chunks[0].page == 1 and chunks[-1].page == 3
    assert len([c for c in chunks if c.page == 2]) > 1 and all(len(c.text) <= MAX_CHARS for c in chunks)
    assert {c.page for c in chunks} == {1, 2, 3}


def test_a_page_with_no_paragraph_breaks_is_split_with_overlap():
    text = " ".join(f"w{i}" for i in range(2000))
    chunks = chunk_page(1, text)
    assert len(chunks) > 2 and all(len(c.text) <= MAX_CHARS for c in chunks)
    assert set(chunks[0].text.split()[-10:]) & set(chunks[1].text.split()[:60])  # consecutive chunks overlap
    assert chunks[-1].text.endswith("w1999")


def test_hyphenated_line_breaks_are_rejoined():
    assert chunk_page(1, "the amplifier ampli-\nfier has gain of ten decibels")[0].text.startswith("the amplifier amplifier")


# ---- extraction ------------------------------------------------------------------------------------


def test_extract_pages_and_low_text_detection(tmp_path):
    pages = extract_pages(make_pdf(tmp_path / "a.pdf", TOPICS, blank=(2, 4)))
    assert len(pages) == 5 and "Friis" in pages[0] and pages[1].strip() == ""
    assert low_text_pages(pages) == [2, 4]


def test_extract_rejects_non_pdf_and_encrypted(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    with pytest.raises(ValueError):
        extract_pages(bad)
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "secret " * 20)
    enc = tmp_path / "enc.pdf"
    doc.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    with pytest.raises(ValueError, match="password"):
        extract_pages(enc)


def test_page_ranges():
    assert _page_ranges([5, 1, 2, 3, 9, 10]) == "1-3,5,9-10" and _page_ranges([4]) == "4" and _page_ranges([]) == ""


# ---- ingest ----------------------------------------------------------------------------------------


def test_ingest_writes_index_and_marks_book_ready(data, tmp_path):
    b = add_book(data, tmp_path, TOPICS)
    assert (b.status, b.pages, b.chunks, b.ocr_pages, b.empty_pages) == ("ready", 5, 5, 0, 0)
    st = LibraryStore(data)
    rows = [json.loads(ln) for ln in st.chunks_path(b.id).read_text(encoding="utf-8").splitlines()]
    assert [r["page"] for r in rows] == [1, 2, 3, 4, 5] and all(r["book_id"] == b.id for r in rows)
    emb = np.load(st.embeddings_path(b.id))
    assert emb.shape == (5, DIM) and emb.dtype == np.float32 and np.allclose(np.linalg.norm(emb, axis=1), 1, atol=1e-5)
    assert (data / "library" / ".gitignore").read_text() == "*\n"  # the PDFs and vectors never reach the data repo


def test_ingest_reports_progress_monotonically(data, tmp_path):
    seen: list[float] = []
    add_book(data, tmp_path, [TOPICS[1 + i % 5] + f" extra {i}" for i in range(80)], progress=seen.append)
    assert seen == sorted(seen) and seen[-1] == 1.0 and len(seen) > 4


def test_ocr_fills_pages_with_almost_no_text(data, tmp_path):
    calls = []

    def ocr(pdf, pages):
        calls.append(list(pages))
        return {p: f"Scanned page {p} on inductor quality factor measured with a vector network analyzer." for p in pages}

    b = add_book(data, tmp_path, TOPICS, blank=(2, 4), ocr=ocr, ocr_ready=lambda: True)
    assert calls == [[2, 4]] and (b.ocr_pages, b.chunks, b.empty_pages, b.warnings) == (2, 5, 0, [])
    hit = libsvc.library_for(data).search("scanned inductor vector network analyzer", k=2)
    assert {h["page"] for h in hit} == {2, 4}


def test_missing_ocr_is_a_warning_not_a_failure(data, tmp_path):
    b = add_book(data, tmp_path, TOPICS, blank=(3,))
    assert b.status == "ready" and b.chunks == 4 and b.empty_pages == 1
    assert "1 pages have < 50 characters and OCR is not available" in b.warnings[0]


def test_crashing_ocr_keeps_the_extracted_text(data, tmp_path):
    def ocr(pdf, pages):
        raise RuntimeError("tesseract exploded")

    b = add_book(data, tmp_path, TOPICS, blank=(1,), ocr=ocr, ocr_ready=lambda: True)
    assert b.status == "ready" and b.chunks == 4 and "OCR failed (RuntimeError: tesseract exploded)" in b.warnings[0]


def test_a_pdf_with_no_text_at_all_fails_the_book_and_the_job(data, tmp_path):
    src = make_pdf(tmp_path / "scan.pdf", ["", "", ""], blank=(1, 2, 3))
    st = LibraryStore(data)
    book = st.create(src, "scan.pdf")
    with pytest.raises(ValueError, match="no text could be extracted"):
        ingest_book(st, book.id, FakeEmbedder(), ocr_ready=lambda: False)
    b = st.require(book.id)
    assert b.status == "failed" and "OCR is not available" in (b.error or "")


def test_store_ids_duplicates_and_delete(data, tmp_path):
    st = LibraryStore(data)
    one = make_pdf(tmp_path / "1.pdf", ["alpha page text here for hashing"])
    same = tmp_path / "same.pdf"
    same.write_bytes(one.read_bytes())
    a = st.create(one, "x.pdf", "Razavi RF Microelectronics")
    b = st.create(make_pdf(tmp_path / "2.pdf", ["beta page text here for hashing"]), "y.pdf", "Razavi RF Microelectronics")
    assert (a.id, b.id) == ("razavi-rf-microelectronics", "razavi-rf-microelectronics-2") and [x.id for x in st.books()] == [a.id, b.id]
    with pytest.raises(LibraryError) as e:
        st.create(same, "z.pdf")
    assert e.value.status == 409 and a.id in e.value.message
    st.delete(a.id)
    assert [x.id for x in st.books()] == [b.id]
    for bad in ("../x", "A b", ""):
        with pytest.raises(LibraryError):
            st.book_dir(bad)


def test_interrupted_ingests_are_failed_at_startup(data, tmp_path):
    st = LibraryStore(data)
    b = st.create(make_pdf(tmp_path / "1.pdf", ["some text for the interrupted book"]), "x.pdf")
    st.update(b.id, status="ingesting")
    assert st.fail_interrupted() == [b.id] and st.require(b.id).status == "failed"


# ---- retrieval -------------------------------------------------------------------------------------


def test_search_ranks_by_cosine_and_cites_book_and_page(data, tmp_path):
    a = add_book(data, tmp_path, TOPICS, title="RF Notes")
    b = add_book(data, tmp_path, {1: "Digital CMOS logic gates propagation delay and fanout of four inverter chains.",
                                  2: "Static timing analysis setup and hold slack across clock domains."}, title="Digital Book")  # fmt: skip
    idx = libsvc.library_for(data)
    hits = idx.search("noise figure of a cascade, Friis formula", k=5)
    assert len(hits) == 5 and (hits[0]["book_id"], hits[0]["page"]) == (a.id, 1) and hits[0]["title"] == "RF Notes"
    assert hits[0]["score"] > hits[1]["score"] > 0 and {"book_id", "page", "text", "score", "title"} <= set(hits[0])
    assert idx.search("propagation delay inverter fanout", k=1)[0]["book_id"] == b.id
    assert [h["book_id"] for h in idx.search("noise figure", k=5, book_ids={b.id})] == [b.id, b.id]
    assert len(idx.search("noise", k=2)) == 2


def test_search_default_is_top_five_and_blank_query_is_empty(data, tmp_path):
    assert libsvc.library_for(data).search("anything") == [] and libsvc.retrieve(data, "anything") == []
    add_book(data, tmp_path, [TOPICS[1 + i % 5] + f" number {i}" for i in range(12)])
    assert len(libsvc.retrieve(data, "inductor quality factor")) == 5
    assert libsvc.library_for(data).search("   ") == []


def test_empty_library_never_embeds_the_query(data):
    class Boom(FakeEmbedder):
        def embed_query(self, text):
            raise AssertionError("must not load the model for an empty library")

    assert LibraryIndex(data, Boom()).search("anything") == []


def test_index_reloads_when_a_book_is_added_or_deleted(data, tmp_path):
    idx = libsvc.library_for(data)
    assert len(idx) == 0
    a = add_book(data, tmp_path, TOPICS)
    assert len(idx) == 5
    LibraryStore(data).delete(a.id)
    assert len(idx) == 0 and idx.search("noise") == []


def test_retrieve_never_raises(data, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(LibraryIndex, "search", boom)
    assert libsvc.retrieve(data, "x") == []


# ---- API + job ---------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path / "d"))
    make_data(tmp_path / "d")
    with TestClient(create_app()) as c:
        yield c


def wait_for(c, book_id, want="ready"):
    b = {}
    for _ in range(200):
        b = next(b for b in c.get("/api/library").json()["books"] if b["id"] == book_id)
        if b["status"] == want:
            return b
        time.sleep(0.05)
    raise AssertionError(f"book never became {want}: {b}")


def upload(c, path: Path, name="notes.pdf", **params):
    return c.post("/api/library/books", params={"filename": name, **params}, content=path.read_bytes(), headers={"Content-Type": "application/pdf"})


def test_upload_ingest_list_search_delete(client, tmp_path):
    r = upload(client, make_pdf(tmp_path / "n.pdf", TOPICS), "notes.pdf", title="RF Notes")
    assert r.status_code == 202
    book, job_id = r.json()["book"], r.json()["job_id"]
    assert book["id"] == "rf-notes" and book["status"] in ("queued", "ingesting", "ready")
    done = wait_for(client, "rf-notes")
    assert (done["pages"], done["chunks"]) == (5, 5)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["type"] == "library_ingest" and job["node_id"] == "lib:rf-notes" and job["status"] == "done"
    lib = client.get("/api/library").json()
    assert lib["allow_library_to_free_models"] is False and isinstance(lib["ocr_available"], bool)
    hits = client.get("/api/library/search", params={"q": "phase noise oscillator Leeson", "k": 2}).json()
    assert len(hits) == 2 and (hits[0]["book_id"], hits[0]["page"]) == ("rf-notes", 5)
    assert client.delete("/api/library/books/rf-notes").status_code == 204
    assert client.get("/api/library").json()["books"] == [] and not (tmp_path / "d" / "library" / "rf-notes").exists()
    assert client.delete("/api/library/books/rf-notes").status_code == 404


def test_upload_rejects_non_pdf_and_duplicates_and_leaves_no_temp_files(client, tmp_path):
    assert client.post("/api/library/books", params={"filename": "x.pdf"}, content=b"not a pdf at all").status_code == 422
    pdf = make_pdf(tmp_path / "n.pdf", TOPICS)
    assert upload(client, pdf).status_code == 202
    r = upload(client, pdf, "again.pdf")
    assert r.status_code == 409 and "already in the library" in r.json()["detail"]
    wait_for(client, "notes")
    assert [p.name for p in (tmp_path / "d" / "library").iterdir() if p.name.startswith(".upload-")] == []


def test_a_bad_pdf_fails_the_book_and_can_be_deleted(client, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
    assert upload(client, bad, "bad.pdf").status_code == 202
    b = wait_for(client, "bad", want="failed")
    assert "not a readable PDF" in b["error"]
    assert client.delete("/api/library/books/bad").status_code == 204


def test_a_book_being_ingested_cannot_be_deleted(client, tmp_path):
    st = LibraryStore(tmp_path / "d")
    b = st.create(make_pdf(tmp_path / "n.pdf", TOPICS), "n.pdf")
    st.update(b.id, status="ingesting")
    assert client.delete(f"/api/library/books/{b.id}").status_code == 409


# ---- Builder -----------------------------------------------------------------------------------------


def _inputs(req) -> dict:
    return json.loads(req.messages[0]["content"].split("```json\n", 1)[1].split("\n```", 1)[0])


def test_builder_gets_top5_chunks_and_records_which_pages_it_cited(data, tmp_path):
    book = add_book(data, tmp_path, [TOPICS[1 + i % 5] + f" detail {i}" for i in range(10)], title="RF Notes")
    dom, state = fake_domain(tmp_path)

    class Citing(FakeBuilder):
        def __call__(self, req):
            out = super().__call__(req)
            self.chunks = _inputs(req)["learner"]["library_chunks"]
            first = self.chunks[0]
            with (Path(req.cwd) / "node.md").open("a", encoding="utf-8") as f:
                f.write(f"\nFurther reading: ({first['book_id']}, p. {first['page']}) and an invented one ({first['book_id']}, p. 999).\n")
            return out

    fb = Citing(state)
    router, ad = _build_router(tmp_path, fb)
    svc = BuildService(data, T, router, dom)
    rec = svc.run(svc.prepare(OPTION))
    assert rec["passed"]
    assert len(fb.chunks) == 5 and all(set(c) == {"book_id", "page", "text"} and c["book_id"] == book.id for c in fb.chunks)
    lib = rec["library"]
    assert len(lib["retrieved"]) == 5 and lib["cited"] == [{"book_id": book.id, "page": fb.chunks[0]["page"]}]
    assert lib["unknown_citations"] == [{"book_id": book.id, "page": 999}]  # recorded, never counted as a citation
    assert ad.requests[0].task_type == "build_challenge"


def test_builder_without_a_library_sends_no_chunks(data, tmp_path):
    dom, state = fake_domain(tmp_path)
    fb = FakeBuilder(state)
    router, _ = _build_router(tmp_path, fb)
    svc = BuildService(data, T, router, dom)
    rec = svc.run(svc.prepare(OPTION))
    assert '"library_chunks": []' in fb.prompts[0] and rec["library"] == {"retrieved": [], "cited": [], "unknown_citations": []}


def test_builder_library_text_flag_follows_the_chunks(data, tmp_path):
    add_book(data, tmp_path, TOPICS)
    dom, state = fake_domain(tmp_path)
    router, _ = _build_router(tmp_path, FakeBuilder(state))
    flags = []
    orig = router.complete

    def spy(task, *a, **k):
        flags.append((task, k.get("library_text")))
        return orig(task, *a, **k)

    router.complete = spy
    svc = BuildService(data, T, router, dom)
    svc.run(svc.prepare(OPTION))
    assert ("build_challenge", True) in flags and all(f for t, f in flags if t == "build_challenge")
    assert all(not f for t, f in flags if t != "build_challenge")  # the fidelity reviewer never gets library text


# ---- tutor -------------------------------------------------------------------------------------------


def lab_env(env, tmp_path, script):
    lc, ad, _ = make(env, tmp_path, script)
    to_lab(lc)
    return lc, ad


def _tutor_inputs(req) -> dict:
    return json.loads(req.messages[1]["content"].split("INPUTS (JSON):\n", 1)[1].split("\n\nCHAT SO FAR", 1)[0])


def test_tutor_gets_library_excerpts_and_only_valid_citations_are_kept(env, tmp_path):
    book = add_book(env, tmp_path, TOPICS, title="RF Notes")
    reply = {"reply": "Think about which stage dominates the cascade (see the book).", "hint": None,
             "citations": [{"book_id": book.id, "page": 1}, {"book_id": book.id, "page": 99}, {"book_id": "nope", "page": 1}, "junk"]}  # fmt: skip
    lc, ad = lab_env(env, tmp_path, {"tutor_chat": [reply]})
    out = lc.tutor.chat(lc.store.find(901), "why does the first stage dominate the noise figure cascade?")
    lib = _tutor_inputs(ad.requests[-1])["library"]
    assert len(lib) == 5 and all(set(c) == {"book_id", "page", "text"} for c in lib)
    assert (lib[0]["book_id"], lib[0]["page"]) == (book.id, 1)
    assert out["citations"] == [{"book_id": book.id, "page": 1, "title": "RF Notes"}]
    assert lc.tutor.history(lc.store.find(901))["messages"][-1]["citations"] == out["citations"]


def test_tutor_without_a_library_has_an_empty_library_context(env, tmp_path):
    lc, ad = lab_env(env, tmp_path, {"tutor_chat": [{"reply": "What sets the corner?", "hint": None}]})
    out = lc.tutor.chat(lc.store.find(901), "help")
    assert out["citations"] == [] and _tutor_inputs(ad.requests[-1])["library"] == []


def test_tutor_drops_library_text_when_no_backend_may_see_it(env, tmp_path):
    add_book(env, tmp_path, TOPICS)
    lc, _ = lab_env(env, tmp_path, {})
    calls = []

    def complete(task, messages, schema=None, node_id=None, library_text=False, **k):
        calls.append((library_text, '"library": []' in messages[1]["content"]))
        if library_text:
            raise LLMError("tutor_chat: all backends failed or skipped: openrouter skipped (library text not allowed)")
        return type("R", (), {"json": {"reply": "plain reply", "hint": None}, "model": "free"})()

    lc.tutor.router.complete = complete
    out = lc.tutor.chat(lc.store.find(901), "noise figure cascade?")
    assert calls == [(True, False), (False, True)] and out["reply"] == "plain reply" and out["citations"] == []


def test_a_real_router_with_the_toggle_off_keeps_library_text_off_free_models(env, tmp_path):
    from app.domains import Domain
    from app.lifecycle.service import Lifecycle
    from app.llm.config import load_routing
    from app.llm.router import Router
    from app.llm.store import Store
    from tests.test_onboarding import ALL, Scripted

    add_book(env, tmp_path, TOPICS)
    adapters = {k: Scripted({}) for k in ALL}
    adapters["claude_code"] = Scripted({"tutor_chat": [{"reply": "ok", "hint": None}]})
    router = Router(load_routing(), Store(tmp_path / "llm.db"), adapters, allow_library_to_free=lambda: False)
    lc = Lifecycle(env, T, router, Domain(name="rfic", image="x", image_size_gb=0, run_checker=Checker()))
    to_lab(lc)
    lc.tutor.chat(lc.store.find(901), "noise figure cascade?")
    assert len(adapters["claude_code"].requests) == 1 and _tutor_inputs(adapters["claude_code"].requests[0])["library"]
    assert all(not a.requests for k, a in adapters.items() if k != "claude_code")
