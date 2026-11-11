"""Manual live smoke (M4.1), real backends, fresh temp data dir with NO OpenRouter key:

1. light task (tutor_chat) must fall through the three keyless OpenRouter entries to claude_code/claude-haiku-5-5;
2. oral_dialogue must be served by claude_code/claude-haiku-5-5 first;
3. grade_diagnostic (no tools) runs on the minimal system prompt: compare tokens/latency with M4 (~25.1k in, ~5.1 s).

    uv run python -m scripts.smoke_m41
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
data = Path(tempfile.mkdtemp(prefix="blahaj-m41-"))
os.environ["BLAHAJ_DATA_DIR"] = str(data)  # no settings.json here => openrouter_api_key == ""

from app.llm.router import Router  # noqa: E402
from app.onboarding.service import GRADE_SCHEMA, load_prompt  # noqa: E402
from app.settings import load_settings  # noqa: E402

assert load_settings().openrouter_api_key == "", "key must be blank for this test"
r = Router()
msgs = [{"role": "system", "content": "You are a terse RF tutor. One sentence."},
        {"role": "user", "content": "What does a return loss of 10 dB mean?"}]
for task in ("tutor_chat", "oral_dialogue"):
    res = r.complete(task, msgs)
    print(f"{task}: served by {res.backend}/{res.model} in {res.latency_ms} ms, usage {res.usage}\n   {res.text.strip()[:160]}")

grade_user = {
    "question": "Why does an L-network match only over a narrow band?",
    "anchors": {"0": "blank or wrong", "1": "mentions Q vaguely", "2": "links loaded Q to bandwidth",
                "3": "links loaded Q to bandwidth and gives the formula or magnitude"},
    "answer": "The two reactances cancel only at one frequency, and the loaded Q = sqrt(Rhigh/Rlow - 1) sets how fast they drift apart, so bandwidth is about f0/Q.",
}
g = r.complete("grade_diagnostic", [{"role": "system", "content": load_prompt("diagnostic_grader", "rfic")},
                                    {"role": "user", "content": json.dumps(grade_user)}], schema=GRADE_SCHEMA)
print(f"grade_diagnostic: {g.backend}/{g.model} in {g.latency_ms} ms, usage {g.usage}\n   {json.dumps(g.json)[:300]}")

print("llm_calls:")
for row in sqlite3.connect(data / "llm.db").execute(
        "select task_type, backend, model, status, substr(error,1,60), input_tokens, output_tokens, latency_ms from llm_calls order by id"):
    print("  ", row)
