"""M6c (manual, LIVE): first-reply latency of tutor_chat through the real router, on a temp data copy.

    uv run python -m scripts.m6c_tutor_latency [N]
Prints per-call seconds and model, then the median. Median > 10 s -> make claude-haiku-5-5 the tutor_chat primary.
"""

import statistics
import sys
import time

from app.builder.nodefile import set_frontmatter_scalar
from app.lifecycle.nodes import LifecycleError, NodeStore
from app.lifecycle.tutor import Tutor
from app.llm.router import Router
from scripts.tempdata import temp_data

QUESTIONS = [
    "My checker fails on the S11 error. What should I think about first?",
    "What does the SHORT dummy measure?",
    "Why do I subtract the OPEN in the Y domain?",
    "How do I convert between Y and Z in numpy?",
    "Which frame element makes the residual error nonzero?",
    "How do I know my de-embedding is working before running the checker?",
]


def main(n: int) -> None:
    with temp_data() as root:
        name = "0004-de-embed-a-simulated-sg13g2-hbt-from-a"
        set_frontmatter_scalar(root / "archive" / "nodes" / name / "node.md", "status", "lab")
        store = NodeStore(root)
        ref = store.find(4)
        tutor = Tutor(store, Router(), "rfic")
        times = []
        for q in QUESTIONS[:n]:
            t0 = time.monotonic()
            try:
                out = tutor.chat(ref, q)
                model = out["model"]
            except LifecycleError as e:
                model = f"ERROR {e.message[:60]}"
            dt = time.monotonic() - t0
            times.append(dt)
            print(f"{dt:6.1f}s  {model}", flush=True)
        print(f"median {statistics.median(times):.1f}s over {len(times)} calls")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 6)
