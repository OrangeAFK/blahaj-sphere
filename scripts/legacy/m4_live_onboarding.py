"""Manual live end-to-end run of M4 onboarding (real router, real backends) in a FRESH temp data dir.

    uv run python -m scripts.legacy.m4_live_onboarding [--keep DIR]

Drives the HTTP API in-process. A simulated learner (claude-haiku-5-5, persona below) answers the interview and the
diagnostic. Uses the OpenRouter key from data/settings.json (copied into the temp dir) and the claude login.
Not part of the automated tests (those use mock adapters).
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PERSONA = (
    "You are a graduate student role-playing a learner. Background: finished an analog IC design course and one RF "
    "circuits course; know Smith chart basics, gm/Id, S-parameters and Friis at a textbook level; have used ngspice; "
    "have never run an EM simulator or used a VNA. Goal: design a 60 GHz LNA in IHP SG13G2 for a thesis, ~8 hours/week. "
    "Answer the question you are given in 1-4 plain sentences, the way a real learner with that background would, "
    "including honest gaps. Output only the answer."
)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # model text has non-cp1252 characters
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=Path, default=None, help="use/keep this data dir instead of a temp one")
    args = ap.parse_args()
    data = args.keep or Path(tempfile.mkdtemp(prefix="blahaj-m4-live-"))
    data.mkdir(parents=True, exist_ok=True)
    if (REPO / "data" / "settings.json").exists():
        shutil.copy(REPO / "data" / "settings.json", data / "settings.json")
    os.environ["BLAHAJ_DATA_DIR"] = str(data)
    print("data dir:", data)

    from fastapi.testclient import TestClient

    from app.llm.adapters.claude_code import ClaudeCodeAdapter
    from app.llm.types import Request
    from app.main import create_app

    persona = ClaudeCodeAdapter()

    def learner(question: str) -> str:
        msgs = [{"role": "system", "content": PERSONA}, {"role": "user", "content": question}]
        return persona.complete(Request("tutor_chat", msgs, light=True), "claude-haiku-5-5", 120).text.strip()

    c = TestClient(create_app())
    t0 = time.monotonic()

    def post(path, body=None):
        t = time.monotonic()
        r = c.post(f"/api/onboarding{path}", json=body or {})
        print(f"  POST {path} -> {r.status_code} in {time.monotonic() - t:.1f}s")
        assert r.status_code == 200, r.text
        return r.json()

    stage = c.get("/api/onboarding/state").json()["stage"]  # --keep makes the run resumable
    print("resuming at stage:", stage)
    print("== interview")
    v = post("/interview/turn") if stage == "interview" else {"done": True}
    while not v["done"]:
        q = v["last_message"]
        a = learner(q)
        print(f"  Q: {q}\n  A: {a}")
        v = post("/interview/turn", {"answer": a})
    print("goals.md:\n" + (data / "profile" / "goals.md").read_text(encoding="utf-8"))

    print("== framework")
    if stage in ("interview", "framework"):
        fw = post("/framework/draft")["framework"]
        print("  domains:", [d["id"] for d in fw["domains"]])
        fw["domains"][0]["scope"] = fw["domains"][0]["scope"] + " (user-edited)"  # the approval step takes the user's edits
        assert c.put("/api/onboarding/framework/draft", json=fw).status_code == 200
        assert post("/framework/approve", {"framework": fw})["frozen"]
    print("  frozen:", (data / "framework" / "competency-v1.yaml").exists())

    print("== diagnostic")
    v = post("/diagnostic/next")
    while not v["done"]:
        p = v["pending"]
        a = learner(p["question"])
        print(f"  [{p['domain']} T{p['tier']}] Q: {p['question']}\n    A: {a}")
        r = post("/diagnostic/answer", {"answer": a})
        print(f"    score {r['score']}/3")
        v = r["view"]
        if not v["done"]:
            v = post("/diagnostic/next")
    print("  tiers:", json.dumps(v["tiers"]))
    print(f"total {time.monotonic() - t0:.0f}s; state: {c.get('/api/onboarding/state').json()}")

    print("== llm_calls")
    con = sqlite3.connect(data / "llm.db")
    for row in con.execute(
        "select task_type, backend, model, status, input_tokens, output_tokens, latency_ms from llm_calls order by rowid"
    ):
        print("  ", row)
    print("events:", sum(1 for _ in open(data / "events.jsonl", encoding="utf-8")))
    print("cache scores:", sqlite3.connect(data / "cache.db").execute("select domain, tier from scores").fetchall())


if __name__ == "__main__":
    main()
