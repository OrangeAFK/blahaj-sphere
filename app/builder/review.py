"""Release gate step 7: fidelity review (DECISIONS M5.1). One heavy no-tool call (minimal prompt, pinned Sonnet 5.5) reads
the option and the node files and returns pass/fail with verbatim quotes on three checks the code gates cannot judge:
method/PDK fidelity to the option, every metric simulated (not read back), no answers in the starter.

Quotes are verified by code; a failing check stands even if its quote is not verbatim (conservative)."""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.llm.types import LLMError

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "fidelity_reviewer.md"
TASK = "review_build"
CHECK_IDS = ("method_fidelity", "metrics_simulated", "no_answers_in_starter")
SCHEMA = {"type": "object", "properties": {"checks": {"type": "array", "minItems": 3, "maxItems": 3}}, "required": ["checks"]}
FILE_CAP = 12_000  # chars per file sent to the reviewer
TOTAL_CAP = 90_000
REVIEWED = ("node.md", "prelab.md", "checker.py", "_hidden/prelab_key.yaml")


class ReviewUnavailable(Exception):
    """The reviewer could not produce a usable verdict (backend down, malformed twice): not the Builder's fault."""


@dataclass
class ReviewResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    model: str = ""
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def review_files(node: Path) -> dict[str, str]:
    """The files the reviewer sees: the node's text files (workspace and reference included), capped."""
    paths = [node / r for r in REVIEWED]
    for d in ("workspace", "_hidden/reference_solution"):
        paths += sorted(p for p in (node / d).rglob("*") if p.is_file())
    out: dict[str, str] = {}
    total = 0
    for p in paths:
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > FILE_CAP:
            text = text[:FILE_CAP] + "\n[... truncated]"
        if total + len(text) > TOTAL_CAP:
            break
        out[p.relative_to(node).as_posix()] = text
        total += len(text)
    return out


def _validate(raw) -> list[dict] | None:
    checks = raw.get("checks") if isinstance(raw, dict) else None
    if not isinstance(checks, list):
        return None
    by_id = {c.get("id"): c for c in checks if isinstance(c, dict)}
    if set(by_id) != set(CHECK_IDS):
        return None
    for c in by_id.values():
        if not isinstance(c.get("pass"), bool) or not all(isinstance(c.get(k), str) for k in ("file", "quote", "reason")):
            return None
    return [by_id[i] for i in CHECK_IDS]


def run_review(router, node_dir: Path, option: dict, metrics: dict, sources: dict, domain_pack: str, node_id: int | None = None) -> ReviewResult:
    node = Path(node_dir)
    files = review_files(node)
    option_text = json.dumps(option, ensure_ascii=False)
    system = {"role": "system", "content": PROMPT.read_text(encoding="utf-8").replace("{domain_pack}", domain_pack)}
    user = {"role": "user", "content": json.dumps({"option": option, "files": files, "metrics": metrics, "metric_sources": sources}, ensure_ascii=False)}
    msgs = [system, user]
    checks = None
    res = None
    for _ in range(2):  # one repair attempt for malformed output
        try:
            res = router.complete(TASK, msgs, schema=SCHEMA, node_id=node_id)
        except LLMError as e:
            raise ReviewUnavailable(f"fidelity review call failed: {e}") from e
        checks = _validate(res.json)
        if checks is not None:
            break
        msgs = [system, user, {"role": "assistant", "content": res.text},
                {"role": "user", "content": f"Invalid: return JSON with exactly the checks {list(CHECK_IDS)}, each with pass (bool), file, quote, reason."}]
    if checks is None or res is None:
        raise ReviewUnavailable("fidelity review returned unusable output twice")

    out = ReviewResult(passed=True, checks=checks, model=res.model, usage=res.usage, latency_ms=res.latency_ms)
    for c in checks:
        src = option_text if c["file"] == "option" else files.get(c["file"], "")
        verbatim = bool(c["quote"].strip()) and _norm(c["quote"]) in _norm(src)
        c["quote_verified"] = verbatim
        if c["pass"]:
            if not verbatim:
                out.warnings.append(f"fidelity review {c['id']}: passing quote not found verbatim in {c['file']!r}")
            continue
        out.passed = False
        note = "" if verbatim else " (quote not found verbatim; the verdict stands)"
        out.errors.append(f"gate 7 (fidelity review) {c['id']}: {c['reason']} [{c['file']}: \"{c['quote'][:300]}\"]{note}")
    return out
