"""M5.1 (manual, LIVE): rebuild released nodes under the stricter gate (provenance, starter hard fail, fidelity review).

    uv run python -m scripts.m51_rebuild 0001-extract-l-q-and-self-resonance-of-an 0003-extract-60-ghz-gsg-pad-parasitics-with
    uv run python -m scripts.m51_rebuild --review-only 0004-de-embed-a-simulated-sg13g2-hbt-from-a

Each original is marked `abandoned` (kept on disk) and its stored option is built again as a new node through the
background job. --review-only runs just the fidelity review on an existing node (informational, nothing changes).
Results -> data/builds/m51_rebuild.json.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

from app.builder.review import run_review
from app.builder.service import BuildService
from app.domains import load_domain
from app.jobs.manager import JobManager
from app.jobs.store import JobStore, default_db_path
from app.llm.router import Router
from app.settings import data_dir
from scripts.m5_acceptance import log, summarize


def review_summary(rec: dict) -> list[dict]:
    return [{"attempt": a["n"], "passed": a["review"]["passed"], "model": a["review"]["model"], "latency_ms": a["review"]["latency_ms"],
             "usage": a["review"]["usage"], "checks": [{k: c.get(k) for k in ("id", "pass", "file", "quote", "reason", "quote_verified")}
                                                       for c in a["review"]["checks"]]}
            for a in rec.get("attempts", []) if a.get("review")]  # fmt: skip


async def rebuild(dirs: list[str]) -> int:
    root = data_dir()
    domain = load_domain()
    router = Router()
    svc = BuildService(root, router, domain)
    jobs = JobManager(JobStore(default_db_path()), concurrency=1)
    await jobs.start()
    out_path = root / "builds" / "m51_rebuild.json"
    results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {"rebuilds": [], "reviews": []}
    ok = True
    try:
        for d in dirs:
            orig = svc.load_record(d)
            if orig is None:
                log(f"{d}: no build record, skipped")
                ok = False
                continue
            option = orig["option"]
            svc.abandon(d)
            job, plan = svc.start(jobs, option)
            log(f"{d} -> abandoned; rebuilding as {plan.name} (job {job.id}); targets {sorted(plan.ctx.targets())}")
            seen = 0
            while True:
                await asyncio.sleep(10)
                j = jobs.store.get(job.id)
                for line in j.log_tail[seen:]:
                    log(f"  | {line}")
                seen = len(j.log_tail)
                if j.status in ("done", "failed"):
                    break
            rec = svc.load_record(plan.name) or {"node_dir": plan.name, "passed": False, "attempts": [], "context": {}}
            entry = {"original": d, "option": option, "job_status": j.status, "job_error": j.error, **summarize(rec),
                     "reviews": review_summary(rec)}  # fmt: skip
            results["rebuilds"].append(entry)
            out_path.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
            log(f"{plan.name}: {'PASS' if entry['passed'] else 'FAIL'} wall {entry['wall_s']} s, retries {entry['retries']}, tools {entry['tool_calls']}")
            ok = ok and entry["passed"]
    finally:
        await jobs.stop()
    return 0 if ok else 1


def review_only(dirs: list[str]) -> int:
    root = data_dir()
    domain = load_domain()
    svc = BuildService(root, Router(), domain)
    out_path = root / "builds" / "m51_rebuild.json"
    results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {"rebuilds": [], "reviews": []}
    for d in dirs:
        rec = svc.load_record(d) or {}
        node = svc.layout.nodes_dir / d
        metrics = json.loads((node / "_hidden" / "ref_result.json").read_text(encoding="utf-8")).get("metrics", {})
        rv = run_review(svc.router, node, rec.get("option", {}), metrics, {}, domain.name)
        results["reviews"].append({"node_dir": d, "ts": datetime.now().astimezone().isoformat(timespec="seconds"), **rv.to_dict()})
        log(f"{d}: review {'PASS' if rv.passed else 'FAIL'} ({rv.latency_ms} ms)")
        for c in rv.checks:
            log(f"  {c['id']}: {'pass' if c['pass'] else 'FAIL'} [{c['file']}] {c['quote'][:120]!r} - {c['reason']}")
    out_path.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--review-only", action="store_true")
    a = ap.parse_args()
    return review_only(a.dirs) if a.review_only else asyncio.run(rebuild(a.dirs))


if __name__ == "__main__":
    sys.exit(main())
