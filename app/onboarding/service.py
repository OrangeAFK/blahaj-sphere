"""Onboarding (SPEC §9): interview -> framework draft/approval -> diagnostic. State lives in files under data/.

All LLM access goes through the Router, so tests inject the mock adapter. Prompts are `app/prompts/*.md`.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.competency.engine import MIN_TIER, base_value_weight, diagnostic_tier_passed, initial_tier
from app.datastore.cache import rebuild_db
from app.datastore.events import append_event, new_event, read_events
from app.datastore.layout import DataLayout

from .framework import SUBSKILL, Framework, FrameworkStore, validation_message

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
MAX_QUESTIONS = 10
MAX_PROBES_PER_DOMAIN = 3  # climb T1..T3 at most: ~2 probes on average, and the diagnostic never places above T4
DIAG_NODE_ID = 0
DIAG_NODE_SLUG = "diagnostic"
GOAL_KEYS = ("target_capability", "motivation", "hours_per_week", "background")

INTERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "done": {"type": "boolean"},
        "goals": {"type": ["object", "null"], "properties": {k: {"type": "string"} for k in GOAL_KEYS}},
    },
    "required": ["message", "done", "goals"],
}
FRAMEWORK_SCHEMA = {
    "type": "object",
    "properties": {"domain_pack": {"type": "string"}, "domains": {"type": "array", "minItems": 5, "maxItems": 8}},
    "required": ["domain_pack", "domains"],
}
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"subskill": {"type": "string"}, "question": {"type": "string"}, "anchors": {"type": "object"}},
    "required": ["subskill", "question", "anchors"],
}
GRADE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array"}, "misconceptions": {"type": "array"}},
    "required": ["items", "misconceptions"],
}


class OnboardingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def load_prompt(name: str, domain_pack: str) -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    return text.replace("{domain_pack}", domain_pack).replace("{max_questions}", str(MAX_QUESTIONS))


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def render_goals(goals: dict) -> str:
    labels = {
        "target_capability": "Target capability", "motivation": "Motivation",
        "hours_per_week": "Hours per week", "background": "Background",
    }
    lines = ["# Goals", ""]
    for k in GOAL_KEYS:
        lines += [f"## {labels[k]}", str(goals.get(k, "")).strip() or "(not given)", ""]
    return "\n".join(lines)


class Onboarding:
    def __init__(self, data_root: Path, router, domain_pack: str, template_path: Path | None):
        self.layout = DataLayout(data_root)
        self.router = router
        self.domain_pack = domain_pack
        self.template_path = template_path
        self.frameworks = FrameworkStore(self.layout.root / "framework")

    # --- paths -----------------------------------------------------------------------------------
    @property
    def profile(self) -> Path:
        return self.layout.root / "profile"

    @property
    def goals_path(self) -> Path:
        return self.profile / "goals.md"

    @property
    def interview_path(self) -> Path:
        return self.profile / "interview.json"

    @property
    def diagnostic_path(self) -> Path:
        return self.profile / "diagnostic.json"

    # --- overall state ---------------------------------------------------------------------------
    def state(self) -> dict:
        goals = self.goals_path.exists()
        frozen = self.frameworks.is_frozen()
        diag = _read_json(self.diagnostic_path, {})
        done = bool(diag.get("done"))
        stage = "interview" if not goals else "framework" if not frozen else "done" if done else "diagnostic"
        return {"stage": stage, "goals": goals, "framework_frozen": frozen, "diagnostic_done": done}

    # --- 1. interview ----------------------------------------------------------------------------
    def _interview_state(self) -> dict:
        return _read_json(self.interview_path, {"messages": [], "done": False})

    def interview_view(self) -> dict:
        s = self._interview_state()
        asked = [m["content"] for m in s["messages"] if m["role"] == "assistant"]
        return {
            "done": s["done"], "questions_asked": len(asked), "max_questions": MAX_QUESTIONS,
            "last_message": asked[-1] if asked else None,
            "goals": self.goals_path.read_text(encoding="utf-8") if self.goals_path.exists() else None,
        }

    def interview_turn(self, answer: str | None) -> dict:
        s = self._interview_state()
        if s["done"]:
            return self.interview_view()
        msgs = s["messages"]
        has_pending_q = bool(msgs) and msgs[-1]["role"] == "assistant"
        if answer is not None and answer.strip():
            if not has_pending_q:
                raise OnboardingError(409, "no open interview question to answer")
            msgs.append({"role": "user", "content": answer.strip()})
        elif has_pending_q:
            raise OnboardingError(422, "answer is required")
        n_asked = sum(1 for m in msgs if m["role"] == "assistant")
        force = n_asked >= MAX_QUESTIONS
        out = self._interview_call(msgs, force)
        if out["done"] or force:
            if not out["done"] or not out["goals"]:
                out = self._interview_call(msgs, True)
            if not (out["done"] and out["goals"]):
                raise OnboardingError(502, "interviewer did not produce goals; try again")
        msgs.append({"role": "assistant", "content": out["message"]})
        if out["done"]:
            s["done"] = True
            self.profile.mkdir(parents=True, exist_ok=True)
            self.goals_path.write_text(render_goals(out["goals"]), encoding="utf-8", newline="\n")
        _write_json(self.interview_path, s)
        return self.interview_view()

    def _interview_call(self, msgs: list[dict], force_finish: bool) -> dict:
        system = {"role": "system", "content": load_prompt("interviewer", self.domain_pack)}
        convo = [{"role": "user", "content": "Begin the interview."}] + msgs
        if force_finish:
            convo = convo + [{"role": "user", "content": "That is enough questions. Finish now: done=true and fill every goals field from what you learned (write 'unknown' where not covered)."}]
        res = self._complete("interview", [system, *convo], INTERVIEW_SCHEMA)
        j = res.json
        if not isinstance(j, dict) or not isinstance(j.get("message"), str) or not isinstance(j.get("done"), bool):
            raise OnboardingError(502, "interviewer returned an unusable reply; try again")
        goals = j.get("goals")
        if j["done"]:
            if not isinstance(goals, dict) or not all(isinstance(goals.get(k), str) for k in GOAL_KEYS):
                goals = None
        else:
            goals = None
        return {"message": j["message"], "done": j["done"], "goals": goals}

    def get_goals(self) -> str | None:
        return self.goals_path.read_text(encoding="utf-8") if self.goals_path.exists() else None

    def put_goals(self, text: str) -> None:
        """Goals are editable anytime (SPEC §9.4)."""
        self.profile.mkdir(parents=True, exist_ok=True)
        self.goals_path.write_text(text, encoding="utf-8", newline="\n")

    # --- 2. framework ----------------------------------------------------------------------------
    def framework_view(self) -> dict:
        fw = self.frameworks.load()
        return {
            "frozen": self.frameworks.is_frozen(),
            "framework": fw.model_dump() if fw else None,
        }

    def draft_framework(self) -> dict:
        if self.frameworks.is_frozen():
            raise OnboardingError(409, "framework is frozen")
        goals = self.get_goals()
        if goals is None:
            raise OnboardingError(409, "finish the interview first")
        template = yaml.safe_load(self.template_path.read_text(encoding="utf-8")) if self.template_path else {}
        system = {"role": "system", "content": load_prompt("framework_drafter", self.domain_pack)}
        user = {"role": "user", "content": json.dumps({"goals": goals, "template": template}, ensure_ascii=False)}
        msgs = [system, user]
        err = ""
        for _ in range(2):  # one repair attempt, with the validation errors fed back
            res = self._complete("draft_framework", msgs, FRAMEWORK_SCHEMA)
            try:
                if not isinstance(res.json, dict):
                    raise ValueError("reply was not a JSON object")
                fw = Framework.model_validate(res.json)
            except (ValidationError, ValueError) as e:
                err = validation_message(e) if isinstance(e, ValidationError) else str(e)
                msgs = [system, user, {"role": "assistant", "content": res.text},
                        {"role": "user", "content": f"That framework is invalid: {err}\nReturn the corrected JSON only."}]
                continue
            self.frameworks.save_draft(fw)
            return self.framework_view()
        raise OnboardingError(502, f"framework draft invalid after retry: {err}")

    def save_draft(self, raw: dict) -> dict:
        if self.frameworks.is_frozen():
            raise OnboardingError(409, "framework is frozen")
        self.frameworks.save_draft(self._validate(raw))
        return self.framework_view()

    def approve_framework(self, raw: dict | None) -> dict:
        """Validate (the user's edited version, else the saved draft) and freeze it as competency-v1.yaml."""
        if self.frameworks.is_frozen():
            raise OnboardingError(409, "framework is already frozen")
        if raw is None:
            fw = self.frameworks.load()
            if fw is None:
                raise OnboardingError(409, "no framework draft to approve")
        else:
            fw = self._validate(raw)
        self.frameworks.freeze(fw)
        return self.framework_view()

    @staticmethod
    def _validate(raw: dict) -> Framework:
        try:
            return Framework.model_validate(raw)
        except ValidationError as e:
            raise OnboardingError(422, validation_message(e)) from None

    # --- 3. diagnostic ---------------------------------------------------------------------------
    def _diag(self) -> dict:
        s = _read_json(self.diagnostic_path, None)
        if s is None:
            fw = self.frameworks.load()
            if fw is None or not self.frameworks.is_frozen():
                raise OnboardingError(409, "approve the framework first")
            s = {
                "done": False, "pending": None, "tiers": None,
                "domains": [{"id": d.id, "name": d.name, "probes": [], "done": False} for d in fw.domains],
            }
        return s

    def diagnostic_view(self) -> dict:
        s = self._diag()
        p = s["pending"]
        return {
            "done": s["done"],
            "pending": None if p is None else {k: p[k] for k in ("domain", "tier", "subskill", "question")},
            "tiers": s["tiers"],
            "domains": [
                {"id": d["id"], "name": d["name"], "done": d["done"],
                 "probes": [{"tier": q["tier"], "subskill": q["subskill"], "score": q["score"]} for q in d["probes"]]}
                for d in s["domains"]
            ],
        }

    def diagnostic_next(self) -> dict:
        """The open probe question; generates it (light dialogue) if none is open."""
        s = self._diag()
        if s["done"] or s["pending"] is not None:
            return self.diagnostic_view()
        fw = self.frameworks.load()
        assert fw is not None
        dom = next((d for d in s["domains"] if not d["done"]), None)
        if dom is None:  # all domains finished but finalize never ran (crash): finish now
            self._finalize(s, fw)
            return self.diagnostic_view()
        fdom = next(d for d in fw.domains if d.id == dom["id"])
        tier = len(dom["probes"]) + 1  # climb from T1; a domain stops at its first miss
        user = {
            "domain": {"id": fdom.id, "name": fdom.name, "scope": fdom.scope, "subskills": fdom.subskills},
            "tier": tier, "tier_descriptor": fdom.tiers[tier], "goals": self.get_goals() or "",
            "asked": [q["question"] for q in dom["probes"]],
        }
        msgs = [
            {"role": "system", "content": load_prompt("diagnostician", self.domain_pack)},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ]
        res = self._complete("diagnostic_dialogue", msgs, PROBE_SCHEMA)
        j = res.json
        anchors = j.get("anchors") if isinstance(j, dict) else None
        ok = (
            isinstance(j, dict) and isinstance(j.get("question"), str) and j["question"].strip()
            and isinstance(j.get("subskill"), str) and isinstance(anchors, dict)
            and all(isinstance(anchors.get(str(k)), str) and anchors[str(k)].strip() for k in range(4))
        )
        if not ok:
            raise OnboardingError(502, "diagnostician returned an unusable probe; try again")
        sub = j["subskill"].strip()
        if not SUBSKILL.match(sub):
            sub = f"{fdom.id}.general"
        s["pending"] = {
            "domain": fdom.id, "tier": tier, "subskill": sub, "question": j["question"].strip(),
            "anchors": {str(k): anchors[str(k)] for k in range(4)}, "asked_by": res.model,
        }
        _write_json(self.diagnostic_path, s)
        return self.diagnostic_view()

    def diagnostic_answer(self, answer: str) -> dict:
        s = self._diag()
        p = s["pending"]
        if p is None:
            raise OnboardingError(409, "no open diagnostic question")
        if not answer.strip():
            raise OnboardingError(422, "answer is required (write 'I don't know' to skip)")
        grade_user = {"question": p["question"], "anchors": p["anchors"], "answer": answer.strip()}
        msgs = [
            {"role": "system", "content": load_prompt("diagnostic_grader", self.domain_pack)},
            {"role": "user", "content": json.dumps(grade_user, ensure_ascii=False)},
        ]
        res = self._complete("grade_diagnostic", msgs, GRADE_SCHEMA)  # raises GradingUnavailable; pending stays open
        item = (res.json or {}).get("items", [None])[0] if isinstance(res.json, dict) else None
        score = item.get("score") if isinstance(item, dict) else None
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 3:
            raise OnboardingError(502, "grader returned an unusable score; try again")
        quote = item.get("quote") if isinstance(item.get("quote"), str) else ""
        quote_ok = quote == "" or quote in answer
        v, w = base_value_weight("diagnostic_item", score=score)
        ev = new_event(
            node_id=DIAG_NODE_ID, domain=p["domain"], subskill=p["subskill"], tier=p["tier"], type="diagnostic_item",
            value=v, weight=w, grader_model=res.model, framework_version=1,
        )
        append_event(self.layout.events_path, ev)
        dom = next(d for d in s["domains"] if d["id"] == p["domain"])
        dom["probes"].append({
            "tier": p["tier"], "subskill": p["subskill"], "question": p["question"], "anchors": p["anchors"],
            "answer": answer.strip(), "score": score, "quote": quote if quote_ok else "",
            "rationale": str(item.get("rationale", "")), "grader_model": res.model,
        })
        if not diagnostic_tier_passed([float(score)]) or len(dom["probes"]) >= MAX_PROBES_PER_DOMAIN:
            dom["done"] = True
        s["pending"] = None
        if all(d["done"] for d in s["domains"]):
            fw = self.frameworks.load()
            assert fw is not None
            self._finalize(s, fw)
        else:
            _write_json(self.diagnostic_path, s)
        return {"score": score, "view": self.diagnostic_view()}

    def _finalize(self, s: dict, fw: Framework) -> None:
        events, _ = read_events(self.layout.events_path)
        diag = [e for e in events if e.type == "diagnostic_item"]
        s["tiers"] = {d.id: initial_tier([e for e in diag if e.domain == d.id]) for d in fw.domains}
        s["done"] = True
        _write_json(self.diagnostic_path, s)
        self._write_node(s)
        rebuild_db(self.layout.root)

    def _write_node(self, s: dict) -> None:
        """The diagnostic is stored as a `kind: diagnostic` node (id 0) with a transcript (SPEC §9.3)."""
        d = self.layout.node_dir(DIAG_NODE_ID, DIAG_NODE_SLUG)
        d.mkdir(parents=True, exist_ok=True)
        fm = {
            "id": DIAG_NODE_ID, "slug": DIAG_NODE_SLUG, "title": "Onboarding diagnostic", "parents": [],
            "kind": "diagnostic", "domain": "onboarding", "subskills": [], "tier": MIN_TIER, "size_est_min": 30,
            "image": "none", "pdk": "none", "status": "done", "provisional": False,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        lines = ["# Onboarding diagnostic", "", "Initial tiers: " + ", ".join(f"{k} T{v}" for k, v in s["tiers"].items()), ""]
        for dom in s["domains"]:
            lines += [f"## {dom['name']}", ""]
            for q in dom["probes"]:
                lines += [
                    f"### T{q['tier']} ({q['subskill']}): score {q['score']}/3", "", f"**Q:** {q['question']}", "",
                    f"**A:** {q['answer']}", "", f"Quote: \"{q['quote']}\"" if q["quote"] else "Quote: (none)",
                    f"Rationale: {q['rationale']}", "",
                ]
        text = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + "\n".join(lines)
        (d / "node.md").write_text(text, encoding="utf-8", newline="\n")

    # --- shared ----------------------------------------------------------------------------------
    def _complete(self, task: str, messages: list[dict], schema: dict):
        node_id = DIAG_NODE_ID if task in ("diagnostic_dialogue", "grade_diagnostic") else None
        return self.router.complete(task, messages, schema=schema, node_id=node_id)
