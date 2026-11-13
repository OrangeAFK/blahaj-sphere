"""Library (SPEC §10): shared reference books. Upload a PDF -> ingest job (text per page, OCR fallback, ~500-token chunks, local
embeddings) -> cosine top-k retrieval with {book_id, page} citations. Nothing here ever leaves the machine; what a prompt
may carry is decided by the router's privacy toggle (SPEC §3)."""
