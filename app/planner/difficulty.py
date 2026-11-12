"""Difficulty control and adaptive sizing for the Planner (SPEC §7.7). Pure given the track's files; all thresholds are constants here.

Rate: success over the last 8 nodes of kind=challenge that have a verdict (remediation, diagnostic and tier_exam nodes never count;
in-progress, abandoned and non-"too hard" rescoped nodes have no verdict). Fewer than 3 -> the diagnostic tier at default sizes.
Otherwise keep the rate in [0.70, 0.85]: above -> raise (work at the current tier, integrate more), below -> lower (a tier down
where one exists, short foundational builds), inside -> hold.
Sizing: a subskill the learner has not yet practised in at least FAMILIAR_NODES nodes is "unfamiliar": options touching one stay in the
foundational band (15-45 min); only options made purely of familiar subskills may grow into integrative builds (up to 3 h).
"""

from dataclasses import dataclass
from pathlib import Path

from app.builder.context import load_framework
from app.datastore.events import read_events
from app.datastore.layout import DataLayout
from app.lifecycle.outcomes import FAILURE, SUCCESS, Outcome, read_outcomes

WINDOW = 8  # §7.7: rolling rate over the last 8 challenge nodes
MIN_NODES = 3  # §7.7: fewer -> diagnostic tier at default sizes
TARGET_LOW_PCT, TARGET_HIGH_PCT = 70, 85  # §7.7: keep the rate in [0.70, 0.85] (integer percents: no float edge cases)
FOUNDATIONAL_MAX_MIN = 45  # §7.7 sizing: unfamiliar/foundational subskills 15-45 min
FAMILIAR_NODES = 2  # a subskill is familiar once it has evidence from this many different nodes (DECISIONS M8)
DIAGNOSTIC, RAISE, HOLD, LOWER = "diagnostic", "raise", "hold", "lower"

RULES = {
    DIAGNOSTIC: "fewer than 3 challenge nodes have a verdict: use the learner's current tier in the domain (the diagnostic placement) at default sizes",
    RAISE: "success rate is above 0.85: every option at the learner's CURRENT tier in its domain, and at least one option integrating 2+ subskills",
    HOLD: "success rate is inside 0.70-0.85: keep the current difficulty",
    LOWER: "success rate is below 0.70: every option one tier below the current tier where one exists, short foundational builds (at most 45 min)",
}


@dataclass(frozen=True)
class Difficulty:
    mode: str  # diagnostic | raise | hold | lower
    n: int  # challenge nodes with a verdict inside the window
    successes: int
    window: tuple[tuple[int, str], ...]  # (node id, "success" | "failure") oldest first

    @property
    def rate(self) -> float | None:
        return self.successes / self.n if self.n else None

    def to_input(self, challenge_nodes: int | None = None) -> dict:
        """What the Planner sees (and what GET /api/planner/difficulty returns)."""
        return {
            "mode": self.mode, "rule": RULES[self.mode], "challenge_nodes": self.n if challenge_nodes is None else challenge_nodes,
            "rated_nodes": self.n, "successes": self.successes, "success_rate": None if self.rate is None else round(self.rate, 3),
            "target": [TARGET_LOW_PCT / 100, TARGET_HIGH_PCT / 100], "window": [{"id": i, "result": r} for i, r in self.window],
        }  # fmt: skip


def difficulty_from(outcomes: dict[int, Outcome]) -> Difficulty:
    rated = [o for _, o in sorted(outcomes.items()) if o.kind == "challenge" and o.result in (SUCCESS, FAILURE)][-WINDOW:]
    n, s = len(rated), sum(1 for o in rated if o.result == SUCCESS)
    if n < MIN_NODES:
        mode = DIAGNOSTIC
    elif 100 * s > TARGET_HIGH_PCT * n:
        mode = RAISE
    elif 100 * s < TARGET_LOW_PCT * n:
        mode = LOWER
    else:
        mode = HOLD
    return Difficulty(mode, n, s, tuple((o.id, o.result) for o in rated))


def read_difficulty(root: Path, track: str) -> Difficulty:
    return difficulty_from(read_outcomes(root, track))


def unfamiliar_subskills(root: Path, track: str) -> list[str]:
    """Framework subskills with evidence from fewer than FAMILIAR_NODES different nodes (the onboarding diagnostic, node 0, is not a node)."""
    fw = load_framework(root, track)
    if fw is None:
        return []
    events, _ = read_events(DataLayout(Path(root)).events_path, track)
    nodes: dict[str, set[int]] = {}
    for e in events:
        if e.node_id != 0 and e.type != "diagnostic_item":
            nodes.setdefault(e.subskill, set()).add(e.node_id)
    return [s for d in fw.domains for s in d.subskills if len(nodes.get(s, ())) < FAMILIAR_NODES]


def check_options(options: list[dict], diff: Difficulty, scores: dict[str, dict], unfamiliar: set[str]) -> list[str]:
    """Code-enforced difficulty and sizing rules over a batch of already schema-valid options -> errors the model can repair."""
    errors: list[str] = []
    for i, o in enumerate(options, 1):
        cap = (scores.get(o["domain"]) or {}).get("tier", 1)
        if diff.mode in (DIAGNOSTIC, RAISE) and o["tier"] != cap:
            errors.append(f"option {i}: tier must be {cap} (the learner's current tier in {o['domain']}); difficulty mode is {diff.mode}")
        if diff.mode == LOWER and o["tier"] > max(1, cap - 1):
            errors.append(f"option {i}: tier must be at most {max(1, cap - 1)} (one below the current tier {cap} in {o['domain']}); difficulty mode is lower")
        foundational = diff.mode == LOWER or any(s in unfamiliar for s in o["subskills"])
        if foundational and o["size_est_min"] > FOUNDATIONAL_MAX_MIN:
            why = "difficulty mode is lower" if diff.mode == LOWER else f"it includes unfamiliar subskills {sorted(s for s in o['subskills'] if s in unfamiliar)}"
            errors.append(f"option {i}: size_est_min must be at most {FOUNDATIONAL_MAX_MIN} ({why})")
    if diff.mode == RAISE and options and not any(len(set(o["subskills"])) >= 2 for o in options):
        errors.append("at least one option must integrate 2 or more subskills (difficulty mode is raise)")
    return errors
