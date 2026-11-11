"""M7.5 acceptance (SPEC §14): tracks + migration, on a TEMPORARY COPY of data/ with the mock LLM. The real data/ is never touched.

    uv run python -m scripts.m75_acceptance

Checks, in order (each prints PASS/FAIL; exit code 1 if any fails):
  1. migration: the copy of the real pre-tracks data/ migrates to track rfic-default and the scores are identical before and after
     (independently: the engine over the events, and the old cache.db);
  2. a second track is created and onboarded (mock interview -> framework -> diagnostic);
  3. switching A <-> B changes /api/nodes, /api/scores, the Planner input and the Builder context, and leaves the other track byte-identical;
  4. the M7 oral flow on B (events, misconceptions, remediation child) lands only in B;
  5. archive / unarchive / reset / delete / restore / purge, and data/models (shared) never moves.
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from scripts.tempdata import temp_data

REAL = Path(__file__).resolve().parent.parent / "data"
SKIP = ("_spike_archive", "spike", "library", "models", "*.db-wal", "*.db-shm")  # keeps .git and builds/: the migration meets them
FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {what}")
    if not ok:
        FAILS.append(what)


def tree(path: Path) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {}
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.rglob("*")) if p.is_file()}


def real_fingerprint() -> dict:
    """The real data dir as it is now: what must not change during this run."""
    names = ("events.jsonl", "misconceptions.jsonl", "tracks.yaml", "cache.db")
    return {n: (hashlib.sha256((REAL / n).read_bytes()).hexdigest() if (REAL / n).exists() else None) for n in names} | {
        "archive": tree(REAL / "archive"), "profile": tree(REAL / "profile"), "framework": tree(REAL / "framework")}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    before_real = real_fingerprint()
    with temp_data(SKIP) as root:
        run(root)
    after_real = real_fingerprint()
    print("\nreal data/ untouched:", "PASS" if before_real == after_real else "FAIL")
    if before_real != after_real:
        FAILS.append("the real data/ changed")
    print("\n" + ("ACCEPTANCE FAILED:\n  - " + "\n  - ".join(FAILS) if FAILS else "ACCEPTANCE PASSED"))
    return 1 if FAILS else 0


def run(root: Path) -> None:
    import sqlite3
    from contextlib import closing

    from fastapi.testclient import TestClient

    from app.builder.context import domain_scores, interleaving_context, learner_state
    from app.competency.engine import score_all
    from app.datastore.cache import connect
    from app.datastore.events import read_events
    from app.datastore.layout import DataLayout
    from app.datastore.misconceptions import fold, read_records
    from app.llm.adapters.mock import MockAdapter
    from app.llm.config import load_routing
    from app.llm.router import Router
    from app.llm.store import Store
    from app.main import create_app
    from app.planner.service import Planner
    from app.tracks.migrate import migrate_to_tracks, needs_migration
    from app.tracks.registry import TrackRegistry
    from app.tracks.service import TrackManager

    A, B = "rfic-default", "rfic-mmwave"
    models = root / "models" / "whisper"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sentinel.bin").write_bytes(b"\x07" * 256)  # stands in for the 520 MB voice models: skipped by the copy
    models_before = tree(root / "models")

    print("1. migration of a copy of the real data/")
    check(needs_migration(root), f"the copy is a pre-tracks data dir ({len(list((root / 'archive' / 'nodes').iterdir()))} node dirs)")
    events_raw = [json.loads(l) for l in (root / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    from app.competency.engine import Event

    before = score_all([Event.from_dict({**e, "track": A}) for e in events_raw])
    with closing(sqlite3.connect(root / "cache.db")) as c:
        old_rows = {r[0]: r for r in c.execute("SELECT domain, tier, mastery FROM scores")}
    rep = migrate_to_tracks(root)
    check(rep.migrated, f"migrated: {len(rep.moved)} items moved, {rep.events_stamped} events and {rep.nodes_stamped} node.md stamped")
    with closing(connect(root / "cache.db")) as c:
        after = {r["domain"]: (r["tier"], r["mastery"]) for r in c.execute("SELECT * FROM scores WHERE track = ?", (A,))}
    check(after == {d: (s.tier, s.mastery) for d, s in before.items()} and len(after) > 0, f"scores identical to the engine over the old events ({len(after)} domains)")
    check(rep.legacy_cache_matches is True and {d: (r[1], r[2]) for d, r in old_rows.items()} == after, "scores identical to the old cache.db")
    check(tree(root / "models") == models_before, "data/models untouched by the migration")
    check(not (root / "archive").exists() and (root / "tracks" / A / "archive" / "nodes").is_dir(), "old layout moved under tracks/rfic-default")
    a_tree = tree(DataLayout(root, A).track_dir)
    a_events = [e for e in read_events(root / "events.jsonl")[0] if e.track == A]

    print("2. second track + onboarding (mock LLM)")
    app = create_app()
    app.state.llm_router = Router(load_routing(), Store(root / "llm.db"), {n: MockAdapter() for n in ("claude_code", "cursor_cli", "openrouter", "mock")})
    with TestClient(app) as c:
        v = c.get("/api/tracks").json()
        check(v["active"] == A and [t["id"] for t in v["tracks"]] == [A], "GET /api/tracks: rfic-default is the active track")
        nodes_a = c.get("/api/nodes").json()
        scores_a = c.get("/api/scores").json()
        r = c.post("/api/tracks", json={"name": "RFIC mmWave", "domain": "rfic"})
        check(r.status_code == 201 and r.json()["active"] == B, "created RFIC mmWave; it is now active")
        check(c.get("/api/nodes").json() == [] and c.get("/api/scores").json() == [], "Home data of the new track is empty")
        c.post("/api/onboarding/interview/turn", json={})
        check(c.get("/api/onboarding/state").json()["stage"] == "framework", "interview wrote goals.md in the new track")
        c.post("/api/onboarding/framework/draft")
        c.post("/api/onboarding/framework/approve", json={})
        c.post("/api/onboarding/diagnostic/next")
        for _ in range(40):
            d = c.post("/api/onboarding/diagnostic/answer", json={"answer": "loaded Q sets the bandwidth"}).json()["view"]
            if d["done"]:
                break
            d = c.post("/api/onboarding/diagnostic/next").json()
        check(c.get("/api/onboarding/state").json()["stage"] == "done" and bool(d["done"]), "framework frozen and diagnostic finished in track B")
        check(tree(DataLayout(root, A).track_dir) == a_tree, "track A byte-identical after onboarding B")
        check([e for e in read_events(root / "events.jsonl")[0] if e.track == A] == a_events, "A's events unchanged in the shared log")

        print("3. switching changes what every screen reads")
        nodes_b, scores_b = c.get("/api/nodes").json(), c.get("/api/scores").json()
        check(nodes_b != nodes_a and [n["id"] for n in nodes_b] == [] , f"/api/nodes: B {len(nodes_b)} nodes vs A {len(nodes_a)}")
        check(scores_b != scores_a and bool(scores_b), "/api/scores differ between the tracks")
        goals_b = DataLayout(root, B).goals_path.read_text(encoding="utf-8")
        goals_a = DataLayout(root, A).goals_path.read_text(encoding="utf-8")
        pin_a, pin_b = Planner(root, A, None, "rfic").planner_input(), Planner(root, B, None, "rfic").planner_input()
        check(pin_a["goals"] == goals_a and pin_b["goals"] == goals_b and pin_a["scores"] != pin_b["scores"] and pin_a["recent_nodes"] != pin_b["recent_nodes"],
              "Planner input: goals, scores and recent nodes come from the track's own data")
        check(learner_state(root, A)["scores"] == domain_scores(root, A) != domain_scores(root, B), "Builder learner state is scoped")
        ev_subs = {t: {e.subskill for e in read_events(root / "events.jsonl", t)[0]} for t in (A, B)}
        stale = {t: set(interleaving_context(root, t, {"subskills": []}).stale_subskills) for t in (A, B)}
        check(all(stale[t] <= ev_subs[t] for t in (A, B)), "Builder interleaving targets come only from the track's own events")
        check(c.post(f"/api/tracks/{A}/activate").json()["active"] == A, "switch back to rfic-default")
        check(c.get("/api/nodes").json() == nodes_a and c.get("/api/scores").json() == scores_a, "A's Home data is exactly what it was (nothing from B)")
        check(tree(DataLayout(root, A).track_dir) == a_tree, "track A still byte-identical after the switches")
        b_tree = tree(DataLayout(root, B).track_dir)
        c.post(f"/api/tracks/{B}/activate")

        print("4. M7 oral flow on track B")
        _oral_on_b(root, B, A, a_tree, a_events)
        c.post(f"/api/tracks/{A}/activate")

        print("5. archive / unarchive / reset / delete / restore / purge")
        v = c.post(f"/api/tracks/{B}/archive").json()
        check({t["id"]: t["status"] for t in v["tracks"]}[B] == "archived", "archive")
        check(c.post(f"/api/tracks/{B}/activate").status_code == 409, "an archived track cannot be entered")
        v = c.post(f"/api/tracks/{B}/unarchive").json()
        check({t["id"]: t["status"] for t in v["tracks"]}[B] == "paused", "unarchive")
        a_events_now = [e for e in read_events(root / "events.jsonl")[0] if e.track == A]
        v = c.post(f"/api/tracks/{B}/reset", json={"keep_goals": True}).json()
        check(v["trash"] and v["trash"][0]["kind"] == "reset" and not list(DataLayout(root, B).nodes_dir.iterdir()), "reset B: data in the trash, track empty")
        check(DataLayout(root, B).goals_path.is_file(), "reset kept the goals")
        check(not any(e.track == B for e in read_events(root / "events.jsonl")[0]) and [e for e in read_events(root / "events.jsonl")[0] if e.track == A] == a_events_now,
              "B's event/misconception lines left the logs; A's stayed")
        check(not fold(read_records(root / "misconceptions.jsonl", B)), "B's misconceptions are in the trash")
        check(c.request("DELETE", f"/api/tracks/{B}", json={"confirm_name": "wrong"}).status_code == 422, "delete refuses a wrong typed name")
        v = c.request("DELETE", f"/api/tracks/{B}", json={"confirm_name": "RFIC mmWave"}).json()
        entry = next(t["entry"] for t in v["trash"] if t["kind"] == "deleted")
        check(not DataLayout(root, B).track_dir.exists() and [t["id"] for t in v["tracks"]] == [A], "delete B (typed name): moved to the trash")
        v = c.post(f"/api/tracks/trash/{entry}/restore").json()
        check([t["id"] for t in v["tracks"]] == [A, B] and DataLayout(root, B).goals_path.is_file(), "restore B from the trash")
        check(c.request("DELETE", "/api/tracks/trash", json={"confirm": "no"}).status_code == 422, "purge needs the confirmation word")
        purged = c.request("DELETE", "/api/tracks/trash", json={"confirm": "PURGE"}).json()["purged"]
        check(len(purged) >= 1 and c.get("/api/tracks").json()["trash"] == [], f"purge removed {len(purged)} trash entries (and nothing else)")
        check(tree(root / "models") == models_before, "data/models untouched by archive/reset/delete/restore/purge")
        check(tree(DataLayout(root, A).track_dir) == a_tree, "track A still byte-identical at the end")
        check(TrackRegistry(root).active_id() == A and TrackManager(root).trash() == [], "registry consistent")
        _ = b_tree


def _oral_on_b(root: Path, B: str, A: str, a_tree: dict, a_events: list) -> None:
    """Node + oral exam of track B through the real Lifecycle (mock grader/examiner, fake checker), failing so that misconceptions and a
    remediation child are produced."""
    import yaml

    from app.datastore.events import read_events
    from app.datastore.layout import DataLayout
    from app.datastore.misconceptions import fold, read_records
    from tests.test_lifecycle import GRADES, make, seed_node
    from tests.test_oral import CLEAN, grades, prepare_oral, run_exam, to_oral

    seed_node(root, B)
    prepare_oral(root, B)  # B already has a framework from the mock onboarding; prepare_oral freezes the template one only if absent

    class FakeBuild:
        def __init__(self, r, track, router, domain):
            self.layout = DataLayout(r, track)

        def start(self, jobs, option, on_success=None):
            d = self.layout.nodes_dir / "0902-remediate"
            d.mkdir(parents=True, exist_ok=True)
            fm = {"id": 902, "slug": "remediate", "kind": option["kind"], "domain": option["domain"], "subskills": option["subskills"], "tier": 1, "status": "prelab"}
            (d / "node.md").write_text("---\n" + yaml.safe_dump(fm) + "---\nb\n")
            on_success({"node_dir": d.name})
            return SimpleNamespace(id="job-r"), SimpleNamespace(name=d.name, node_id=902)

    import app.lifecycle.oral as oral_mod

    real_build = oral_mod.BuildService
    oral_mod.BuildService = FakeBuild  # type: ignore[assignment]
    try:
        rem = {"title": "Probe the L-match Q", "why_now": "gap", "kind": "remediation", "domain": "matching", "subskills": ["matching.l_network"], "tier": 1, "size_est_min": 30}
        g = grades({"o1": 0, "o2": 3, "o7": 3}, mis=[{"text": "L-match always raises Q", "subskill": "matching.l_network"}])
        tmp = Path(tempfile.mkdtemp(prefix="m75-oral-"))
        lc, _, _ = make(root, tmp, {"grade_prelab_explain": [GRADES], "grade_oral": [g], "plan_branches": [{"options": [rem]}]}, track=B)
        lc.oral.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _id: SimpleNamespace(status="done", error=None)))
        ref = to_oral(lc, CLEAN)
        lc.oral.start(ref, "text", False)
        run_exam(lc, ref)
        out = lc.oral_finish(901)["oral"]
    finally:
        oral_mod.BuildService = real_build  # type: ignore[assignment]
    evs, _ = read_events(DataLayout(root).events_path)
    mine = [e for e in evs if e.node_id == 901 and e.type == "oral_item"]
    check(len(mine) == 3 and {e.track for e in mine} == {B}, "oral_item events are tagged with track B")
    check([e for e in evs if e.track == A] == a_events, "no event of A added, removed or changed")
    check(any("L-match always raises Q" in m["text"] for m in fold(read_records(DataLayout(root).misconceptions_path, B))), "misconceptions written to B")
    check(not any(m["text"] == "L-match always raises Q" for m in fold(read_records(DataLayout(root).misconceptions_path, A))), "...and not to A")
    check(out["remediation"]["node_dir"] == "0902-remediate" and (DataLayout(root, B).nodes_dir / "0902-remediate" / "node.md").is_file()
          and not (DataLayout(root, A).nodes_dir / "0902-remediate").exists(), "remediation child built in track B only")
    check(tree(DataLayout(root, A).track_dir) == a_tree, "track A byte-identical after the oral flow on B")


if __name__ == "__main__":
    raise SystemExit(main())
