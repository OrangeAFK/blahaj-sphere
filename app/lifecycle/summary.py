"""SUMMARY.md regeneration (SPEC §3.2): after each Debrief the finished node gets a short archive entry (task `summarize`, light tier) and
`archive/SUMMARY.md` is reassembled from all entries, capped at 1,500 tokens (estimated as chars / 3.5, like the prompt packs).

Agents never see the raw archive, only this file, so it must stay small: past the cap the oldest entries shrink to their heading line,
and past that the oldest lines are dropped behind a count. The entry itself is the model's text if it passes validation, else a
deterministic entry from the same facts, so an LLM outage never blocks or empties the summary. Entries live in
sessions/<node dir>/summary.md; SUMMARY.md is a pure function of them.
"""

import json
import os
import threading
from pathlib import Path

from app.lifecycle.nodes import NodeRef, NodeStore
from app.lifecycle.outcomes import read_outcomes
from app.llm.types import LLMError

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
TOKEN_CAP = 1500  # §3.2
CHARS_PER_TOKEN = 3.5
MAX_ENTRY_LINES = 7  # heading + 6 lines (summarizer.md: "at most 6 lines after the heading")
MAX_ENTRY_CHARS = 1100
HEADER = "# Archive summary\n\nOne entry per finished node, oldest first. Facts only.\n"
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _lock(track: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(track, threading.Lock())


def valid_entry(text) -> str | None:
    """The model's entry, cleaned, or None when it does not look like one (heading first, short, no code fences)."""
    if not isinstance(text, str):
        return None
    t = text.strip().replace("\r\n", "\n")
    lines = t.split("\n")
    if not t.startswith("## ") or len(lines) > MAX_ENTRY_LINES or len(t) > MAX_ENTRY_CHARS or "```" in t:
        return None
    return t


def assemble(entries: list[str], cap_tokens: int = TOKEN_CAP) -> str:
    """HEADER + entries (oldest first), shrunk oldest-first until the estimate fits `cap_tokens`."""
    parts = [e.strip() for e in entries]

    def text(ps: list[str], dropped: int = 0) -> str:
        note = f"\n(+{dropped} earlier node(s) omitted to keep this file short)\n" if dropped else ""
        return HEADER + note + "\n" + "\n\n".join(ps) + "\n"

    for i in range(len(parts)):  # 1. oldest entries shrink to their heading line
        if estimate_tokens(text(parts)) <= cap_tokens:
            return text(parts)
        parts[i] = parts[i].split("\n", 1)[0]
    dropped = 0
    while parts and estimate_tokens(text(parts, dropped)) > cap_tokens:  # 2. then the oldest headings go
        parts.pop(0)
        dropped += 1
    return text(parts, dropped)


class Summarizer:
    def __init__(self, store: NodeStore, router, domain_pack: str, debrief):
        self.store, self.router, self.domain_pack, self.debrief = store, router, domain_pack, debrief

    # --- inputs: facts only, no answer-key values ---------------------------------------------------------------------
    def inputs(self, ref: NodeRef) -> dict:
        fm = self.store.frontmatter(ref)
        v = self.debrief.view(ref)
        out = read_outcomes(self.store.root, ref.track).get(ref.id)
        prelab = [{"id": c["id"], "kind": "numeric", "subskill": c["subskill"], "within_tolerance": c["within"],
                   "error_pct": None if c["rel_error"] is None else round(100 * c["rel_error"], 1)} for c in v["calibration"]["numeric"]]  # fmt: skip
        prelab += [{"id": g["id"], "kind": "explain", "subskill": g["subskill"], "score": g["score"], "quote": g["quote"]} for g in v["grades"]["items"]]
        o = v["oral"]
        return {
            "node": {"id": ref.id, "title": fm.get("title"), "kind": fm.get("kind"), "domain": fm.get("domain"), "tier": fm.get("tier"),
                     "subskills": [str(s) for s in fm.get("subskills") or []], "brief": self.store.brief(ref)[:1200]},  # fmt: skip
            "outcome": {"label": out.label if out else str(fm.get("status")), "proof_passed": v["proof"]["passed"], "proof_submissions": v["proof"]["submissions"],
                        "oral_passed": o.get("passed"), "grades_pending": bool(fm.get("provisional"))},  # fmt: skip
            "prelab": prelab,
            "oral": [{"subskill": i["subskill"], "score": i["score"], "interleaved": i["interleaved"], "quote": i["quote"]} for i in o.get("items") or []],
            "misconceptions": {"logged": [m["text"] for m in v["misconceptions"]["logged"]], "resolved": [m["text"] for m in v["misconceptions"]["resolved"]]},
            "mastery_deltas": [{"domain": m["domain"], "tier": m["tier"], "from": m["mastery_before"], "to": m["mastery_after"]} for m in v["mastery"]],
        }  # fmt: skip

    @staticmethod
    def fallback_entry(inp: dict) -> str:
        """The same entry built by code (LLM unavailable or its text unusable)."""
        n, o, oral = inp["node"], inp["outcome"], inp["oral"]
        num = [p for p in inp["prelab"] if p["kind"] == "numeric"]
        expl = [p for p in inp["prelab"] if p["kind"] == "explain"]
        L = [f"## {n['id']:04d} {n['title']} ({n['kind']}, {n['domain']}, T{n['tier']}): {o['label']}",
             f"- Built: {n['title']}; subskills {', '.join(n['subskills'])}."]  # fmt: skip
        pre = []
        if num:
            errs = ", ".join("%s %+.0f%%" % (p["subskill"], p["error_pct"]) for p in num if p["error_pct"] is not None)
            pre.append(f"{sum(1 for p in num if p['within_tolerance'])}/{len(num)} numeric predictions within tolerance ({errs})")
        if expl:
            pre.append("explanations " + ", ".join(f"{p['subskill']} {p['score']}/3" for p in expl))
        L.append("- Prelab: " + ("; ".join(pre) if pre else "none") + ".")
        if oral:
            L.append(f"- Oral: {'passed' if o['oral_passed'] else 'not passed'}; " + ", ".join(f"{i['subskill']} {i['score']}/3" for i in oral) + ".")
        else:
            L.append("- Oral: " + ("grades pending." if o["grades_pending"] else "not graded."))
        m = inp["misconceptions"]
        if m["logged"] or m["resolved"]:
            L.append("- Misconceptions: " + "; ".join([*(f"logged: {t}" for t in m["logged"]), *(f"resolved: {t}" for t in m["resolved"])]) + ".")
        if inp["mastery_deltas"]:
            L.append("- Mastery: " + ", ".join(f"{d['domain']} {d['from']} -> {d['to']}" for d in inp["mastery_deltas"]) + ".")
        return "\n".join(L)

    # --- one node -------------------------------------------------------------------------------------------------------
    def entry_path(self, ref: NodeRef) -> Path:
        return self.store.session_dir(ref) / "summary.md"

    def summarize_node(self, ref: NodeRef) -> str:
        """Write sessions/<dir>/summary.md for a finished node -> the entry. Never raises for an LLM problem."""
        inp = self.inputs(ref)
        system = (PROMPTS / "summarizer.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain_pack)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(inp, ensure_ascii=False)}]
        entry = None
        try:
            res = self.router.complete("summarize", msgs, node_id=ref.id, track=ref.track)
            entry = valid_entry(res.text)
        except LLMError:
            pass
        entry = entry or self.fallback_entry(inp)
        p = self.entry_path(ref)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(entry + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, p)
        return entry

    # --- the file ---------------------------------------------------------------------------------------------------------
    def regenerate(self) -> str:
        """Reassemble archive/SUMMARY.md from the entries of every finished node (a finished node without an entry gets a code-built one,
        so backfilling never costs an LLM call). -> the text."""
        with _lock(self.store.track):
            entries = []
            for ref in self.store.all():
                if self.store.status(ref) != "done":
                    continue
                p = self.entry_path(ref)
                if p.is_file():
                    entries.append(p.read_text(encoding="utf-8").strip())
                else:
                    entries.append(self.fallback_entry(self.inputs(ref)))
            text = assemble(entries)
            dst = self.store.layout.summary_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".md.tmp")
            tmp.write_text(text, encoding="utf-8", newline="\n")
            os.replace(tmp, dst)
            return text

    def after_debrief(self, ref: NodeRef) -> str:
        """The §3.2 trigger: the node's entry, then the file."""
        self.summarize_node(ref)
        return self.regenerate()
