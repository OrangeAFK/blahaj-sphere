"""Competency engine (SPEC §8.2-8.4): pure unit tests."""

import pytest

from app.competency.engine import (
    Event,
    base_value_weight,
    effective,
    exam_unlocked,
    initial_tier,
    mastery,
    numeric_value,
    score_domain,
    tier_history,
)

_n = 0


def ev(type="oral_item", v=1.0, w=1.0, *, domain="matching", sub="matching.l_network", tier=1, node=1, **kw) -> Event:
    global _n
    _n += 1
    return Event(id=f"e{_n}", ts="2026-11-10T00:00:00-06:00", node_id=node, domain=domain, subskill=sub, tier=tier, type=type, value=v, weight=w, **kw)


# --- §8.2: every row --------------------------------------------------------------------------

def test_proof_pass_row():
    assert base_value_weight("proof_pass") == (1.0, 1.0)


@pytest.mark.parametrize("type", ["oral_item", "tier_exam_item", "prelab_explain"])
@pytest.mark.parametrize("score,v", [(0, 0.0), (1, 1 / 3), (2, 2 / 3), (3, 1.0)])
def test_scored_rows(type, score, v):
    assert base_value_weight(type, score=score) == (pytest.approx(v), 1.0)


def test_diagnostic_row_and_exclusion_from_mastery():
    assert base_value_weight("diagnostic_item", score=2) == (pytest.approx(2 / 3), 1.0)
    evs = [ev("diagnostic_item", 1.0)] * 5
    s = score_domain(evs, "matching")
    assert s.mastery == 0 and s.n_window == 0  # excluded from mastery


def test_score_out_of_range_rejected():
    for bad in (-1, 3.5, None):
        with pytest.raises(ValueError):
            base_value_weight("oral_item", score=bad)


def test_prelab_numeric_row():
    assert base_value_weight("prelab_numeric", err=0.1, tol=0.15) == (1.0, 0.5)  # within tol
    assert numeric_value(0.15, 0.15) == 1.0  # boundary is within
    assert numeric_value(-0.15, 0.15) == 1.0  # sign irrelevant
    assert numeric_value(0.3, 0.15) == pytest.approx(1 - 0.3 / 0.45)  # 1 - |err|/(3 tol)
    assert numeric_value(0.45, 0.15) == pytest.approx(0.0)
    assert numeric_value(5.0, 0.15) == 0.0  # clamped at 0
    assert numeric_value(0.0, 0.0) == 1.0 and numeric_value(0.1, 0.0) == 0.0  # zero tolerance does not divide by zero


def test_unknown_type_rejected():
    with pytest.raises(ValueError):
        base_value_weight("nope")


def test_interleaved_multiplies_weight_by_1_5():
    assert effective(ev(v=0.8, w=1.0, interleaved=True)) == (pytest.approx(0.8), pytest.approx(1.5))
    assert effective(ev("prelab_numeric", 1.0, 0.5, interleaved=True)) == (1.0, pytest.approx(0.75))


def test_assist_reveal_multiplies_value_by_0_7():
    assert effective(ev(v=1.0, assist_reveal=True)) == (pytest.approx(0.7), 1.0)


def test_both_modifiers():
    assert effective(ev(v=1.0, w=1.0, interleaved=True, assist_reveal=True)) == (pytest.approx(0.7), pytest.approx(1.5))


def test_interleaved_shifts_mastery():
    plain = mastery([ev(v=1.0), ev(v=0.0)])  # 100*1/(2+3) = 20
    heavy = mastery([ev(v=1.0, interleaved=True), ev(v=0.0)])  # 100*1.5/(2.5+3)
    assert plain == pytest.approx(20.0) and heavy == pytest.approx(100 * 1.5 / 5.5)


# --- §8.3 ---------------------------------------------------------------------------------------

def test_reference_13_events_at_0_8_is_65():
    assert mastery([ev(v=0.8) for _ in range(13)]) == pytest.approx(65.0)


def test_empty_is_zero():
    assert mastery([]) == 0.0


def test_window_is_most_recent_20():
    old = [ev(v=0.0, sub=f"matching.s{i}") for i in range(10)]
    new = [ev(v=1.0) for _ in range(20)]
    s = score_domain(old + new, "matching")
    assert s.n_window == 20 and s.n_since_tier == 30
    assert s.mastery == pytest.approx(100 * 20 / 23)  # the 10 old zeros fell out of the window


def test_window_counts_events_not_weight():
    evs = [ev("prelab_numeric", 1.0, 0.5) for _ in range(25)]
    assert score_domain(evs, "matching").n_window == 20


def test_events_above_current_tier_excluded():
    evs = [ev(v=1.0, tier=1), ev(v=1.0, tier=2)]  # at tier 1, the tier-2 event does not count
    s = score_domain(evs, "matching")
    assert s.n_window == 1 and s.mastery == pytest.approx(25.0)


def test_other_domains_excluded():
    evs = [ev(v=1.0), ev(v=1.0, domain="noise", sub="noise.nf")]
    assert score_domain(evs, "matching").n_window == 1


def _exam(node, scores, tier=1, domain="matching"):
    return [ev("tier_exam_item", s / 3, domain=domain, sub=f"matching.s{i % 3}", tier=tier, node=node) for i, s in enumerate(scores)]


def test_window_only_since_reaching_tier():
    before = [ev(v=1.0) for _ in range(5)]
    exam = _exam(100, [3] * 6)
    after = [ev(v=0.5, tier=2)]
    s = score_domain(before + exam + after, "matching")
    assert s.tier == 2 and s.n_window == 1 and s.mastery == pytest.approx(100 * 0.5 / 4)


def test_lower_tier_events_after_reaching_tier_still_count():
    evs = _exam(100, [3] * 6) + [ev(v=1.0, tier=1), ev(v=1.0, tier=2)]  # remediation at tier 1, then tier 2
    assert score_domain(evs, "matching").n_window == 2


# --- §8.4 ---------------------------------------------------------------------------------------

def test_unlock_needs_mastery_and_three_subskills():
    assert exam_unlocked(65.0, 3, 1)
    assert not exam_unlocked(64.9, 3, 1)
    assert not exam_unlocked(90.0, 2, 1)
    assert not exam_unlocked(65.0, 3, 5)  # no tier above 5


def test_unlock_via_reference_events():
    subs = ["matching.a", "matching.b", "matching.c"]
    s = score_domain([ev(v=0.8, sub=subs[i % 3]) for i in range(13)], "matching")
    assert s.mastery == pytest.approx(65.0) and s.subskills_evidenced == tuple(subs) and s.unlocked


def test_no_unlock_with_two_subskills_even_at_high_mastery():
    s = score_domain([ev(v=1.0, sub=f"matching.{'ab'[i % 2]}") for i in range(30)], "matching")
    assert s.mastery > 65 and not s.unlocked


def test_no_unlock_below_65():
    s = score_domain([ev(v=0.8, sub=f"matching.s{i % 3}") for i in range(12)], "matching")
    assert s.mastery < 65 and not s.unlocked


def test_unlock_has_no_time_condition():
    old = "1999-01-01T00:00:00+00:00"
    evs = [Event(f"x{i}", old, 1, "matching", f"matching.s{i % 3}", 1, "oral_item", 0.8, 1.0) for i in range(13)]
    assert score_domain(evs, "matching").unlocked


def test_subskills_evidenced_must_be_at_tier_T():
    evs = _exam(100, [3] * 6) + [ev(v=1.0, sub="matching.a", tier=2), ev(v=1.0, sub="matching.b", tier=1), ev(v=1.0, sub="matching.c", tier=1)]
    assert score_domain(evs, "matching").subskills_evidenced == ("matching.a",)


def test_exam_pass_promotes_and_resets_mastery():
    evs = [ev(v=1.0) for _ in range(13)] + _exam(100, [3, 3, 2, 3, 2, 3])  # mean 2.67
    s = score_domain(evs, "matching")
    assert s.tier == 2 and s.n_window == 0 and s.mastery == 0
    assert [t.tier for t in tier_history(evs, "matching")] == [1, 2]


def test_exam_fails_on_zero_item_or_low_mean():
    for scores in ([3, 3, 3, 3, 3, 0], [2, 2, 2, 3, 2, 2]):  # mean 2.5 with a 0; mean 2.17
        evs = _exam(100, scores)
        s = score_domain(evs, "matching")
        assert s.tier == 1 and s.n_window == 6  # failed exam items still count as evidence


def test_exam_pass_boundary_mean_2_3():
    # 6 items cannot give exactly 2.3; check the rule on a longer set to pin >= 2.3
    from app.competency.engine import exam_passed

    assert exam_passed([3, 2, 2, 2, 3, 2, 2, 2, 3, 2])  # 23/10
    assert not exam_passed([3, 2, 2, 2, 3, 2, 2, 2, 2, 2])  # 22/10


def test_partial_exam_does_not_promote():
    assert score_domain(_exam(100, [3] * 5), "matching").tier == 1


def test_retake_is_a_new_node():
    evs = _exam(100, [1] * 6) + _exam(101, [3] * 6)
    assert [t.tier for t in tier_history(evs, "matching")] == [1, 2]


def test_exam_at_wrong_tier_ignored():
    assert score_domain(_exam(100, [3] * 6, tier=3), "matching").tier == 1


def test_tier_capped_at_5():
    evs = []
    for t in range(1, 6):
        evs += _exam(100 + t, [3] * 6, tier=t)
    assert score_domain(evs, "matching").tier == 5


def test_diagnostic_sets_initial_tier():
    def d(tier, s):
        return ev("diagnostic_item", s / 3, tier=tier, sub=f"matching.s{tier}")

    assert initial_tier([]) == 1
    assert initial_tier([d(1, 3), d(1, 3), d(2, 3), d(2, 2), d(3, 1), d(3, 1)]) == 3  # passed 1,2 -> start at 3
    assert initial_tier([d(1, 1), d(1, 1)]) == 1
    evs = [d(1, 3), d(1, 3), d(2, 3), d(2, 3)]
    s = score_domain(evs + [ev(v=1.0, tier=3)], "matching")
    assert s.tier == 3 and s.n_window == 1 and tier_history(evs, "matching")[0].via == "diagnostic"


def test_late_diagnostic_does_not_retier():
    evs = [ev(v=1.0), ev("diagnostic_item", 1.0, tier=1), ev("diagnostic_item", 1.0, tier=2)]
    assert score_domain(evs, "matching").tier == 1
