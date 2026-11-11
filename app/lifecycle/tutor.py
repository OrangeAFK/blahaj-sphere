"""Tutor (SPEC §7.4): Socratic chat on the light tier, the hint ladder, and the heavy "Show me" call.

The backend builds the tutor's context from a fixed list of learner-visible files; `_hidden/` is never read for it (the
`show_me` call is the one heavy exception). As a backstop, every reply is checked against the hidden key values and the reference
solution, and replaced when it repeats one of them (DECISIONS M6a)."""

import json
import re
from datetime import datetime
from pathlib import Path

import yaml

from app.builder.gate import _number_tokens, _same_value, _trivial
from app.lifecycle.nodes import WORKING, LifecycleError, NodeRef, NodeStore, node_lock
from app.llm.types import LLMError

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
FILE_CAP = 6_000  # chars of one workspace file sent to the tutor
TOTAL_CAP = 24_000
SKIP_SUFFIXES = {".raw", ".png", ".jpg", ".gds", ".oas", ".pdf", ".zip", ".npy", ".npz", ".h5", ".vcd", ".fst"}
MAX_FILE_BYTES = 400_000
HISTORY_TURNS = 10
MAX_MESSAGE = 2_000
REF_LINE_MIN = 30  # a reference-solution line this long, verbatim in a reply, counts as a leak

TUTOR_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}, "hint": {"type": ["object", "null"]}}, "required": ["reply"]}
SHOW_ME_SCHEMA = {"type": "object", "properties": {"explanation": {"type": "string"}, "subskills": {"type": "array"}},
                  "required": ["explanation", "subskills"]}  # fmt: skip
REDACTED_REPLY = ("I can't hand you that value or line. Tell me which quantity you would compute it from and what you get with your "
                  "own numbers, and we'll check the reasoning from there.")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def public_checker(rec: dict | None) -> dict | None:
    """What the learner and the tutor may see of a checker run: per-test pass/fail, measured value and spec threshold, plus the
    metrics that are not prelab answer keys (`_check` already dropped the key metrics; DECISIONS M6a.1)."""
    if not rec:
        return None
    return {
        "ts": rec.get("ts"), "kind": rec.get("kind"), "verdict": rec.get("verdict"), "passed": rec.get("passed"),
        "tests": [{"name": t.get("name"), "pass": bool(t.get("pass")), "measured": t.get("measured"), "spec": t.get("spec"),
                   "unit": t.get("unit", "")} for t in rec.get("tests") or []],  # fmt: skip
        "metrics": rec.get("metrics") or {},
        "seconds": rec.get("seconds"), "detail": rec.get("detail"),
    }


def workspace_texts(ref: NodeRef, file_cap: int = FILE_CAP, total_cap: int = TOTAL_CAP) -> dict[str, str]:
    """The learner's workspace as text, size-capped; binary/huge/simulator-output files are skipped."""
    ws = ref.path / "workspace"
    out: dict[str, str] = {}
    total = 0
    for p in sorted(ws.rglob("*")) if ws.is_dir() else []:
        if not p.is_file() or p.suffix.lower() in SKIP_SUFFIXES or p.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > file_cap:
            text = text[:file_cap] + "\n[... truncated]"
        if total + len(text) > total_cap:
            break
        total += len(text)
        out[p.relative_to(ws).as_posix()] = text
    return out


class Tutor:
    def __init__(self, store: NodeStore, router, domain_pack: str):
        self.store = store
        self.router = router
        self.domain_pack = domain_pack

    # --- state ------------------------------------------------------------------------------------------
    def _state(self, ref: NodeRef) -> dict:
        s = self.store.session(ref, "tutor", {})
        return {"messages": s.get("messages", []), "hints_used": s.get("hints_used", {}), "reveals": s.get("reveals", {})}

    def reveals(self, ref: NodeRef) -> set[str]:
        """Subskills the learner has seen a "Show me" for: their events on this node carry assist_reveal (§8.2)."""
        return set(self._state(ref)["reveals"])

    def _ladders(self, ref: NodeRef) -> list[dict]:
        p = ref.path / "hints.yaml"
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
        return [l for l in (doc or {}).get("ladders") or [] if isinstance(l, dict) and isinstance(l.get("hints"), list)]

    # --- context ----------------------------------------------------------------------------------------
    def context(self, ref: NodeRef) -> dict:
        """§7.4 backend-injected context. Reads only learner-visible paths; nothing under _hidden/."""
        fm = self.store.frontmatter(ref)
        ans = ref.path / "prelab_answers.yaml"
        used = self._state(ref)["hints_used"]
        return {
            "brief": {"title": fm.get("title"), "subskills": fm.get("subskills"), "text": self.store.brief(ref)},
            "prelab": (ref.path / "prelab.md").read_text(encoding="utf-8") if (ref.path / "prelab.md").is_file() else "",
            "prelab_answers": (yaml.safe_load(ans.read_text(encoding="utf-8")) or {}).get("answers") if ans.is_file() else None,
            "hints": self._ladders(ref),
            "hints_used": used,
            "workspace": workspace_texts(ref),
            "checker_result": public_checker(self.store.session(ref, "checker", None)),
            "library": [],  # library chunks arrive with M9
        }

    # --- hints ------------------------------------------------------------------------------------------
    def hints_view(self, ref: NodeRef) -> list[dict]:
        used = self._state(ref)["hints_used"]
        return [{"subskill": l.get("subskill"), "total": len(l["hints"]), "revealed": l["hints"][: int(used.get(l.get("subskill"), 0))]}
                for l in self._ladders(ref)]  # fmt: skip

    def reveal_hint(self, ref: NodeRef, subskill: str) -> dict:
        """User-requested hint (§5.5: user-visible on request): the next unused rung of that ladder."""
        self.store.require(ref, *WORKING)
        ladder = next((l for l in self._ladders(ref) if l.get("subskill") == subskill), None)
        if ladder is None:
            raise LifecycleError(404, f"no hint ladder for {subskill!r}")
        with node_lock(ref.track, ref.id):
            st = self._state(ref)
            n = int(st["hints_used"].get(subskill, 0))
            if n >= len(ladder["hints"]):
                raise LifecycleError(409, "every hint for this subskill has been shown")
            st["hints_used"][subskill] = n + 1
            self.store.save_session(ref, "tutor", st)
        return {"subskill": subskill, "index": n, "hint": ladder["hints"][n], "remaining": len(ladder["hints"]) - n - 1}

    # --- leak backstop ----------------------------------------------------------------------------------
    def _hidden_numbers(self, ref: NodeRef) -> list[float]:
        vals = []
        for it in self.store.prelab_key(ref):
            for k in ("value", "hand_estimate"):
                v = it.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and not _trivial(float(v)):
                    vals.append(float(v))
        return vals

    def _reference_lines(self, ref: NodeRef, visible: str) -> list[str]:
        out = []
        vis = _norm(visible)
        base = ref.path / "_hidden" / "reference_solution"
        for p in sorted(base.rglob("*")) if base.is_dir() else []:
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in text.splitlines():
                n = _norm(line)
                if len(n) >= REF_LINE_MIN and not n.startswith(("#", "*", ";", "//")) and n not in vis:
                    out.append(n)
        return out

    def leaks(self, ref: NodeRef, reply: str, visible: str) -> list[str]:
        """Hidden material repeated in `reply`: key numbers (unless the learner can already see that number in their own
        inputs/outputs) and verbatim reference-solution lines. -> short labels, never the leaked content itself."""
        found = []
        seen = _number_tokens(visible)
        toks = _number_tokens(reply)
        for v in self._hidden_numbers(ref):
            if any(_same_value(n, v, tok) for n, tok in toks) and not any(_same_value(n, v, tok) for n, tok in seen):
                found.append("key value")
        nr = _norm(reply)
        if any(line in nr for line in self._reference_lines(ref, visible)):
            found.append("reference line")
        return found

    # --- chat -------------------------------------------------------------------------------------------
    def chat(self, ref: NodeRef, message: str) -> dict:
        self.store.require(ref, *WORKING)
        message = message.strip()
        if not message:
            raise LifecycleError(422, "message is required")
        if len(message) > MAX_MESSAGE:
            raise LifecycleError(422, f"message is longer than {MAX_MESSAGE} characters")
        st = self._state(ref)
        ctx = self.context(ref)
        history = st["messages"][-HISTORY_TURNS:]
        convo = "\n".join(f"{'Learner' if m['role'] == 'user' else 'Tutor'}: {m['content']}" for m in history if m.get("kind", "chat") == "chat")
        system = (PROMPTS / "tutor.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain_pack)
        user = ("INPUTS (JSON):\n" + json.dumps(ctx, ensure_ascii=False) + "\n\nCHAT SO FAR:\n" + (convo or "(none)")
                + "\n\nLearner now says:\n" + message)  # fmt: skip
        for _ in range(3):  # free models sometimes return an empty body (DECISIONS M6a): ask again
            res = self.router.complete("tutor_chat", [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                       schema=TUTOR_SCHEMA, node_id=ref.id)  # fmt: skip
            j = res.json if isinstance(res.json, dict) else None
            if j and isinstance(j.get("reply"), str) and j["reply"].strip():
                break
        else:
            raise LifecycleError(502, "the tutor returned an unusable reply; try again")
        reply = j["reply"].strip()
        found = self.leaks(ref, reply, json.dumps(ctx, ensure_ascii=False) + "\n" + message)
        redacted = bool(found)
        if redacted:
            reply = REDACTED_REPLY
        hint = self._accept_hint(ref, j.get("hint")) if not redacted else None
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with node_lock(ref.track, ref.id):
            st = self._state(ref)  # re-read: a hint reveal may have happened meanwhile
            if hint:
                st["hints_used"][hint["subskill"]] = max(int(st["hints_used"].get(hint["subskill"], 0)), hint["index"] + 1)
            st["messages"] += [{"role": "user", "content": message, "ts": now, "kind": "chat"},
                               {"role": "assistant", "content": reply, "ts": now, "kind": "chat", "hint": hint, "redacted": redacted,
                                "model": res.model}]  # fmt: skip
            self.store.save_session(ref, "tutor", st)
        return {"reply": reply, "hint": hint, "redacted": redacted, "model": res.model}

    def _accept_hint(self, ref: NodeRef, hint) -> dict | None:
        """Ladder order is tracked by code (§7.4): a hint counts only if it is the next unused rung of a real ladder."""
        if not isinstance(hint, dict):
            return None
        sub, idx = hint.get("subskill"), hint.get("index")
        ladder = next((l for l in self._ladders(ref) if l.get("subskill") == sub), None)
        if ladder is None or isinstance(idx, bool) or not isinstance(idx, int):
            return None
        used = int(self._state(ref)["hints_used"].get(sub, 0))
        return {"subskill": sub, "index": idx} if idx == used and idx < len(ladder["hints"]) else None

    def history(self, ref: NodeRef) -> dict:
        st = self._state(ref)
        return {"messages": [{k: m.get(k) for k in ("role", "content", "ts", "kind", "hint", "redacted")} for m in st["messages"]],
                "hints": self.hints_view(ref), "show_me_used": sorted(st["reveals"])}  # fmt: skip

    # --- show me (heavy, _hidden/ access) ---------------------------------------------------------------
    def show_me(self, ref: NodeRef, question: str) -> dict:
        self.store.require(ref, *WORKING)
        question = question.strip()
        if not question:
            raise LifecycleError(422, "say what you want to be shown")
        fm = self.store.frontmatter(ref)
        node_subs = [str(s) for s in fm.get("subskills") or []]
        base = ref.path / "_hidden" / "reference_solution"
        ref_files = {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8", errors="replace")[:FILE_CAP]
                     for p in sorted(base.rglob("*")) if p.is_file() and p.suffix.lower() not in SKIP_SUFFIXES}  # fmt: skip
        key = [{k: it.get(k) for k in ("id", "kind", "subskill", "unit", "metric", "value", "hand_estimate")} for it in self.store.prelab_key(ref)]
        inp = {
            "brief": self.store.brief(ref), "question": question, "workspace": workspace_texts(ref),
            "checker_result": public_checker(self.store.session(ref, "checker", None)),
            "reference_solution": ref_files, "prelab_key": key, "hints": self._ladders(ref), "node_subskills": node_subs,
        }  # fmt: skip
        system = (PROMPTS / "show_me.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain_pack)
        res = self.router.complete("show_me", [{"role": "system", "content": system},
                                               {"role": "user", "content": json.dumps(inp, ensure_ascii=False)}],
                                   schema=SHOW_ME_SCHEMA, node_id=ref.id)  # fmt: skip
        j = res.json if isinstance(res.json, dict) else None
        if not j or not isinstance(j.get("explanation"), str) or not j["explanation"].strip():
            raise LifecycleError(502, "show-me returned an unusable explanation; try again")
        subs = [s for s in (j.get("subskills") or []) if isinstance(s, str) and s in node_subs]
        if not subs:  # the model named none (or unknown ones): be conservative, every subskill of the node counts as assisted
            subs = list(node_subs)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with node_lock(ref.track, ref.id):
            st = self._state(ref)
            for s in subs:
                st["reveals"].setdefault(s, now)
            st["messages"] += [{"role": "user", "content": f"[Show me] {question}", "ts": now, "kind": "show_me"},
                               {"role": "assistant", "content": j["explanation"].strip(), "ts": now, "kind": "show_me", "model": res.model}]  # fmt: skip
            self.store.save_session(ref, "tutor", st)
        return {"explanation": j["explanation"].strip(), "subskills": subs, "assist_reveal": True, "model": res.model}


__all__ = ["Tutor", "public_checker", "workspace_texts", "LLMError"]
