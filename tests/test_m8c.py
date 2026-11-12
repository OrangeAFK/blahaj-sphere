"""M8.1: hidden `key_terms` per oral_key item (SPEC §5.4): gate requirement, coverage matching, the extra probe trigger, and secrecy
from the examiner (and from every learner-facing view). Mock LLM."""

import json

import pytest
import yaml

from app.builder.gate import KEY_TERMS_MAX, KEY_TERMS_MIN, run_gate
from app.datastore.layout import DataLayout
from app.lifecycle.oral import key_term_coverage, misses_key_terms
from tests.helpers import TRACK as T
from tests.test_gate import FakeRunner, _y, node  # noqa: F401 (node = fixture)
from tests.test_lifecycle import env  # noqa: F401 (fixture)
from tests.test_lifecycle import make
from tests.test_oral import CLEAN, FULL, KEY, NODE, THIN, examiner_calls, oral_env, run_exam, to_oral  # noqa: F401

TERMS_O1 = ["quality factor", "reflection coefficient", "bandwidth", "resonance"]
TERMS_O2 = ["Friis", "gain", "cascade"]
OFF_TOPIC = ("the resistor and the capacitor together set the time constant of the first order response, and the pole sits where the reactance "
             "equals the resistance in the simple divider")  # >= 25 words, no hedge, none of the terms
ALL_TERMS = TERMS_O1 + TERMS_O2


def with_key_terms(oral_env):
    key = [{**it} for it in KEY]
    key[0]["key_terms"], key[1]["key_terms"] = TERMS_O1, TERMS_O2  # o7 (interleaved) has none: older-node behaviour
    (DataLayout(oral_env, T).nodes_dir / NODE / "_hidden" / "oral_key.yaml").write_text(yaml.safe_dump({"items": key}, sort_keys=False))


# ---- gate -----------------------------------------------------------------------------------------------------------------------


def test_a_well_formed_key_passes_g0(node):
    res = run_gate(node, FakeRunner())
    assert not [e for e in res.errors if "key_terms" in e]


@pytest.mark.parametrize("bad", [
    None, "corner", [], ["a", "b"], ["a", "b", "c", "d", "e", "f", "g", "h", "i"], ["a", "b", ""], ["a", "b", 3], ["Corner", "corner", "x"], ["Time  Constant", "time constant", "x"],
], ids=["missing", "string", "empty", "two", "nine", "blank", "non-string", "dup-case", "dup-space"])  # fmt: skip
def test_gate_rejects_malformed_key_terms(node, bad):
    ok = yaml.safe_load((node / "_hidden" / "oral_key.yaml").read_text())
    if bad is None:
        ok["items"][0].pop("key_terms")
    else:
        ok["items"][0]["key_terms"] = bad
    _y(node / "_hidden" / "oral_key.yaml", ok)
    errs = [e for e in run_gate(node, FakeRunner()).errors if "key_terms" in e]
    assert len(errs) == 1 and "oral_key.yaml o1" in errs[0] and f"{KEY_TERMS_MIN}-{KEY_TERMS_MAX}" in errs[0]


def test_builder_prompt_and_spec_describe_key_terms():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "key_terms" in (root / "domains/rfic/prompts/builder.md").read_text(encoding="utf-8")
    spec = (root / "docs/SPEC.md").read_text(encoding="utf-8")
    assert "key_terms:" in spec and "fewer than half" in spec


# ---- coverage matching -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer, terms, hit", [
    ("The Quality Factor sets it", ["quality factor"], 1),  # case-insensitive
    ("the quality-factor, honestly", ["quality factor"], 1),  # punctuation
    ("we de embed with an open short", ["de-embedding", "open-short"], 1),  # open-short ~ "open short"; "de embed" is not "de embedding"
    ("it is de embedding", ["de-embedding"], 1),  # hyphen vs space
    ("the pad capacitances matter", ["capacitance"], 1),  # inflection (3+ letters: substring on the squashed text)
    ("the quality of the match", ["Q"], 0),  # a one-letter term only matches as a whole word
    ("Q is loaded", ["Q"], 1),
    ("my gmail is down", ["gm"], 0),
    ("the gm of the device", ["gm"], 1),
    ("S11 is low", ["S11"], 1),
    ("", ["a thing"], 0),
    ("anything", [], 0),
])
def test_key_term_hits(answer, terms, hit):
    assert key_term_coverage(answer, terms) == (hit, len(terms))


@pytest.mark.parametrize("hits, total, misses", [(0, 4, True), (1, 4, True), (2, 4, False), (4, 4, False), (1, 3, True), (2, 3, False), (0, 0, False), (1, 1, False), (0, 1, True)])
def test_fewer_than_half_is_a_miss_exactly_half_is_enough(hits, total, misses):
    terms = [f"term{chr(97 + i)}x" for i in range(total)]
    assert misses_key_terms(" ".join(terms[:hits]), terms) is misses


def test_items_without_key_terms_never_miss():
    assert misses_key_terms("", None) is False and misses_key_terms("anything", []) is False


# ---- the probe trigger -----------------------------------------------------------------------------------------------------------------


def probe_script(n=4):
    return {"oral_dialogue": [{"say": "Why does that hold?", "item_id": "o1", "kind": "probe"}] * n}


def start(oral_env, tmp_path, script=None):
    with_key_terms(oral_env)
    lc, ad, _ = make(oral_env, tmp_path, script or {})
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    return lc, ad, ref


def test_a_long_confident_answer_that_misses_the_key_terms_is_probed(oral_env, tmp_path):
    lc, ad, ref = start(oral_env, tmp_path, probe_script())
    out = lc.oral.answer(ref, OFF_TOPIC)["examiner"]
    assert (out["kind"], out["source"], out["text"]) == ("probe", "llm", "Why does that hold?") and len(examiner_calls(ad)) == 1
    turn = lc.oral._state(ref)["turns"][-1]
    assert turn["trigger"] == "key_terms"


def test_an_answer_covering_half_or_more_of_the_terms_goes_straight_on(oral_env, tmp_path):
    lc, ad, ref = start(oral_env, tmp_path, probe_script())
    out = lc.oral.answer(ref, FULL)["examiner"]  # uses quality factor, reflection coefficient, bandwidth: 3 of 4
    assert (out["kind"], out["source"], out["text"]) == ("question", "key", "Question 2?") and examiner_calls(ad) == []


def test_the_thin_and_hedged_triggers_still_work_and_say_so(oral_env, tmp_path):
    lc, ad, ref = start(oral_env, tmp_path, probe_script())
    lc.oral.answer(ref, FULL + ", but I am not sure")  # hedged although the terms are covered
    assert lc.oral._state(ref)["turns"][-1]["trigger"] == "thin_or_hedged" and len(examiner_calls(ad)) == 1


def test_coverage_is_cumulative_over_the_item_so_a_good_probe_answer_ends_the_probing(oral_env, tmp_path):
    lc, ad, ref = start(oral_env, tmp_path, probe_script())
    assert lc.oral.answer(ref, OFF_TOPIC)["examiner"]["kind"] == "probe"
    nxt = lc.oral.answer(ref, "the quality factor and the bandwidth are linked through the resonance, " + OFF_TOPIC)["examiner"]  # now 3 of 4
    assert (nxt["kind"], nxt["text"]) == ("question", "Question 2?") and len(examiner_calls(ad)) == 1


def test_probes_stay_capped_at_two_per_item_under_the_term_trigger(oral_env, tmp_path):
    lc, ad, ref = start(oral_env, tmp_path, probe_script(5))
    kinds = [lc.oral.answer(ref, OFF_TOPIC)["examiner"]["kind"] for _ in range(3)]
    assert kinds == ["probe", "probe", "question"] and len(examiner_calls(ad)) == 2


def test_the_examiner_cannot_cancel_a_term_probe_but_the_keys_own_probe_is_used(oral_env, tmp_path):
    lc, ad, ref = start(oral_env, tmp_path, {"oral_dialogue": [{"say": "", "item_id": "o1", "kind": "none"}] * 3})
    out = lc.oral.answer(ref, OFF_TOPIC)["examiner"]  # "none" overridden: the answer missed the terms
    assert (out["kind"], out["source"], out["text"]) == ("probe", "fallback", "Probe 1a?")
    out = lc.oral.answer(ref, THIN)["examiner"]  # a THIN answer is the old trigger (not the term trigger): the examiner's "none" is honoured
    assert out["kind"] == "question" and out["text"] == "Question 2?"


def test_outage_falls_back_to_the_keys_probes_for_a_term_miss(oral_env, tmp_path):
    lc, _, ref = start(oral_env, tmp_path)  # no examiner at all
    outs = [lc.oral.answer(ref, OFF_TOPIC)["examiner"] for _ in range(3)]
    assert [(o["kind"], o["source"], o["text"]) for o in outs] == [("probe", "fallback", "Probe 1a?"), ("probe", "fallback", "Probe 1b?"), ("question", "key", "Question 2?")]


def test_nodes_without_key_terms_are_unaffected(oral_env, tmp_path):
    lc, ad, _ = make(oral_env, tmp_path, probe_script())  # the stock KEY of test_oral has no key_terms
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    assert lc.oral.answer(ref, OFF_TOPIC)["examiner"]["text"] == "Question 2?" and examiner_calls(ad) == []


# ---- secrecy ---------------------------------------------------------------------------------------------------------------------------------


def test_the_examiner_never_receives_key_terms_or_anchors(oral_env, tmp_path):
    lc, ad, ref = start(oral_env, tmp_path, probe_script())
    lc.oral.answer(ref, OFF_TOPIC)  # key_terms trigger
    lc.oral.answer(ref, THIN)  # thin trigger
    reqs = examiner_calls(ad)
    assert len(reqs) == 2
    for r in reqs:
        blob = json.dumps(r.messages)
        assert "key_terms" not in blob and "anchors" not in blob and "ANCHOR-SECRET" not in blob
        assert not [t for t in ALL_TERMS if t in json.dumps(json.loads(r.messages[1]["content"])["item"])]  # the item part carries question + probes only
    sent = json.loads(reqs[0].messages[1]["content"])
    assert set(sent["item"]) == {"id", "question", "probes", "interleaved"}


def test_no_learner_facing_view_exposes_key_terms(oral_env, tmp_path):
    lc, _, ref = start(oral_env, tmp_path)
    run_exam(lc, ref, answers=[OFF_TOPIC])
    views = json.dumps([lc.oral.view(ref), lc.oral_info(901)])
    assert "key_terms" not in views and "trigger" not in views
    assert not [t for t in ALL_TERMS if t.lower() in views.lower()]  # no term text anywhere in what the learner can fetch
