"""M8 live check: a failed oral exam spawns a kind=remediation child that is BUILT END TO END (previously the Builder was stubbed).

    BLAHAJ_DATA_DIR=<real data dir> uv run python -m scripts.m8_remediation_live [--out FILE]

Everything runs on a TEMP COPY of data/ (scripts/tempdata.py), track rfic-default. Node 0006 is put at stage oral with wrong prelab answers, a
typed oral with weak answers is graded by the real pinned grader (claude-sonnet-5-5), the real Planner proposes the remediation option, and the real
Builder (claude -p + MCP in the Builder container, then the release gate incl. the fidelity review) builds it on an in-process job worker.
Needs Docker running and the claude CLI logged in. Prints the evidence; exit 0 only if every check holds.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.voice.models import models_dir  # noqa: E402

os.environ["BLAHAJ_MODELS_DIR"] = str(models_dir())

from scripts.tempdata import temp_data  # noqa: E402

TRACK, NODE = "rfic-default", 6
WEAK = "I am not sure. I think it is mostly numerical noise in the simulator, and the pad does not really matter."


def say(m: str) -> None:
    print(time.strftime("%H:%M:%S"), m, flush=True)


async def run() -> dict:
    import yaml

    from app.builder.service import BuildService
    from app.datastore.cache import rebuild_db
    from app.datastore.misconceptions import fold, read_records
    from app.domains import load_domain
    from app.jobs.manager import JobManager
    from app.jobs.store import JobStore, default_db_path
    from app.lifecycle.graph import graph_view
    from app.lifecycle.outcomes import read_outcomes
    from app.lifecycle.service import Lifecycle
    from app.llm.router import Router

    with temp_data() as root:
        say(f"temp data copy {root}")
        domain = load_domain()
        router = Router()
        jobs = JobManager(JobStore(default_db_path()), concurrency=1)
        await jobs.start()
        try:
            lc = Lifecycle(root, TRACK, router, domain, jobs=jobs)
            ref = lc.store.find(NODE)
            answers = {it["id"]: float(it["value"]) * 3.0 if it["kind"] == "numeric" else "I do not know" for it in lc.store.prelab_key(ref)}
            (ref.path / "prelab_answers.yaml").write_text(yaml.safe_dump({"submitted": "2026-11-11T00:00:00+00:00", "answers": answers, "units": {}}), encoding="utf-8")
            lc.store.set_scalar(ref, "status", "oral")
            rebuild_db(root)
            nodes_before = {p.name for p in lc.store.layout.nodes_dir.iterdir()}

            v = lc.oral.start(ref, "text", False)
            say(f"oral plan {v['plan']}")
            n = 0
            while lc.oral.view(ref)["status"] == "active" and n < 25:
                lc.oral.answer(ref, WEAK)
                n += 1
            say(f"{n} weak answers; finishing (real grader + real Planner + build job start)")
            t0 = time.monotonic()
            d = await asyncio.to_thread(lc.oral_finish, NODE)
            o = d["oral"]
            say(f"graded in {time.monotonic() - t0:.0f} s: passed {o['passed']} mean {o['mean']} model {o['model']}; remediation {o['remediation'] and o['remediation'].get('status')}")
            rem = o["remediation"] or {}
            res: dict = {"oral_passed": o["passed"], "remediation_start": {k: rem.get(k) for k in ("status", "node_dir", "child_id", "title", "error")},
                         "option": rem.get("option")}  # fmt: skip
            if rem.get("status") != "building":
                return {**res, "ok": False, "why": f"remediation did not start: {rem}"}
            say(f"remediation option: {rem['option']}")
            seen, t_build = 0, time.monotonic()
            while True:
                await asyncio.sleep(15)
                j = jobs.store.get(rem["job_id"])
                for line in j.log_tail[seen:] if len(j.log_tail) > seen else []:
                    say(f"  | {line}")
                seen = len(j.log_tail)
                if j.status in ("done", "failed"):
                    break
            res["build"] = {"job_status": j.status, "job_error": j.error, "wall_s": round(time.monotonic() - t_build)}
            rec = BuildService(root, TRACK, router, domain).load_record(rem["node_dir"]) or {}
            res["record"] = {"passed": rec.get("passed"), "attempts": len(rec.get("attempts", [])), "wall_s": rec.get("wall_s"), "sim_runtime_s": rec.get("sim_runtime_s"),
                             "gate4_targets": rec.get("context"), "gate4_hits": rec.get("gate4_hits"),
                             "models": [a.get("model") for a in rec.get("attempts", [])], "review": [a.get("review", {}).get("passed") for a in rec.get("attempts", [])]}  # fmt: skip
            after = {p.name for p in lc.store.layout.nodes_dir.iterdir()}
            new = sorted(after - nodes_before)
            child = lc.store.find(int(rem["node_dir"].split("-", 1)[0])) if new else None
            fm = lc.store.frontmatter(child) if child else {}
            oral_after = lc.oral.debrief_view(ref)
            graph = graph_view(root, TRACK)
            mis = [m for m in fold(read_records(lc.store.layout.misconceptions_path, TRACK)) if m["node_id"] == NODE]
            checks = {
                "job done": j.status == "done",
                "gate passed": bool(rec.get("passed")),
                "child node released": bool(new) and fm.get("status") == "prelab",
                "child kind=remediation": fm.get("kind") == "remediation",
                "child parents == [parent]": fm.get("parents") == [NODE],
                "parent remediation ready": (oral_after.get("remediation") or {}).get("status") == "ready",
                "graph edge parent->child": child is not None and {"source": NODE, "target": child.id} in graph["edges"],
                "parent outcome oral_failed": read_outcomes(root, TRACK)[NODE].label == "oral_failed",
                "misconceptions logged on parent": bool(mis),
                "child interleaves the open misconceptions": bool(rec.get("gate4_hits")) or not (rec.get("context") or {}).get("open_misconceptions"),
            }  # fmt: skip
            res.update(child={k: fm.get(k) for k in ("id", "slug", "title", "kind", "domain", "subskills", "tier", "parents", "status", "size_est_min", "sim_runtime_s")},
                       misconceptions=[{"subskill": m["subskill"], "text": m["text"][:140]} for m in mis], checks=checks, ok=all(checks.values()))  # fmt: skip
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
    out = Path(a.out) if a.out else Path(os.environ.get("TEMP", ".")) / "m8_remediation_live.json"
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    say(f"{'OK' if res.get('ok') else 'FAILED'}; results: {out}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
