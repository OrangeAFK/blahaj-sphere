"""Planner, minimal (SPEC §7.7, §5.6): 2-4 next-node options from the learner state. Full difficulty control,
remediation and misconception resolution are M8; here code validates the options against the frozen framework."""

import html
import json
import os
from datetime import datetime
from pathlib import Path

from app.builder.context import learner_state, load_framework
from app.datastore.layout import DataLayout

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
MIN_OPTIONS, MAX_OPTIONS = 2, 4
SIZE_MIN, SIZE_MAX = 15, 180  # §7.7 sizing: 15-45 min foundational, 1-3 h integrative
KINDS = ("challenge",)  # tier exams M10; remediation children come from Planner.remediation_option (M7)
REMEDIATION_KINDS = ("remediation",)
MIN_CHALLENGES_FOR_RATE = 3  # §7.7: fewer than 3 challenge nodes -> diagnostic tier at default sizes

OPTIONS_SCHEMA = {
    "type": "object",
    "properties": {"options": {"type": "array", "minItems": MIN_OPTIONS, "maxItems": MAX_OPTIONS}},
    "required": ["options"],
}
REMEDIATION_SCHEMA = {"type": "object", "properties": {"options": {"type": "array", "minItems": 1, "maxItems": 1}}, "required": ["options"]}
OPTION_KEYS = ("title", "why_now", "kind", "domain", "subskills", "tier", "size_est_min")


class PlannerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def validate_option(o, where: str, framework, scores: dict[str, dict], kinds: tuple[str, ...] = KINDS) -> tuple[dict | None, list[str]]:
    """One option against the frozen framework and the learner's tiers -> (clean option, errors). Also used for Rescope."""
    errors: list[str] = []
    if not isinstance(o, dict):
        return None, [f"{where}: must be an object"]
    missing = [k for k in OPTION_KEYS if k not in o]
    if missing:
        return None, [f"{where}: missing keys {missing}"]
    domains = {d.id: d for d in framework.domains}
    for k in ("title", "why_now"):
        if not isinstance(o[k], str) or not o[k].strip():
            errors.append(f"{where}: `{k}` must be a non-empty string")
    if o["kind"] not in kinds:
        errors.append(f"{where}: kind must be one of {list(kinds)}")
    dom = domains.get(o["domain"])
    if dom is None:
        errors.append(f"{where}: domain {o['domain']!r} is not in the framework {sorted(domains)}")
    subs = o["subskills"]
    all_subs = {s for d in framework.domains for s in d.subskills}
    if not isinstance(subs, list) or not subs or not all(isinstance(s, str) for s in subs):
        errors.append(f"{where}: subskills must be a non-empty list of framework subskill ids")
    else:
        unknown = [s for s in subs if s not in all_subs]
        if unknown:
            errors.append(f"{where}: unknown subskills {unknown}; use ids from the framework")
        elif dom is not None and not any(s in dom.subskills for s in subs):
            errors.append(f"{where}: at least one subskill must belong to domain {dom.id} {dom.subskills}")
    cap = (scores.get(o["domain"]) or {}).get("tier", 1)
    if isinstance(o["tier"], bool) or not isinstance(o["tier"], int) or not 1 <= o["tier"] <= cap:
        errors.append(f"{where}: tier must be an integer 1..{cap} (the learner's current tier in {o['domain']})")
    if isinstance(o["size_est_min"], bool) or not isinstance(o["size_est_min"], int) or not SIZE_MIN <= o["size_est_min"] <= SIZE_MAX:
        errors.append(f"{where}: size_est_min must be an integer {SIZE_MIN}-{SIZE_MAX}")
    if errors:
        return None, errors
    # models sometimes HTML-escape text ("K &gt; 1"); options are plain text (DECISIONS M5)
    return {k: (html.unescape(o[k]).strip() if isinstance(o[k], str) else o[k]) for k in OPTION_KEYS}, []


def validate_options(raw, framework, scores: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """-> (clean options, errors). Every error is phrased so the model can repair it."""
    errors: list[str] = []
    opts = raw.get("options") if isinstance(raw, dict) else None
    if not isinstance(opts, list) or not MIN_OPTIONS <= len(opts) <= MAX_OPTIONS:
        return [], [f"`options` must be a list of {MIN_OPTIONS}-{MAX_OPTIONS} objects"]
    clean = []
    for i, o in enumerate(opts, 1):
        c, errs = validate_option(o, f"option {i}", framework, scores)
        errors += errs
        if c is not None:
            clean.append(c)
    return (clean if not errors else []), errors


class Planner:
    def __init__(self, data_root: Path, track: str, router, domain_pack: str):
        self.root = Path(data_root)
        self.track = track
        self.layout = DataLayout(self.root, track)
        self.router = router
        self.domain_pack = domain_pack

    @property
    def options_path(self) -> Path:
        return self.layout.planner_dir / "options.json"

    def last_options(self) -> dict | None:
        p = self.options_path
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def planner_input(self) -> dict:
        fw = load_framework(self.root, self.track)
        if fw is None:
            raise PlannerError(409, "approve the competency framework first (onboarding)")
        state = learner_state(self.root, self.track)
        if not state["scores"]:
            raise PlannerError(409, "finish the onboarding diagnostic first (no tiers yet)")
        n_challenges = sum(1 for n in state["recent_nodes"] if n["kind"] == "challenge")
        return {
            "goals": state["goals"],
            "summary": state["summary"],
            "framework": [{"id": d.id, "name": d.name, "subskills": d.subskills, "tiers": d.tiers} for d in fw.domains],
            "scores": state["scores"],
            "open_misconceptions": state["open_misconceptions"],
            "recent_nodes": state["recent_nodes"],
            "difficulty": {
                "challenge_nodes": n_challenges,
                "rule": "fewer than 3 challenge nodes: use the learner's current tiers and default sizes"
                if n_challenges < MIN_CHALLENGES_FOR_RATE else "success-rate control arrives in M8; keep current tiers",
            },
        }

    def generate(self) -> dict:
        """Call the Planner, validate, keep one repair attempt; store and return {options, model, ts}."""
        fw = load_framework(self.root, self.track)
        inp = self.planner_input()
        system = {"role": "system", "content": (PROMPTS / "planner.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain_pack)}
        user = {"role": "user", "content": json.dumps(inp, ensure_ascii=False)}
        msgs = [system, user]
        errors: list[str] = []
        for _ in range(2):
            res = self.router.complete("plan_branches", msgs, schema=OPTIONS_SCHEMA)
            options, errors = validate_options(res.json, fw, inp["scores"])
            if not errors:
                out = {"options": options, "model": res.model, "ts": datetime.now().astimezone().isoformat(timespec="seconds")}
                self._save(out)
                return out
            msgs = [system, user, {"role": "assistant", "content": res.text},
                    {"role": "user", "content": "Those options are invalid:\n- " + "\n- ".join(errors) + "\nReturn the corrected JSON only."}]
        raise PlannerError(502, "planner options invalid after retry: " + "; ".join(errors))

    def remediation_option(self, remediation: dict) -> dict:
        """One kind=remediation option for a failed oral (SPEC §7.6): `remediation` = {parent, failed_items, misconceptions}.
        Same validation as options, one repair attempt; not stored (the caller keeps it with the failing node)."""
        fw = load_framework(self.root, self.track)
        inp = {**self.planner_input(), "remediation": remediation}
        system = {"role": "system", "content": (PROMPTS / "planner.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain_pack)}
        user = {"role": "user", "content": json.dumps(inp, ensure_ascii=False)}
        msgs = [system, user]
        errors: list[str] = []
        for _ in range(2):
            res = self.router.complete("plan_branches", msgs, schema=REMEDIATION_SCHEMA)
            opts = res.json.get("options") if isinstance(res.json, dict) else None
            if not isinstance(opts, list) or len(opts) != 1:
                errors = ["`options` must be a list with exactly one remediation option"]
            else:
                opt, errors = validate_option(opts[0], "option 1", fw, inp["scores"], REMEDIATION_KINDS)
                if opt is not None:
                    return opt
            msgs = [system, user, {"role": "assistant", "content": res.text},
                    {"role": "user", "content": "That option is invalid:\n- " + "\n- ".join(errors) + "\nReturn the corrected JSON only."}]
        raise PlannerError(502, "remediation option invalid after retry: " + "; ".join(errors))

    def _save(self, out: dict) -> None:
        p = self.options_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
        os.replace(tmp, p)
