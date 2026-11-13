"""M10 live validation: ONE real build of a kind=remediation option on a temp copy of data/, to validate the builder.md changes that no live
build had exercised: (a) `kind: remediation` in the skeleton (M8 fix) and (b) hidden `key_terms` on every oral_key item (M8.1; gate G0 rejects
a missing/malformed list).

    BLAHAJ_DATA_DIR=<real data dir> uv run python -m scripts.m10_live_build [--out FILE]

Needs Docker running and the claude CLI logged in. Real data/ is never touched (scripts/tempdata.py). Exit 0 only if every check holds.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.tempdata import temp_data  # noqa: E402

TRACK = "rfic-default"


def say(m: str) -> None:
    print(time.strftime("%H:%M:%S"), m, flush=True)


async def run() -> dict:
    import yaml

    from app.builder.service import BuildService
    from app.datastore.cache import DataStore
    from app.domains import load_domain
    from app.jobs.manager import JobManager
    from app.jobs.store import JobStore, default_db_path
    from app.lifecycle.nodes import NodeStore
    from app.llm.router import Router

    with temp_data() as root:
        say(f"temp data copy {root}")
        domain, router = load_domain(), Router()
        scores = DataStore(root, TRACK).scores()
        tier = int((scores.get("matching") or {"tier": 1})["tier"])
        option = {"title": "Component Q and the real loss of an L-match at 28 GHz", "kind": "remediation", "domain": "matching",
                  "subskills": ["matching.l_network", "smith_chart.navigation"], "tier": tier, "size_est_min": 40,
                  "why_now": "Remediation: the learner treated lumped matching elements as ideal; show what finite component Q does to an L-match."}  # fmt: skip
        jobs = JobManager(JobStore(default_db_path()), concurrency=1)
        await jobs.start()
        try:
            builder = BuildService(root, TRACK, router, domain)
            job, plan = builder.start(jobs, option)
            say(f"build job {job.id} -> {plan.name}")
            t0, seen = time.monotonic(), 0
            while True:
                await asyncio.sleep(20)
                j = jobs.store.get(job.id)
                for line in j.log_tail[seen:] if len(j.log_tail) > seen else []:
                    say(f"  | {line}")
                seen = len(j.log_tail)
                if j.status in ("done", "failed"):
                    break
            wall = round(time.monotonic() - t0)
            rec = builder.load_record(plan.name) or {}
            res: dict = {"job_status": j.status, "job_error": j.error, "wall_s": wall, "attempts": len(rec.get("attempts", [])),
                         "models": [a.get("model") for a in rec.get("attempts", [])],
                         "gate_errors_per_attempt": [a.get("gate", {}).get("errors") for a in rec.get("attempts", [])],
                         "review": [(a.get("review") or {}).get("passed") for a in rec.get("attempts", [])]}  # fmt: skip
            store = NodeStore(root, TRACK)
            checks = {"job done": j.status == "done", "gate passed": bool(rec.get("passed"))}
            if j.status == "done":
                ref = store.find(plan.node_id)
                fm = store.frontmatter(ref)
                key = store.oral_key(ref)
                terms = [it.get("key_terms") for it in key]
                checks["node.md kind == remediation"] = fm.get("kind") == "remediation"
                checks["every oral item has 3-8 key_terms"] = bool(key) and all(isinstance(t, list) and 3 <= len(t) <= 8 for t in terms)
                checks["first attempt had no kind/key_terms gate error"] = not any(
                    "kind" in str(e) or "key_terms" in str(e) for e in (res["gate_errors_per_attempt"][0] or []))  # fmt: skip
                res["node"] = {k: fm.get(k) for k in ("id", "slug", "title", "kind", "domain", "subskills", "tier", "size_est_min", "sim_runtime_s")}
                res["oral_items"] = [{"id": it["id"], "subskill": it.get("subskill"), "interleaved": it.get("interleaved"), "key_terms": it.get("key_terms")} for it in key]
            res.update(checks=checks, ok=all(checks.values()))
            for k, v_ in checks.items():
                say(f"  {'PASS' if v_ else 'FAIL'}  {k}")
            return res
        finally:
            await jobs.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res = asyncio.run(run())
    out = Path(a.out) if a.out else Path(os.environ.get("TEMP", ".")) / "m10_live_build.json"
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    say(f"{'OK' if res.get('ok') else 'FAILED'}; results: {out}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
