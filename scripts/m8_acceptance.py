"""M8 acceptance (SPEC §14), backend part, on a TEMPORARY COPY of data/ with the mock LLM and a fake checker. The real data/ is never touched.

    uv run python -m scripts.m8_acceptance

SPEC §14 M8: "Oral fail spawns remediation child; misconception reappears in a later node and resolves; graph renders" (graph UI excluded here).
Track rfic-default of the copy, real node 0004 (measurement, 2 core + 1 interleaved oral items):
  1. Planner: full input set; difficulty control (diagnostic -> raise -> lower) enforced by code on the options;
  2. a failed oral on 0004 logs misconceptions and spawns a kind=remediation child (the Builder is stubbed here: the live build is
     scripts/m8_remediation_live.py); the child is linked to its parent;
  3. the open misconception becomes a gate-4 interleaving target of the next build (a key without an interleaved item on it is rejected, one with it passes);
  4. the child's oral revisits it through its interleaved item (planned first), and a score >= 2 resolves it (an interleaved event, 1.5x weight);
  5. SUMMARY.md after each Debrief (<= 1,500 tokens); outcomes feed the success rate;
  6. graph API: nodes + parent edges + status/domain/tier of the active track only;
  7. POST /api/rfic/run is dev-only.
"""

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

from scripts.tempdata import temp_data

REAL = Path(__file__).resolve().parent.parent / "data"
A = "rfic-default"
FAILS: list[str] = []
LONG = ("because the residual after open-short subtraction comes from the interaction between the shunt and series parasitics, which the model assumes "
        "are separable, and that error grows with frequency so it matters at sixty gigahertz")


def check(ok: bool, what: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {what}", flush=True)
    if not ok:
        FAILS.append(what)


def tree(path: Path) -> dict[str, str]:
    path = Path(path)
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.rglob("*")) if p.is_file()} if path.exists() else {}


def real_fingerprint() -> dict:
    names = ("events.jsonl", "misconceptions.jsonl", "tracks.yaml")
    return {n: (hashlib.sha256((REAL / n).read_bytes()).hexdigest() if (REAL / n).exists() else None) for n in names} | {"tracks": tree(REAL / "tracks")}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    before = real_fingerprint()
    with temp_data() as root:
        print(f"temp data copy: {root}")
        run(root)
    ok = before == real_fingerprint()
    print("\nreal data/ untouched:", "PASS" if ok else "FAIL")
    if not ok:
        FAILS.append("the real data/ changed")
    print("\n" + ("ACCEPTANCE FAILED:\n  - " + "\n  - ".join(FAILS) if FAILS else "ACCEPTANCE PASSED"))
    return 1 if FAILS else 0


def run(root: Path) -> None:
    from fastapi.testclient import TestClient

    from app.builder.context import interleaving_context
    from app.builder.gate import _interleaving
    from app.datastore.cache import DataStore, rebuild_db
    from app.datastore.layout import DataLayout
    from app.datastore.misconceptions import fold, read_records
    from app.lifecycle.outcomes import read_outcomes
    from app.lifecycle.summary import TOKEN_CAP, estimate_tokens
    from app.llm.adapters.mock import MockAdapter
    from app.llm.config import load_routing
    from app.llm.router import Router
    from app.llm.store import Store
    from app.main import create_app
    from app.planner.difficulty import read_difficulty
    from app.planner.service import Planner, PlannerError
    from app.tracks.registry import TrackRegistry
    from tests.test_lifecycle import make
    from tests.test_oral import grades
    from tests.test_onboarding import Scripted

    lay = DataLayout(root, A)
    tmp = Path(tempfile.mkdtemp(prefix="m8-acc-"))
    N4 = "0004-de-embed-a-simulated-sg13g2-hbt-from-a"
    expl = {"items": [{"id": "p3", "score": 2, "quote": "I know", "rationale": "ok"}, {"id": "p4", "score": 2, "quote": "I know", "rationale": "ok"}], "misconceptions": []}
    rem_opt = {"title": "Quantify the open-short residual at 60 GHz", "why_now": "the oral showed a de-embedding gap", "kind": "remediation", "domain": "measurement",
               "subskills": ["measurement.deembedding"], "tier": 2, "size_est_min": 30}  # fmt: skip
    child: dict = {}

    class FakeBuild:
        """Stands in for the Builder: 'releases' node 0007 as a copy of 0004 whose oral key honours gate 4 for the given option."""

        def __init__(self, r, track, router, domain):
            self.lay = DataLayout(r, track)

        def start(self, jobs, option, on_success=None):
            ctx = interleaving_context(self.lay.root, self.lay.track, option)
            child["ctx"] = ctx
            d = self.lay.nodes_dir / "0007-quantify-the-open-short-residual"
            shutil.copytree(self.lay.nodes_dir / N4, d)
            md = (d / "node.md").read_text(encoding="utf-8")
            fm, body = md.split("---\n", 2)[1], md.split("---\n", 2)[2]
            y = yaml.safe_load(fm)
            y.update(id=7, slug=d.name[5:], kind=option["kind"], title=option["title"], subskills=option["subskills"], tier=option["tier"], status="prelab", parents=[])
            (d / "node.md").write_text("---\n" + yaml.safe_dump(y, sort_keys=False) + "---\n" + body, encoding="utf-8")
            key = yaml.safe_load((d / "_hidden" / "oral_key.yaml").read_text(encoding="utf-8"))
            items = key["items"]
            for it in items:  # the Builder's job under gate 4: an interleaved item on a target subskill (here the open misconception)
                it["interleaved"] = it["subskill"] in ctx.targets() and it["id"] == "o1"
            (d / "_hidden" / "oral_key.yaml").write_text(yaml.safe_dump(key, sort_keys=False), encoding="utf-8")
            child["key"], child["dir"] = items, d
            child["release"] = lambda: on_success({"node_dir": d.name})  # the build job finishing, later
            return SimpleNamespace(id="job-r"), SimpleNamespace(name=d.name, node_id=7)

    import app.lifecycle.remediation as rem_mod

    real_build = rem_mod.BuildService
    rem_mod.BuildService = FakeBuild  # type: ignore[assignment]
    try:
        print("1. Planner input + difficulty control")
        inp = Planner(root, A, None, "rfic").planner_input()
        check(set(inp) == {"goals", "summary", "framework", "scores", "open_misconceptions", "recent_nodes", "difficulty", "sizing"}, "input = goals, SUMMARY.md, scores, misconceptions, recent nodes (+ difficulty, sizing)")
        check(inp["difficulty"]["mode"] == "diagnostic" and inp["difficulty"]["rated_nodes"] == 0, f"no challenge verdicts yet -> diagnostic tier at default sizes ({inp['difficulty']['rule'][:50]}...)")
        check(all(n["outcome"] for n in inp["recent_nodes"]), f"{len(inp['recent_nodes'])} recent nodes carry an outcome: {[n['outcome'] for n in inp['recent_nodes']]}")
        base = {"title": "x", "why_now": "w", "kind": "challenge", "domain": "measurement", "subskills": ["measurement.sparams"], "tier": 3, "size_est_min": 30}
        cap = inp["scores"]["measurement"]["tier"]

        def plan_with(opts):
            ad = Scripted({"plan_branches": [{"options": opts}] * 2})
            router = Router(load_routing(), Store(tmp / "p.db"), {k: ad for k in ("claude_code", "cursor_cli", "openrouter", "mock")})
            return Planner(root, A, router, "rfic").generate()

        out = plan_with([{**base, "title": "a", "tier": cap}, {**base, "title": "b", "tier": cap, "subskills": ["measurement.deembedding"]}])
        check(len(out["options"]) == 2 and out["difficulty"]["mode"] == "diagnostic", "options at the current tier accepted in diagnostic mode")
        try:
            plan_with([{**base, "title": "a", "tier": cap - 1}, {**base, "title": "b", "tier": cap - 1}])
            check(False, "an option below the current tier is refused in diagnostic mode")
        except PlannerError as e:
            check(e.status == 502 and "tier must be" in e.message, "an option below the current tier is refused in diagnostic mode")
        # seed 4 successes (real node files untouched; synthetic done nodes with session files) -> raise
        for i in range(101, 105):
            d = lay.node_dir(i, f"syn{i}")
            d.mkdir(parents=True)
            (d / "node.md").write_text("---\n" + yaml.safe_dump({"id": i, "slug": f"syn{i}", "title": f"syn {i}", "kind": "challenge", "domain": "measurement", "subskills": ["measurement.sparams"], "tier": 3, "status": "done", "parents": []}) + "---\nb\n")
            s = lay.sessions_dir / d.name
            s.mkdir(parents=True)
            (s / "proof.json").write_text(json.dumps({"submissions": [{"n": 1, "verdict": "pass"}]}))
            (s / "oral.json").write_text(json.dumps({"grading": {"status": "done", "passed": True}}))
        rebuild_db(root)
        d = read_difficulty(root, A)
        check(d.mode == "raise" and d.rate == 1.0, f"4/4 successes -> raise (rate {d.rate})")
        try:
            plan_with([{**base, "title": "a", "tier": cap}, {**base, "title": "b", "tier": cap}])
            check(False, "raise needs an option integrating 2+ subskills")
        except PlannerError as e:
            check("integrate 2 or more subskills" in e.message, "raise: code demands an integrative option")
        out = plan_with([{**base, "title": "a", "tier": cap, "subskills": ["measurement.sparams", "measurement.deembedding"]}, {**base, "title": "b", "tier": cap}])
        check(out["difficulty"]["mode"] == "raise", "raise: valid options accepted")
        for i in range(101, 105):  # now 4 failures after the successes -> last 8 = 4 ok + 4 failed = 0.5 -> lower
            s = lay.sessions_dir / lay.node_dir(i, f"syn{i}").name
            if i > 102:
                (s / "oral.json").write_text(json.dumps({"grading": {"status": "done", "passed": False}}))
        rebuild_db(root)
        d = read_difficulty(root, A)
        check(d.mode == "lower" and (d.n, d.successes) == (4, 2), f"2/4 -> lower (rate {d.rate})")
        try:
            plan_with([{**base, "title": "a", "tier": cap}, {**base, "title": "b", "tier": cap}])
            check(False, "lower asks for a tier below")
        except PlannerError as e:
            check("difficulty mode is lower" in e.message, "lower: code demands a tier down and short builds")
        for i in range(101, 105):  # remove the synthetic nodes again: the rest of the run must see the real history
            shutil.rmtree(lay.node_dir(i, f"syn{i}"))
            shutil.rmtree(lay.sessions_dir / lay.node_dir(i, f"syn{i}").name)
        rebuild_db(root)

        print("2. failed oral -> misconceptions + remediation child")
        key4 = lay.nodes_dir / N4
        ans = {}
        pk = yaml.safe_load((key4 / "_hidden" / "prelab_key.yaml").read_text(encoding="utf-8"))["items"]
        for it in pk:
            ans[it["id"]] = float(it["value"]) if it["kind"] == "numeric" else "I know the open short model"
        (key4 / "prelab_answers.yaml").write_text(yaml.safe_dump({"submitted": "2026-11-11T00:00:00+00:00", "answers": ans, "units": {}}), encoding="utf-8")
        lc0, _, _ = make(root, tmp / "a", {}, track=A)
        lc0.store.set_scalar(lc0.store.find(4), "status", "oral")
        rebuild_db(root)
        g_fail = grades({"o1": 0, "o2": 3, "o3": 3}, quote="residual", mis=[{"text": "the open-short model removes all pad parasitics", "subskill": "measurement.deembedding"}])
        lc, ad, _ = make(root, tmp / "b", {"grade_prelab_explain": [expl], "grade_oral": [g_fail], "plan_branches": [{"options": [rem_opt]}]}, track=A)
        lc.oral.remediator.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _i: SimpleNamespace(status="done", error=None)))
        ref = lc.store.find(4)
        lc.oral.start(ref, "text", False)
        while lc.oral.view(ref)["status"] == "active":
            lc.oral.answer(ref, "the residual " + LONG)
        o = lc.oral_finish(4)["oral"]
        check(o["passed"] is False and o["items"][0]["score"] == 0, "oral failed (o1 scored 0)")
        check((o["remediation"] or {}).get("status") == "building" and o["remediation"]["node_dir"].startswith("0007-"), f"remediation child spawned and building: {o['remediation'] and o['remediation'].get('node_dir')}")
        child["release"]()  # the build job finishes: the child is released and linked
        check(lc.oral.debrief_view(ref)["remediation"]["status"] == "ready", "the parent's remediation record flips to ready")
        kid = lc.store.frontmatter(lc.store.find(7))
        check(kid["kind"] == "remediation" and kid["parents"] == [4], "child is kind=remediation with parents=[4]")
        mis = [m for m in fold(read_records(lay.misconceptions_path, A)) if not m["resolved"]]
        check(any(m["subskill"] == "measurement.deembedding" for m in mis), f"misconception open: {[m['text'][:50] for m in mis]}")
        lc.debrief_complete(4)
        check(read_outcomes(root, A)[4].label == "oral_failed" and read_outcomes(root, A)[7].kind == "remediation", "outcome of 0004 = oral_failed; the remediation child never counts toward the rate")
        text = (lay.summary_path).read_text(encoding="utf-8")
        check("## 0004" in text and estimate_tokens(text) <= TOKEN_CAP, f"SUMMARY.md regenerated after the Debrief ({estimate_tokens(text)} of {TOKEN_CAP} tokens)")

        print("3. the misconception is a gate-4 target of the next build")
        ctx = child["ctx"]
        check(ctx.open_misconceptions[:1] == [{"text": "the open-short model removes all pad parasitics", "subskill": "measurement.deembedding"}], "interleaving context lists it first (target for the NEXT build)")
        errs: list[str] = []
        _interleaving({"oral": [{**it, "interleaved": False} for it in child["key"]], "prelab": []}, ctx, errs)
        check(bool(errs) or "measurement.deembedding" not in ctx.targets(), "a key with no interleaved item on it fails gate 4" if errs else "(target not applicable)")
        errs = []
        _interleaving({"oral": child["key"], "prelab": []}, ctx, errs)
        check(errs == [], "the released child's key (interleaved item o1 on it) passes gate 4")

        print("4. later node: the oral revisits it and it resolves")
        kdir = child["dir"]
        for it in yaml.safe_load((kdir / "_hidden" / "prelab_key.yaml").read_text(encoding="utf-8"))["items"]:
            ans[it["id"]] = ans.get(it["id"])
        (kdir / "prelab_answers.yaml").write_text(yaml.safe_dump({"submitted": "2026-11-11T00:00:00+00:00", "answers": ans, "units": {}}), encoding="utf-8")
        sess = lay.sessions_dir / kdir.name
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "proof.json").write_text(json.dumps({"submissions": [{"n": 1, "verdict": "pass"}]}))
        lc2, _, _ = make(root, tmp / "c", {"grade_prelab_explain": [expl], "grade_oral": [grades({"o2": 3, "o3": 3, "o1": 3}, quote="residual")]}, track=A)
        r7 = lc2.store.find(7)
        lc2.store.set_scalar(r7, "status", "oral")
        rebuild_db(root)
        plan = lc2.oral.plan(r7)
        check(plan["items"][-1] == "o1" and "revisits the open misconception on measurement.deembedding" in plan["reason"], f"oral plan puts the interleaved item last: {plan['items']}")
        lc2.oral.start(r7, "text", False)
        while lc2.oral.view(r7)["status"] == "active":
            lc2.oral.answer(r7, "the residual " + LONG)
        d7 = lc2.oral_finish(7)
        ev = [e for e in DataStore(root, A).events() if e.node_id == 7 and e.type == "oral_item" and e.interleaved]
        check(len(ev) == 1 and ev[0].subskill == "measurement.deembedding" and ev[0].value >= 2 / 3 - 1e-9, "an interleaved oral_item event (score >= 2) was written")
        m = next(x for x in fold(read_records(lay.misconceptions_path, A)) if x["subskill"] == "measurement.deembedding")
        check(m["resolved"] and m["resolved_by_node"] == 7, "the misconception is resolved by node 7")
        check([x["subskill"] for x in d7["misconceptions"]["resolved"]] == ["measurement.deembedding"], "the Debrief of node 7 lists it under resolved")
        lc2.debrief_complete(7)
        text = lay.summary_path.read_text(encoding="utf-8")
        check("## 0007" in text and "## 0004" in text and estimate_tokens(text) <= TOKEN_CAP, "SUMMARY.md now has both entries, within the cap")

        print("5. graph API + dev-only route")
        reg = TrackRegistry(root)
        other = [t.id for t in reg.list() if t.id != A and t.status != "archived"]
        app = create_app()
        app.state.llm_router = Router(load_routing(), Store(root / "llm.db"), {n: MockAdapter() for n in ("claude_code", "cursor_cli", "openrouter", "mock")})
        with TestClient(app) as c:
            was = reg.active_id()
            reg.activate(A)
            g = c.get("/api/graph").json()
            ids = {n["id"]: n for n in g["nodes"]}
            check(g["track"] == A and {"source": 4, "target": 7} in g["edges"], f"graph of {A}: {len(g['nodes'])} nodes, edge 4 -> 7")
            check(ids[4]["status"] == "done" and ids[4]["outcome"] == "oral_failed" and ids[4]["domain"] == "measurement" and ids[4]["tier"] == 3, "node 4: status done, outcome oral_failed, domain measurement, tier 3")
            check(ids[7]["kind"] == "remediation" and ids[7]["parents"] == [4], "node 7: kind remediation, parents [4]")
            if other:
                reg.activate(other[0])
                g2 = c.get("/api/graph").json()
                check(g2["track"] == other[0] and {"source": 4, "target": 7} not in g2["edges"], f"graph of {other[0]} is that track's own ({len(g2['nodes'])} nodes)")
            reg.activate(was)
            check(c.post("/api/rfic/run", json={"tool": "ngspice", "node": "x", "path": "y"}).status_code == 404 and "/api/rfic/run" not in c.get("/openapi.json").json()["paths"],
                  "POST /api/rfic/run: 404 without BLAHAJ_DEV_ROUTES=1 and not in the OpenAPI schema")
    finally:
        rem_mod.BuildService = real_build  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
