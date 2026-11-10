"""Competency engine (SPEC §8.2-8.4). Pure code: no I/O, no clock, no randomness.

Event order is log order (file order of events.jsonl), never `ts`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, fields

EVENT_TYPES = ("proof_pass", "oral_item", "prelab_numeric", "prelab_explain", "tier_exam_item", "diagnostic_item")
SCORED_TYPES = ("oral_item", "tier_exam_item", "prelab_explain", "diagnostic_item")  # 0-3 score / 3

MIN_TIER, MAX_TIER = 1, 5
WINDOW = 20
K = 3.0
UNLOCK_MASTERY = 65.0
UNLOCK_SUBSKILLS = 3
EXAM_ITEMS = 6
EXAM_PASS_MEAN = 2.3  # on the 0-3 item scale; no item may be 0

INTERLEAVED_WEIGHT_MULT = 1.5
ASSIST_REVEAL_VALUE_MULT = 0.7
NUMERIC_WEIGHT = 0.5
_EPS = 1e-9  # float slack for threshold comparisons (13 x 0.8 must reach exactly 65)


@dataclass(frozen=True)
class Event:
    """SPEC §5.8. `value`/`weight` are the BASE numbers from §8.2; modifiers are applied by `effective`."""

    id: str
    ts: str
    node_id: int
    domain: str
    subskill: str
    tier: int
    type: str
    value: float
    weight: float
    interleaved: bool = False
    assist_reveal: bool = False
    grader_model: str | None = None
    framework_version: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# --- §8.2 ---------------------------------------------------------------------------------------

def numeric_value(err: float, tol: float) -> float:
    """prelab_numeric: 1 if |err| <= tol else max(0, 1 - |err| / (3 * tol)). Same units for err and tol."""
    err = abs(err)
    if err <= tol:
        return 1.0
    if tol == 0:
        return 0.0
    return max(0.0, 1.0 - err / (3.0 * tol))

def base_value_weight(type: str, *, score: float | None = None, err: float | None = None, tol: float | None = None) -> tuple[float, float]:
    """The §8.2 table, before modifiers. `score` is the 0-3 grade; prelab_numeric takes err + tol instead."""
    if type == "proof_pass":
        return 1.0, 1.0
    if type in SCORED_TYPES:
        if score is None or not 0 <= score <= 3:
            raise ValueError(f"{type} needs score in 0..3, got {score!r}")
        return score / 3.0, 1.0
    if type == "prelab_numeric":
        if err is None or tol is None or tol < 0:
            raise ValueError("prelab_numeric needs err and tol >= 0")
        return numeric_value(err, tol), NUMERIC_WEIGHT
    raise ValueError(f"unknown event type {type!r}")


def effective(ev: Event) -> tuple[float, float]:
    """(v, w) after modifiers: interleaved -> w x 1.5; assist_reveal -> v x 0.7."""
    v, w = ev.value, ev.weight
    if ev.interleaved:
        w *= INTERLEAVED_WEIGHT_MULT
    if ev.assist_reveal:
        v *= ASSIST_REVEAL_VALUE_MULT
    return v, w


def counts_toward_mastery(ev: Event) -> bool:
    return ev.type != "diagnostic_item"  # diagnostics only set the initial tier


# --- §8.4 tiers ---------------------------------------------------------------------------------

def exam_passed(scores: Sequence[float]) -> bool:
    """Tier exam: mean >= 2.3 and no item scored 0 (scores on the 0-3 scale)."""
    return bool(scores) and min(scores) > 0 and sum(scores) / len(scores) >= EXAM_PASS_MEAN - _EPS


def initial_tier(diagnostic: Sequence[Event]) -> int:
    """Tier to start a domain at, from its diagnostic items (SPEC gives no rule; DECISIONS M3).

    A tier t is 'passed' if its diagnostic items meet the exam bar (mean >= 2.3, none 0). Start at one above the
    highest tier t whose own and all lower tiers with items are passed; no diagnostic -> tier 1.
    """
    by_tier: dict[int, list[float]] = {}
    for e in diagnostic:
        by_tier.setdefault(e.tier, []).append(e.value * 3.0)
    t = MIN_TIER - 1
    for tier in sorted(by_tier):
        if not exam_passed(by_tier[tier]):
            break
        t = tier
    return min(MAX_TIER, max(MIN_TIER, t + 1))


@dataclass(frozen=True)
class TierStep:
    tier: int
    since: int  # index into the event list: events at index >= since count toward this tier
    via: str  # "initial" | "diagnostic" | "exam:<node_id>"


def tier_history(events: Sequence[Event], domain: str) -> list[TierStep]:
    """Tiers a domain has been at, oldest first, derived only from the event log (so rebuildable)."""
    dom = [(i, e) for i, e in enumerate(events) if e.domain == domain]
    first_real = next((i for i, e in dom if e.type != "diagnostic_item"), len(events))
    diag = [e for i, e in dom if e.type == "diagnostic_item" and i < first_real]  # later diagnostics don't re-tier
    tier = initial_tier(diag)
    since = max((i for i, e in dom if e.type == "diagnostic_item" and i < first_real), default=-1) + 1
    steps = [TierStep(tier, since, "diagnostic" if diag else "initial")]

    exam_items: dict[int, list[float]] = {}
    done: set[int] = set()
    for i, e in dom:
        if i < since or e.type != "tier_exam_item" or e.tier != tier or e.node_id in done:
            continue
        items = exam_items.setdefault(e.node_id, [])
        items.append(e.value * 3.0)
        if len(items) < EXAM_ITEMS:
            continue
        done.add(e.node_id)
        if exam_passed(items) and tier < MAX_TIER:
            tier += 1
            steps.append(TierStep(tier, i + 1, f"exam:{e.node_id}"))
            exam_items = {}
    return steps


# --- §8.3 mastery + §8.4 unlock -----------------------------------------------------------------

def mastery(events: Sequence[Event]) -> float:
    """100 * sum(w*v) / (sum(w) + K) over the given events (the caller supplies the window)."""
    sw = swv = 0.0
    for e in events:
        v, w = effective(e)
        swv += w * v
        sw += w
    return 100.0 * swv / (sw + K)


def exam_unlocked(mastery_value: float, subskills_evidenced: int, tier: int) -> bool:
    """mastery >= 65 AND >= 3 distinct subskills at tier T. No time conditions. Tier 5 has no exam above it."""
    return tier < MAX_TIER and mastery_value >= UNLOCK_MASTERY - _EPS and subskills_evidenced >= UNLOCK_SUBSKILLS


@dataclass(frozen=True)
class DomainScore:
    domain: str
    tier: int
    since: int
    mastery: float
    n_window: int
    n_since_tier: int
    subskills_evidenced: tuple[str, ...] = field(default_factory=tuple)
    unlocked: bool = False


def score_domain(events: Sequence[Event], domain: str) -> DomainScore:
    """Current tier, mastery (most recent 20 qualifying events since reaching T), and exam unlock for one domain."""
    step = tier_history(events, domain)[-1]
    T = step.tier
    pool = [e for i, e in enumerate(events) if i >= step.since and e.domain == domain and e.tier <= T and counts_toward_mastery(e)]
    window = pool[-WINDOW:]
    m = mastery(window)
    evidenced = tuple(sorted({e.subskill for e in pool if e.tier == T}))
    return DomainScore(domain, T, step.since, m, len(window), len(pool), evidenced, exam_unlocked(m, len(evidenced), T))


def score_all(events: Sequence[Event]) -> dict[str, DomainScore]:
    return {d: score_domain(events, d) for d in sorted({e.domain for e in events})}
