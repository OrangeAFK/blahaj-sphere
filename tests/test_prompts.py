"""SPEC §15 prompt packs: every pack exists, stays <= 800 tokens, names its output schema; grader prompts are frozen (§8.5)."""

import hashlib
from pathlib import Path

import pytest

PROMPTS = Path(__file__).resolve().parents[1] / "app" / "prompts"
CHARS_PER_TOKEN = 3.5  # conservative for English prose (~4 chars/token); no tokenizer is installed (DECISIONS M5)
MAX_TOKENS = 800

# pack -> keys of its output schema (§5 where one exists) that the prompt must spell out
PACKS = {
    "interviewer": ["message", "done", "goals"],
    "framework_drafter": ["domain_pack", "domains"],
    "diagnostician": ["subskill", "question", "anchors"],
    "diagnostic_grader": ["items", "score", "quote", "rationale", "misconceptions"],
    "planner": ["options", "title", "why_now", "kind", "domain", "subskills", "tier", "size_est_min"],  # §5.6
    "rescoper": ["action", "reason_class", "changes", "option"],
    "tutor": ["reply", "hint", "subskill", "index"],
    "show_me": ["explanation", "subskills"],
    "examiner": ["say", "item_id", "kind"],
    "oral_grader": ["items", "score", "quote", "rationale", "misconceptions"],  # §5.7
    "prelab_explain_grader": ["items", "score", "quote", "rationale", "misconceptions"],  # §5.7
    "exam_writer": ["title", "brief", "items", "anchors", "probes", "key_terms"],  # tier exam key (M10)
    "summarizer": ["## <id> <title>"],  # Markdown entry, no JSON
    "fidelity_reviewer": ["checks", "method_fidelity", "metrics_simulated", "no_answers_in_starter", "pass", "file", "quote", "reason"],
}

# SPEC §8.5: editing a grader prompt fails this test on purpose. Log the change in docs/DECISIONS.md with the new hash.
FROZEN = {
    "diagnostic_grader": "32c6c91aa56ccc498b2f60b872e718855b00a148e562e72355b52dab8afe92e7",
    "oral_grader": "f3e44ebb9b979b08d0483159660961546fc52ad858665b859b129cbb546f566a",
    "prelab_explain_grader": "e508de7e982ae06ec2c7136c7882c500ce615b695fa4871b2371b59d839d3499",
    "fidelity_reviewer": "8831ef3491c2b89b832abf4841afd5ae7b8eff6c88e7552c547156b40cd7f0fe",  # release-gate judge (M5.1)
}


def _text(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_every_spec_pack_exists_and_nothing_else():
    assert {p.stem for p in PROMPTS.glob("*.md")} == set(PACKS)


@pytest.mark.parametrize("name", sorted(PACKS))
def test_pack_is_within_token_budget_and_names_its_output(name):
    text = _text(name)
    assert len(text) / CHARS_PER_TOKEN <= MAX_TOKENS, f"{name}: ~{len(text) / CHARS_PER_TOKEN:.0f} tokens"
    assert "# Role" in text and "# Output" in text
    missing = [k for k in PACKS[name] if k not in text]
    assert not missing, f"{name} does not name output keys {missing}"


@pytest.mark.parametrize("name", ["tutor", "examiner"])
def test_tutor_and_examiner_are_told_to_never_reveal_hidden_values(name):
    text = _text(name).lower()
    assert "# forbidden" in text and ("target values" in text or "correct answers" in text)


def test_examiner_never_receives_anchors():
    text = _text("examiner")
    inputs = text.split("# Inputs", 1)[1].split("#", 1)[0]
    assert "anchors" not in inputs


@pytest.mark.parametrize("name", sorted(FROZEN))
def test_grader_prompt_is_frozen(name):
    assert hashlib.sha256(_text(name).encode()).hexdigest() == FROZEN[name]


BUILDER_MAX_TOKENS = 5000  # §15 exception for domains/rfic/prompts/builder.md (DECISIONS M5.1): a ceiling, not 800


def test_builder_prompt_stays_under_its_section_15_exception_ceiling():
    text = (PROMPTS.parents[1] / "domains" / "rfic" / "prompts" / "builder.md").read_text(encoding="utf-8")
    assert len(text) / CHARS_PER_TOKEN <= BUILDER_MAX_TOKENS, f"builder.md ~{len(text) / CHARS_PER_TOKEN:.0f} tokens"
    assert "METRIC_SOURCES" in text and "Fidelity review" in text
