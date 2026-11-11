"""M5 acceptance (manual, LIVE: real Planner + Builder calls on the subscription, real container). Not run by pytest.

    uv run python -m scripts.legacy.m5_acceptance [--rounds 3]     # resumes: appends to an existing m5_acceptance.json

Each round: Planner options from the real data/ (goals.md, frozen framework, diagnostic tiers) -> pick the first
option whose domain+subskills no earlier round built -> BuildService as a background job on an in-process JobManager
-> wait. Results (per node: wall time, usage, tool calls, gate-4 hits) -> data/builds/m5_acceptance.json.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime

from app.builder.service import BuildService
from app.domains import load_domain
from app.jobs.manager import JobManager
from app.jobs.store import JobStore, default_db_path
from app.llm.router import Router
from app.planner.service import Planner
from app.settings import data_dir


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def summarize(rec: dict) -> dict:
    tot = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "num_turns": 0, "notional_cost_usd": 0.0}
    tools: dict[str, int] = {}
    denials: list[str] = []
    models: set[str] = set()
    for a in rec.get("attempts", []):
        for k in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"):
            tot[k] += int((a.get("usage") or {}).get(k) or 0)
        meta = a.get("meta") or {}
        tot["num_turns"] += int(meta.get("num_turns") or 0)
        tot["notional_cost_usd"] += float(meta.get("notional_cost_usd") or 0)
        denials += meta.get("permission_denials") or []
        models |= set(meta.get("models") or [])
        for t, n in (a.get("tool_calls") or {}).items():
            tools[t] = tools.get(t, 0) + n
    tot["notional_cost_usd"] = round(tot["notional_cost_usd"], 2)
    return {
        "node_dir": rec["node_dir"], "passed": rec["passed"], "retries": len(rec["attempts"]) - 1, "wall_s": rec.get("wall_s"),
        "agent_s": round(sum(a.get("agent_wall_s", 0) for a in rec["attempts"]), 1),
        "gate_s": round(sum(a.get("gate_wall_s", 0) for a in rec["attempts"]), 1), "sim_runtime_s": rec.get("sim_runtime_s"),
        "models": sorted(models), "usage": tot, "tool_calls": tools, "permission_denials": denials,
        "targets": rec["context"], "gate4_hits": rec.get("gate4_hits"),
        "warnings": (rec["attempts"][-1].get("gate") or {}).get("warnings", []),
    }  # fmt: skip


async def run(rounds: int) -> int:
    root = data_dir()
    domain = load_domain()
    router = Router()
    planner = Planner(root, router, domain.name)
    builder = BuildService(root, router, domain)
    jobs = JobManager(JobStore(default_db_path()), concurrency=1)
    await jobs.start()
    out_path = root / "builds" / "m5_acceptance.json"
    # Resume: earlier rounds (e.g. before an environment crash) stay in the results and count toward acceptance.
    results: dict = (json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file()
                     else {"started": datetime.now().astimezone().isoformat(timespec="seconds"), "rounds": []})  # fmt: skip
    built: set[tuple] = {(x["options"][x["picked"]]["domain"], tuple(sorted(x["options"][x["picked"]]["subskills"])))
                         for x in results["rounds"]}  # fmt: skip
    first = len(results["rounds"]) + 1
    try:
        for r in range(first, first + rounds):
            t0 = time.monotonic()
            log(f"round {r}: planner")
            opts = await asyncio.to_thread(planner.generate)
            plan_s = round(time.monotonic() - t0, 1)
            for i, o in enumerate(opts["options"]):
                log(f"  option {i}: [{o['domain']} T{o['tier']} {o['size_est_min']} min] {o['title']} {o['subskills']}")
            idx = next((i for i, o in enumerate(opts["options"]) if (o["domain"], tuple(sorted(o["subskills"]))) not in built), 0)
            option = opts["options"][idx]
            built.add((option["domain"], tuple(sorted(option["subskills"]))))
            job, plan = builder.start(jobs, option)
            log(f"round {r}: picked option {idx}; building {plan.name} (job {job.id}); targets {plan.ctx.targets()}")
            seen = 0
            while True:
                await asyncio.sleep(10)
                j = jobs.store.get(job.id)
                for line in j.log_tail[seen:] if len(j.log_tail) > seen else []:
                    log(f"  | {line}")
                seen = len(j.log_tail)
                if j.status in ("done", "failed"):
                    break
            rec = builder.load_record(plan.name) or {"node_dir": plan.name, "passed": False, "attempts": [], "context": {}}
            entry = {"round": r, "planner_s": plan_s, "planner_model": opts["model"], "options": opts["options"], "picked": idx,
                     "job_status": j.status, "job_error": j.error, **summarize(rec)}  # fmt: skip
            results["rounds"].append(entry)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
            log(f"round {r}: {'PASS' if entry['passed'] else 'FAIL'} wall {entry['wall_s']} s, retries {entry['retries']}, "
                f"tools {entry['tool_calls']}, gate4 hits {entry['gate4_hits']}")  # fmt: skip
            if not entry["passed"]:
                log(f"stopping: {j.error}")
                return 1
    finally:
        await jobs.stop()
    ok = all(x["passed"] for x in results["rounds"]) and any(x["gate4_hits"] for x in results["rounds"])
    log(f"ACCEPTANCE {'PASS' if ok else 'FAIL'}: {sum(x['passed'] for x in results['rounds'])}/{len(results['rounds'])} nodes passed")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    return asyncio.run(run(ap.parse_args().rounds))


if __name__ == "__main__":
    sys.exit(main())
