# BlahajSphere — agent rules

- Spec: SPEC.md. Follow §0. Load ONLY §0, §5, and the sections listed for the current milestone in §14. Never read the whole spec.
- Record every deviation, VERIFY outcome, and non-obvious choice in DECISIONS.md (one line each: date, milestone, decision, reason).
- Work on one milestone per session. Stop when its acceptance criteria pass; report results.
- Tests use the `mock` LLM adapter. Never call live LLMs in automated tests.
- Never call paid APIs. Never put RFIC-specific code outside domains/rfic/.
- Host is Windows 11; tools run in Docker (WSL2 backend). Use `uv` for Python, npm for ui/.
- Commands: backend tests `uv run pytest`; UI tests `cd ui && npm test`; run app `uv run python -m app`.
- Ask before: adding a dependency not in SPEC §2, changing a §5 schema, or deleting files.