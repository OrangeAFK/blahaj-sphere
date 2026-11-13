"""M10 full-loop smoke (SPEC §14): create track -> onboarding (mock) -> plan -> BUILD (real) -> prelab -> lab -> proof -> oral (text) -> debrief
-> tier-exam unlock from seeded events -> exam pass -> tier-up. Everything runs through the HTTP API on a TEMPORARY COPY of data/.

    BLAHAJ_DATA_DIR=<real data dir> uv run python -m scripts.m10_smoke [--out FILE]

Only the Builder (build_challenge, review_build) and the lab/proof checker run for real (claude CLI, Docker toolchain). Every other model call is
the mock: canned interview/framework/diagnostic, and a scripted Planner, graders and exam writer that answer from the request they receive. The
"learner" copies the node's reference solution into the workspace (the script reads _hidden/, which the app itself never shows a learner).
Exit 0 only if every check holds. Needs Docker running and the claude CLI logged in.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.tempdata import temp_data  # noqa: E402

REAL_TASKS = ("build_challenge", "review_build")
CHECKS: dict[str, bool] = {}


def say(m: str) -> None:
    print(time.strftime("%H:%M:%S"), m, flush=True)


def check(ok: bool, what: str) -> None:
    CHECKS[what] = bool(ok)
    say(f"  {'PASS' if ok else 'FAIL'}  {what}")


class Smoke:
    """One adapter behind every backend: the real claude_code adapter for the Builder tasks, scripted answers for the rest."""

    name = "smoke"

    def __init__(self, real, mock):
        self.real, self.mock = real, mock
        self.calls: list[str] = []

    def complete(self, req, model, timeout_s):
        from app.llm.types import Result

        self.calls.append(req.task_type)
        if req.task_type in REAL_TASKS:
            return self.real.complete(req, model, timeout_s)
        fn = getattr(self, f"_{req.task_type}", None)
        if fn is None:
            return self.mock.complete(req, model, timeout_s)
        out = fn(json.loads(req.messages[-1]["content"]) if req.messages else {}, req)
        return Result(text=json.dumps(out), json=out, usage={}, backend="mock", model="smoke-mock")

    # -- scripted tasks ---------------------------------------------------------------------------------------
    def _plan_branches(self, inp, req):
        sc = inp["scores"]
        tier = sc["matching"]["tier"]
        mk = lambda title, dom, subs: {"title": title, "why_now": "Smoke run: a small simulation-backed matching task.", "kind": "challenge", "domain": dom,
                                       "subskills": subs, "tier": min(tier, sc[dom]["tier"]), "size_est_min": 30}  # noqa: E731
        return {"options": [mk("Hand-design an L-match at 2.4 GHz and verify it in ngspice", "matching", ["matching.l_network"]),
                            mk("Cascade the noise figure of an LNA plus mixer", "noise", ["noise.figure_cascade"])]}

    def _grade_prelab_explain(self, inp, req):
        return {"items": [{"id": i["id"], "score": 3, "quote": "", "rationale": "smoke"} for i in inp["items"]], "misconceptions": []}

    def _grade_oral(self, inp, req):
        return {"items": [{"id": i["id"], "score": 3, "quote": "", "rationale": "smoke"} for i in inp["items"]], "misconceptions": []}

    _grade_tier_exam = _grade_oral

    def _build_exam(self, inp, req):
        subs = list(dict.fromkeys([*inp["evidenced_subskills"], *inp["subskills"]]))[:3]
        items = [{"id": f"e{i}", "subskill": subs[(i - 1) % len(subs)], "question": f"Smoke exam question {i} on {subs[(i - 1) % len(subs)]}?",
                  "anchors": {str(k): f"anchor {k}" for k in range(4)}, "probes": ["probe a?", "probe b?"], "key_terms": ["quality factor", "bandwidth", "match"]}
                 for i in range(1, 7)]  # fmt: skip
        return {"title": f"Tier {inp['tier']} exam: {inp['domain']['name']}", "brief": "Six oral questions across the domain.", "items": items}


def poll_job(c, job_id: str, what: str, every: float = 15.0, limit_s: float = 4500) -> dict:
    t0, seen = time.time(), 0
    while True:
        j = c.get(f"/api/jobs/{job_id}").json()
        tail = j.get("log_tail") or []
        for line in tail[seen:]:
            say(f"  | {line}")
        seen = len(tail)
        if j["status"] in ("done", "failed"):
            say(f"{what}: {j['status']} after {time.time() - t0:.0f} s")
            return j
        if time.time() - t0 > limit_s:
            raise TimeoutError(what)
        time.sleep(every)


def seed_events(root: Path, track: str, domain: str, tier: int, subs: list[str], n: int = 13) -> None:
    """13 oral_item events at v=0.8 (the §8.3 reference: mastery exactly 65) over 3 subskills, at the domain's current tier."""
    from app.datastore.events import append_event, new_event
    from app.datastore.layout import DataLayout

    for i in range(n):
        append_event(DataLayout(root).events_path, new_event(track=track, node_id=1, domain=domain, subskill=subs[i % len(subs)], tier=tier, type="oral_item",
                                                             value=0.8, weight=1.0, grader_model="seed", framework_version=1))  # fmt: skip


def answer_all(c, nid: int, key: list[dict]) -> None:
    """Text-mode oral: long answers that use every key term of the item being asked (no live probes)."""
    c.post(f"/api/nodes/{nid}/oral/start", json={"mode": "text", "tts": False})
    by_id = {it["id"]: it for it in key}
    for _ in range(40):
        v = c.get(f"/api/nodes/{nid}/oral").json()
        if v["status"] == "complete":
            return
        last = [t for t in v["turns"] if t["role"] == "examiner"][-1]
        item = by_id.get(next((k for k, it in by_id.items() if it["question"] == last["text"]), ""), {})
        terms = ", ".join(item.get("key_terms") or ["quality factor", "bandwidth", "match"])
        text = (f"I would reason about this through {terms}: the quality factor of the network sets the bandwidth, the reactive parts cancel at the design "
                "frequency, and the match transforms the load resistance to the source resistance, which I would then confirm in simulation.")
        r = c.post(f"/api/nodes/{nid}/oral/answer", json={"text": text})
        assert r.status_code == 200, r.text


def run(root: Path) -> dict:
    import yaml
    from fastapi.testclient import TestClient

    from app.datastore.layout import DataLayout
    from app.datastore.misconceptions import fold, read_records
    from app.llm.adapters.mock import MockAdapter
    from app.llm.config import load_routing
    from app.llm.router import default_adapters
    from app.llm.router import Router
    from app.llm.store import Store
    from app.main import create_app

    adapters = default_adapters()
    smoke = Smoke(adapters["claude_code"], MockAdapter())
    app = create_app()
    app.state.llm_router = Router(load_routing(), Store(root / "llm.db"), {n: smoke for n in ("claude_code", "cursor_cli", "openrouter", "mock")})
    res: dict = {}
    TRACK = "m10-smoke"
    with TestClient(app) as c:
        say("1. create track + onboarding (mock)")
        r = c.post("/api/tracks", json={"name": "M10 smoke", "domain": "rfic"})
        check(r.status_code == 201 and r.json()["active"] == TRACK, "track created and active")
        c.post("/api/onboarding/interview/turn", json={})
        c.post("/api/onboarding/framework/draft")
        check(c.post("/api/onboarding/framework/approve", json={}).status_code == 200, "framework approved")
        c.post("/api/onboarding/diagnostic/next")
        for _ in range(60):
            d = c.post("/api/onboarding/diagnostic/answer", json={"answer": "loaded Q sets the bandwidth"}).json()["view"]
            if d["done"]:
                break
            c.post("/api/onboarding/diagnostic/next")
        check(c.get("/api/onboarding/state").json()["stage"] == "done", "diagnostic done")
        comp0 = {d["domain"]: d for d in c.get("/api/competency").json()["domains"]}
        t0 = comp0["matching"]["tier"]
        check(comp0["matching"]["exam"]["state"] == "locked", f"matching starts at T{t0} with the exam locked")

        say("2. plan + REAL build")
        opts = c.post("/api/planner/options").json()
        check(len(opts["options"]) == 2 and all(o["kind"] == "challenge" for o in opts["options"]), "Planner offers 2 challenges (no exam yet)")
        b = c.post("/api/build", json={"option_index": 0}).json()
        nid, ndir = b["node_id"], b["node_dir"]
        j = poll_job(c, b["job_id"], f"build {ndir}")
        check(j["status"] == "done", "real Builder released the node through the gate")
        if j["status"] != "done":
            res["build_error"] = j.get("error")
            return res
        node = DataLayout(root, TRACK).nodes_dir / ndir
        fm = yaml.safe_load((node / "node.md").read_text(encoding="utf-8").split("---")[1])
        res["node"] = {k: fm.get(k) for k in ("id", "title", "kind", "domain", "subskills", "tier", "sim_runtime_s")}
        check(fm["kind"] == "challenge" and fm["track"] == TRACK, "node.md is a challenge of the smoke track")

        say("3. prelab -> lab -> proof")
        pkey = yaml.safe_load((node / "_hidden" / "prelab_key.yaml").read_text(encoding="utf-8"))["items"]
        answers = {it["id"]: (float(it["value"]) if it["kind"] == "numeric" else "I reason from the physics of the circuit.") for it in pkey}
        r = c.post(f"/api/nodes/{nid}/prelab/submit", json={"answers": answers})
        check(r.status_code == 200 and r.json()["status"] == "lab", "prelab submitted, lab unlocked")
        r = c.post(f"/api/nodes/{nid}/run")
        if r.status_code == 202:
            jr = poll_job(c, r.json()["job_id"], "lab: run simulation (starter)", every=5)
            check(jr["status"] in ("done", "failed"), "lab: Run simulation job completed in the learner container")
        ref = node / "_hidden" / "reference_solution"
        for p in ref.rglob("*"):
            if p.is_file():
                dst = node / "workspace" / p.relative_to(ref)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(p.read_bytes())
        r = c.post(f"/api/nodes/{nid}/proof/submit")
        check(r.status_code == 202, "proof submitted")
        jp = poll_job(c, r.json()["job_id"], "proof check", every=5)
        st = c.get(f"/api/nodes/{nid}").json()["status"]
        check(jp["status"] == "done" and st == "oral", f"proof passed (real checker), node at stage {st}")
        if st != "oral":
            return res

        say("4. oral (text) -> debrief")
        key = yaml.safe_load((node / "_hidden" / "oral_key.yaml").read_text(encoding="utf-8"))["items"]
        answer_all(c, nid, key)
        d = c.post(f"/api/nodes/{nid}/oral/finish").json()
        check(d["oral"]["passed"] is True and d["status"] == "debrief", f"oral graded and passed (mean {d['oral']['mean']}), node in debrief")
        check(c.post(f"/api/nodes/{nid}/debrief/complete").json()["status"] == "done", "debrief completed, node done")

        say("5. tier-exam unlock from seeded events")
        comp = {x["domain"]: x for x in c.get("/api/competency").json()["domains"]}["matching"]
        tier = comp["tier"]
        before_mastery = comp["mastery"]
        subs = [s["name"] for s in comp["subskills"]][:2] + ["matching.bandwidth"]
        for_seed = dict.fromkeys(subs)
        seed_events(root, TRACK, "matching", tier, list(for_seed))
        from app.datastore.cache import rebuild_db

        rebuild_db(root)
        comp = {x["domain"]: x for x in c.get("/api/competency").json()["domains"]}["matching"]
        res["unlock"] = {"mastery_before_seed": before_mastery, "mastery": comp["mastery"], "evidenced": comp["unlock"]["subskills_evidenced"], "state": comp["exam"]["state"]}
        check(comp["exam"]["state"] == "available" and comp["mastery"] >= 65 and comp["unlock"]["subskills_evidenced"] >= 3, f"exam unlocked (mastery {comp['mastery']}%, {comp['unlock']['subskills_evidenced']} subskills)")
        po = c.post("/api/planner/options").json()
        check(po["options"][-1]["kind"] == "tier_exam" and po["options"][-1]["domain"] == "matching", "Planner offers the tier exam")

        say("6. exam -> pass -> tier-up")
        x = c.post("/api/competency/matching/exam")
        check(x.status_code == 200 and not x.json()["reused"], "exam node created")
        eid = x.json()["node_id"]
        en = c.get(f"/api/nodes/{eid}").json()
        check(en["kind"] == "tier_exam" and en["status"] == "oral", "exam node is a tier_exam at stage oral")
        ekey = yaml.safe_load((DataLayout(root, TRACK).nodes_dir / x.json()["dir"] / "_hidden" / "oral_key.yaml").read_text(encoding="utf-8"))["items"]
        check(len(ekey) == 6, "exam key has 6 questions")
        answer_all(c, eid, ekey)
        d = c.post(f"/api/nodes/{eid}/oral/finish").json()
        ex = d["oral"].get("exam") or {}
        res["exam"] = {"passed": d["oral"]["passed"], "mean": d["oral"]["mean"], **ex}
        check(d["oral"]["passed"] is True and ex.get("tier_up") is True, f"exam passed (mean {d['oral']['mean']}), tier up T{ex.get('tier_before')} -> T{ex.get('tier_after')}")
        c.post(f"/api/nodes/{eid}/debrief/complete")
        after = {x["domain"]: x for x in c.get("/api/competency").json()["domains"]}["matching"]
        check(after["tier"] == tier + 1 and after["mastery"] == 0 and after["exam"]["state"] == "locked", f"competency: matching T{tier + 1}, mastery fresh ({after['mastery']}%), exam locked again")
        check(not [m for m in fold(read_records(DataLayout(root).misconceptions_path, TRACK)) if m["node_id"] == eid], "no misconceptions from the passed exam")
        res["smoke_calls"] = {t: smoke.calls.count(t) for t in sorted(set(smoke.calls))}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from app.voice.models import models_dir

    os.environ.setdefault("BLAHAJ_MODELS_DIR", str(models_dir()))
    with temp_data() as root:
        say(f"temp data copy {root}")
        try:
            res = run(root)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            res = {"exception": f"{e.__class__.__name__}: {e}"}
    res["checks"], res["ok"] = CHECKS, bool(CHECKS) and all(CHECKS.values()) and "exception" not in res
    out = Path(a.out) if a.out else Path(os.environ.get("TEMP", ".")) / "m10_smoke.json"
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    say(f"{'OK' if res['ok'] else 'FAILED'}; results: {out}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
