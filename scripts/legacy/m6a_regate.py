"""M6a (manual, LIVE): re-gate one released node with the current code gates + the fidelity review.

    uv run python -m scripts.legacy.m6a_regate 0004-de-embed-a-simulated-sg13g2-hbt-from-a
"""

import json
import sys
from pathlib import Path

import yaml

from app.builder.gate import GateContext, run_gate
from app.builder.nodefile import read_node_md
from app.builder.review import run_review
from app.domains import load_domain
from app.llm.router import Router
from app.settings import data_dir


def main(name: str) -> int:
    domain = load_domain()
    assert domain is not None and domain.run_checker is not None
    root = data_dir()
    node = root / "archive" / "nodes" / name
    fm, _ = read_node_md(node / "node.md")
    option = {"title": fm["title"], "kind": fm["kind"], "domain": fm["domain"], "subskills": fm["subskills"], "tier": fm["tier"], "size_est_min": fm["size_est_min"], "why_now": ""}
    rec_path = root / "builds" / name / "record.json"
    ctx = GateContext.from_dict(json.loads(rec_path.read_text(encoding="utf-8")).get("context") if rec_path.is_file() else None)
    res = run_gate(node, domain.run_checker, ctx)
    print("GATE", "PASS" if res.passed else "FAIL", "sim_runtime_s", res.sim_runtime_s)
    for e in res.errors:
        print("  ERROR", e)
    for w in res.warnings:
        print("  warn ", w)
    print("metric_sources", res.metric_sources)
    ok = res.passed
    if res.passed:
        rv = run_review(Router(), node, option, res.ref_metrics, res.metric_sources, domain.name, fm["id"])
        print("REVIEW", "PASS" if rv.passed else "FAIL", [(c["id"], c.get("pass")) for c in rv.checks])
        for e in rv.errors:
            print("  ERROR", e)
        ok = rv.passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
