"""Manual live measurement (M4): per-turn latency and token usage of claude_code on a light-tier task,
default invocation (full Claude Code system prompt + tools) vs the light invocation (replaced system prompt, no tools/MCP).

    uv run python -m scripts.measure_light [--model claude-haiku-5-5] [--turns 4]

Calls the real `claude` CLI (subscription login); not part of the automated tests.
"""

import argparse
import json
import statistics

from app.llm.adapters.claude_code import ClaudeCodeAdapter
from app.llm.types import Request
from app.onboarding.service import load_prompt

ANSWERS = [
    "I want to design a 60 GHz LNA in SG13G2 for my thesis.",
    "About 8 hours a week, mostly evenings.",
    "I took analog IC design and one RF course; I have used ngspice and Cadence a little.",
    "I have never done EM simulation or measured S-parameters.",
]


def run(light: bool, model: str, turns: int) -> list[dict]:
    a = ClaudeCodeAdapter()
    system = {"role": "system", "content": load_prompt("interviewer", "rfic")}
    convo = [{"role": "user", "content": "Begin the interview."}]
    rows = []
    for i in range(turns):
        req = Request("interview", [system, *convo], schema=None, light=light)
        res = a.complete(req, model, 180)
        rows.append({"turn": i + 1, "latency_ms": res.latency_ms, **res.usage, "reply_chars": len(res.text)})
        convo += [{"role": "assistant", "content": res.text}, {"role": "user", "content": ANSWERS[i % len(ANSWERS)]}]
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--turns", type=int, default=4)
    args = ap.parse_args()
    out = {}
    for name, light in (("default", False), ("light", True)):
        rows = run(light, args.model, args.turns)
        out[name] = rows
        for r in rows:
            print(name, json.dumps(r))
        print(name, "median latency ms", statistics.median(r["latency_ms"] for r in rows),
              "| median input tokens", statistics.median(r["input_tokens"] for r in rows))


if __name__ == "__main__":
    main()
