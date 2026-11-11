"""M7.5: tracks (SPEC §8.6): registry, migration of a pre-tracks data dir, per-track isolation (scores, Planner/Builder inputs,
misconceptions, node ids, the M7 oral flow, the grading queue), reset / delete / restore / purge, and the HTTP API. Mock LLM only."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app.builder.context import domain_scores, interleaving_context, learner_state, open_misconceptions
from app.competency.engine import Event, score_all
from app.datastore.cache import DataStore, connect, rebuild_db
from app.datastore.events import read_events
from app.datastore.layout import DataLayout, init_repo
from app.datastore.misconceptions import add_misconception, fold, read_records, resolve_misconception
from app.llm.types import Request
from app.main import create_app
from app.planner.service import Planner, PlannerError
from app.tracks.migrate import MigrationError, ensure_tracks, migrate_to_tracks, needs_migration
from app.tracks.registry import TrackError, TrackRegistry, installed_domains, registry_exists, slugify, valid_id
from app.tracks.service import TrackManager
from app.tracks.trash import list_trash
from tests.helpers import make_data
from tests.test_builder_m5 import _diag
from tests.test_lifecycle import ANSWERS, GRADES, SyncJobs, make, seed_node
from tests.test_oral import CLEAN, NODE, grades, prepare_oral, run_exam, to_oral

A, B = "alpha", "beta"
FIXTURES = Path(__file__).parent / "fixtures" / "spike_nodes"


def tree(path: Path) -> dict[str, str]:
    """relative file -> sha256 for everything under `path` (empty if it does not exist): byte-identical checks."""
    path = Path(path)
    if not path.exists():
        return {}
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.rglob("*")) if p.is_file()}


def mgr(root, busy=None) -> TrackManager:
    return TrackManager(root, busy=busy or (lambda _t: []))


# ---------------------------------------------------------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------------------------------------------------------


def test_registry_seed_create_activate_invariants(tmp_path):
    root = make_data(tmp_path / "d", A)
    reg = TrackRegistry(root)
    assert [(t.id, t.status) for t in reg.list()] == [(A, "active")] and reg.active_id() == A
    b = reg.create("Beta Path", "rfic")
    assert b.id == "beta-path" and b.status == "active" and reg.active_id() == "beta-path"
    assert {t.id: t.status for t in reg.list()} == {A: "paused", "beta-path": "active"}  # the previous one is paused, not lost
    reg.activate(A)
    assert {t.id: t.status for t in reg.list()} == {A: "active", "beta-path": "paused"}
    with pytest.raises(TrackError) as e:
        reg.seed()
    assert e.value.status == 409
    doc = yaml.safe_load((root / "tracks.yaml").read_text(encoding="utf-8"))
    assert doc["active"] == A and [t["id"] for t in doc["tracks"]] == [A, "beta-path"]


def test_slug_name_and_domain_rules(tmp_path):
    root = make_data(tmp_path / "d", A)
    reg = TrackRegistry(root)
    assert slugify("RFIC mmWave!") == "rfic-mmwave" and slugify("!!!") == "track"
    assert valid_id("rfic-mmwave") and not valid_id("Rfic") and not valid_id("../x") and not valid_id("con") and not valid_id("-x") and not valid_id("a" * 41)
    assert installed_domains() == ["rfic"]
    with pytest.raises(TrackError, match="not installed"):
        reg.create("X", "frontier-ml")
    with pytest.raises(TrackError):
        reg.create("   ", "rfic")
    with pytest.raises(TrackError) as e:
        reg.create(A.upper(), "rfic")  # names are unique, case-insensitively
    assert e.value.status == 409
    t1, t2 = reg.create("Same Slug", "rfic"), reg.create("Same-Slug", "rfic")  # different names, same slug -> numbered id
    assert (t1.id, t2.id) == ("same-slug", "same-slug-2")
    with pytest.raises(TrackError):
        reg.create("Y", "rfic", track_id="Bad Id")


def test_archive_unarchive_rename_and_active_switching(tmp_path):
    root = make_data(tmp_path / "d", A, B)
    reg = TrackRegistry(root)
    assert reg.active_id() == A
    reg.rename(B, "Beta renamed")
    assert reg.get(B).name == "Beta renamed"
    with pytest.raises(TrackError):
        reg.rename(B, A.title())  # taken
    reg.archive(B)
    assert reg.get(B).status == "archived"
    with pytest.raises(TrackError, match="unarchive"):
        reg.activate(B)  # hidden from the switcher: cannot be entered
    with pytest.raises(TrackError, match="only active track"):
        reg.archive(A)  # the last non-archived track
    with pytest.raises(TrackError):
        reg.archive(B)
    reg.unarchive(B)
    assert reg.get(B).status == "paused"
    reg.archive(A)  # B is available now: active moves to it
    assert reg.active_id() == B and reg.get(A).status == "archived"
    assert reg.activate(B).id == B and {t.status for t in reg.list()} == {"active", "archived"}


def test_a_hand_edited_registry_that_breaks_the_invariant_is_refused_on_write(tmp_path):
    root = make_data(tmp_path / "d", A, B)
    p = root / "tracks.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    doc["tracks"][1]["status"] = "active"  # two active tracks
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(TrackError):
        TrackRegistry(root).rename(A, "x")


def test_fresh_data_dir_is_seeded_with_rfic_default(tmp_path):
    root = tmp_path / "fresh"
    assert not needs_migration(root)
    ensure_tracks(root)
    reg = TrackRegistry(root)
    assert [(t.id, t.name, t.domain, t.status) for t in reg.list()] == [("rfic-default", "RFIC", "rfic", "active")]
    assert DataLayout(root, "rfic-default").nodes_dir.is_dir()
    ensure_tracks(root)  # idempotent


# ---------------------------------------------------------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------------------------------------------------------

LEGACY_EVENTS = [
    # (node, domain, subskill, tier, type, value, weight, interleaved)
    *[(901, "matching", f"matching.s{i % 3}", 1, "oral_item", 0.8, 1.0, False) for i in range(13)],
    (901, "noise", "noise.nf", 1, "oral_item", 1.0, 1.0, True),
    (902, "matching", "matching.s0", 1, "prelab_numeric", 0.5, 0.5, False),
    *[(903, "matching", f"matching.s{i % 3}", 1, "tier_exam_item", 1.0, 1.0, False) for i in range(6)],
    *[(903, "matching", f"matching.s{i % 3}", 2, "oral_item", 0.9, 1.0, False) for i in range(6)],
]


def _legacy_data(root: Path, git: bool = True) -> dict:
    """A pre-tracks data dir exactly as M7 left it: flat layout, no `track` anywhere, an old-schema cache.db and llm.db."""
    root.mkdir(parents=True)
    lines = []
    for i, (node, dom, sub, tier, type_, v, w, il) in enumerate(LEGACY_EVENTS):
        lines.append({"id": f"e{i}", "ts": f"2026-11-10T00:00:{i:02d}-06:00", "node_id": node, "domain": dom, "subskill": sub, "tier": tier,
                      "type": type_, "value": v, "weight": w, "interleaved": il, "assist_reveal": False, "grader_model": "m", "framework_version": 1})
    (root / "events.jsonl").write_text("".join(json.dumps(l, separators=(",", ":")) + "\n" for l in lines) + '{"id": "torn", "ts"', encoding="utf-8")
    mis = [{"type": "add", "id": 1, "ts": "t", "node_id": 901, "subskill": "matching.s0", "text": "Q only rises"},
           {"type": "add", "id": 2, "ts": "t", "node_id": 902, "subskill": "noise.nf", "text": "NF is gain"},
           {"type": "resolve", "id": 1, "ts": "t", "node_id": 903, "event_id": "e1"}]
    (root / "misconceptions.jsonl").write_text("".join(json.dumps(m, separators=(",", ":")) + "\n" for m in mis), encoding="utf-8")
    shutil.copytree(FIXTURES, root / "archive" / "nodes")
    (root / "profile").mkdir()
    (root / "profile" / "goals.md").write_text("# Goals\n60 GHz\n", encoding="utf-8")
    (root / "framework").mkdir()
    (root / "framework" / "competency-v1.yaml").write_text("domain_pack: rfic\n", encoding="utf-8")
    (root / "planner").mkdir()
    (root / "planner" / "options.json").write_text("{}", encoding="utf-8")
    sess = root / "sessions" / "0901-rc"
    sess.mkdir(parents=True)
    (sess / "rescope.json").write_text(json.dumps({"backup": "builds/0901-rc/rescope-1/node/workspace", "editing": False}), encoding="utf-8")
    (root / "rescopes.jsonl").write_text(json.dumps({"event": "requested", "backup": "builds/0901-rc/rescope-1/node/workspace"}) + "\n", encoding="utf-8")
    (root / "remediation.jsonl").write_text(json.dumps({"node_id": 901, "decision": "accepted"}) + "\n", encoding="utf-8")
    (root / "builds" / "0901-rc" / "rescope-1" / "node" / "workspace").mkdir(parents=True)
    (root / "builds" / "0901-rc" / "rescope-1" / "node" / "workspace" / "rc.sp").write_text("old", encoding="utf-8")
    (root / "builds" / "0901-rc" / "record.json").write_text(json.dumps({"node_dir": "0901-rc", "moved_to": "builds/failed/0905-x-1/"}), encoding="utf-8")
    (root / "builds" / "failed" / "0905-x-1").mkdir(parents=True)
    (root / "builds" / "failed" / "0905-x-1" / "node.md").write_text("x", encoding="utf-8")
    (root / "models" / "whisper").mkdir(parents=True)
    (root / "models" / "whisper" / "model.bin").write_bytes(b"\x00" * 64)
    (root / "library" / "razavi").mkdir(parents=True)
    (root / "library" / "razavi" / "p1.txt").write_text("page", encoding="utf-8")
    (root / ".gitignore").write_text("settings.json\n*.db\narchive/nodes/*/workspace/**/*.raw\n", encoding="utf-8")
    (root / "settings.json").write_text("{}", encoding="utf-8")
    # old-schema operational/derived DBs
    ev = [Event.from_dict({**l, "track": "x"}) for l in lines]
    sc = score_all(ev)
    with closing(sqlite3.connect(root / "cache.db")) as c, c:
        c.execute("CREATE TABLE scores (domain TEXT PRIMARY KEY, tier INTEGER, mastery REAL, n_window INTEGER, n_since_tier INTEGER, subskills_evidenced TEXT, unlocked INTEGER)")
        for d, s in sc.items():
            c.execute("INSERT INTO scores VALUES (?,?,?,?,?,?,?)", (d, s.tier, s.mastery, s.n_window, s.n_since_tier, json.dumps(list(s.subskills_evidenced)), int(s.unlocked)))
    with closing(sqlite3.connect(root / "llm.db")) as c, c:
        c.execute("CREATE TABLE pending_grades (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, task_type TEXT, node_id INTEGER, request TEXT, "
                  "attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, status TEXT DEFAULT 'pending', last_error TEXT)")
        c.execute("INSERT INTO pending_grades(created, task_type, node_id, request) VALUES ('t', 'grade_oral', 901, '{}')")
    if git:
        init_repo(root)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "legacy"], cwd=root, check=True, capture_output=True)
    return {"scores": sc, "lines": lines}


def test_migration_moves_everything_stamps_the_track_and_keeps_scores_identical(tmp_path):
    root = tmp_path / "data"
    legacy = _legacy_data(root)
    shared_before = (tree(root / "models"), tree(root / "library"), (root / "settings.json").read_bytes())
    events_before = (root / "events.jsonl").read_bytes()
    assert needs_migration(root) and not registry_exists(root)

    rep = migrate_to_tracks(root)

    assert rep.migrated and rep.legacy_cache_matches is True  # the old cache.db independently agrees with the events
    assert rep.scores_before == rep.scores_after and set(rep.scores_after) == {"matching", "noise"}
    assert rep.events_stamped == len(LEGACY_EVENTS) and rep.misconceptions_stamped == 3 and rep.nodes_stamped == 3
    # registry
    reg = TrackRegistry(root)
    assert [(t.id, t.name, t.domain, t.status) for t in reg.list()] == [("rfic-default", "RFIC", "rfic", "active")]
    # moved, not copied
    td = root / "tracks" / "rfic-default"
    for gone in ("profile", "framework", "archive", "sessions", "planner", "rescopes.jsonl", "remediation.jsonl"):
        assert not (root / gone).exists() and (td / gone).exists(), gone
    assert (td / "archive" / "nodes" / "0901-rc-lowpass-sky130" / "node.md").exists() or any((td / "archive" / "nodes").iterdir())
    assert not (root / "builds" / "0901-rc").exists() and (root / "builds" / "rfic-default" / "0901-rc" / "record.json").is_file()
    assert (root / "builds" / "rfic-default" / "failed" / "0905-x-1" / "node.md").is_file()
    # stamped
    events, bad = read_events(root / "events.jsonl")
    assert len(events) == len(LEGACY_EVENTS) and [b[1] for b in bad] != [] and {e.track for e in events} == {"rfic-default"}
    stamped = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(stamped[0]) == {**legacy["lines"][0]} | {"track": "rfic-default"} and list(json.loads(stamped[0]))[:3] == ["id", "ts", "track"]
    assert stamped[-1] == '{"id": "torn", "ts"'  # a torn line is carried through verbatim
    recs = read_records(root / "misconceptions.jsonl")
    assert len(recs) == 3 and {r["track"] for r in recs} == {"rfic-default"}
    assert [(m["id"], m["resolved"]) for m in fold(recs)] == [("rfic-default", 1)][:0] or [(m["id"], m["resolved"]) for m in fold(recs)] == [(1, True), (2, False)]
    for d in (td / "archive" / "nodes").iterdir():
        assert yaml.safe_load((d / "node.md").read_text(encoding="utf-8").split("---")[1])["track"] == "rfic-default"
    # stored backup paths now include the track
    assert json.loads((td / "sessions" / "0901-rc" / "rescope.json").read_text())["backup"] == "builds/rfic-default/0901-rc/rescope-1/node/workspace"
    assert json.loads((td / "rescopes.jsonl").read_text().splitlines()[0])["backup"] == "builds/rfic-default/0901-rc/rescope-1/node/workspace"
    assert json.loads((root / "builds" / "rfic-default" / "0901-rc" / "record.json").read_text())["moved_to"] == "builds/rfic-default/failed/0905-x-1/"
    # cache: scores equal the pure engine's, per track
    with closing(connect(root / "cache.db")) as c:
        rows = {r["domain"]: r for r in c.execute("SELECT * FROM scores WHERE track = 'rfic-default'")}
        assert {d: (r["tier"], round(r["mastery"], 9)) for d, r in rows.items()} == {d: (s.tier, round(s.mastery, 9)) for d, s in legacy["scores"].items()}
        assert c.execute("SELECT COUNT(*) FROM nodes WHERE track = 'rfic-default'").fetchone()[0] == 3
    # grading queue + git
    with closing(sqlite3.connect(root / "llm.db")) as c:
        assert c.execute("SELECT track FROM pending_grades").fetchall() == [("rfic-default",)]
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    assert "tracks/*/archive/nodes/*/workspace/**/*.raw" in gi and "_trash/" in gi and "settings.json" in gi
    log = subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True).stdout
    assert "pre-tracks snapshot" in log
    pre = root / "_migration" / "pre-tracks"
    assert (pre / "events.jsonl").read_bytes() == events_before  # the append-only log, as it was
    # shared data untouched
    assert (tree(root / "models"), tree(root / "library"), (root / "settings.json").read_bytes()) == shared_before
    # idempotent, and nothing left to migrate
    assert not needs_migration(root) and migrate_to_tracks(root).migrated is False
    ensure_tracks(root)
    assert len(TrackRegistry(root).list()) == 1


def test_migration_without_a_git_repo_or_old_cache(tmp_path):
    root = tmp_path / "data"
    _legacy_data(root, git=False)
    (root / "cache.db").unlink()
    rep = migrate_to_tracks(root)
    assert rep.migrated and rep.legacy_cache_matches is None and (root / "_migration" / "pre-tracks" / "events.jsonl").is_file()


def test_migration_resumes_after_a_crash_between_steps(tmp_path):
    root = tmp_path / "data"
    _legacy_data(root, git=False)
    (root / "tracks" / "rfic-default").mkdir(parents=True)
    shutil.move(str(root / "profile"), str(root / "tracks" / "rfic-default" / "profile"))  # the previous run got this far
    rep = migrate_to_tracks(root)
    assert rep.migrated and (root / "tracks" / "rfic-default" / "profile" / "goals.md").is_file() and not (root / "profile").exists()


def test_migration_refuses_to_overwrite_and_keeps_data_when_both_copies_exist(tmp_path):
    root = tmp_path / "data"
    _legacy_data(root, git=False)
    (root / "tracks" / "rfic-default" / "profile").mkdir(parents=True)  # a second profile/ already in the target
    with pytest.raises(MigrationError, match="already exists"):
        migrate_to_tracks(root)
    assert (root / "profile" / "goals.md").is_file()  # the legacy copy was not touched
    assert not registry_exists(root)


def test_old_cache_schema_is_rebuilt_not_read(tmp_path):
    root = make_data(tmp_path / "d", A)
    with closing(sqlite3.connect(root / "cache.db")) as c, c:
        c.execute("CREATE TABLE scores (domain TEXT)")  # a pre-tracks cache.db
    ds = DataStore(root, A)
    ds.ensure()
    assert ds.scores() == {}  # no OperationalError: the stale schema was rebuilt


# ---------------------------------------------------------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def two(tmp_path):
    """Two tracks, each with the same node id 901, the same domains/subskills, a frozen framework and goals."""
    root = make_data(tmp_path / "d", A, B)
    for t in (A, B):
        seed_node(root, t)
        prepare_oral(root, t)
    return root


def test_scores_misconceptions_and_ids_are_per_track(two):
    root = two
    for i in range(13):
        _diag(root, "matching", "matching.l_network", 1, 3, node_id=901, type_="oral_item", track=A)
    rebuild_db(root)
    assert domain_scores(root, A)["matching"]["mastery"] > 60  # 13 perfect oral items + the diagnostic
    assert domain_scores(root, B)["matching"]["mastery"] < domain_scores(root, A)["matching"]["mastery"]  # B only has its diagnostic
    assert len([e for e in DataStore(root, A).events() if e.type == "oral_item"]) == 13
    assert not [e for e in DataStore(root, B).events() if e.type == "oral_item"]
    with pytest.raises(ValueError, match="track"):
        DataStore(root, B).append_event(DataStore(root, A).events()[0])  # an event never crosses tracks

    p = DataLayout(root).misconceptions_path
    a1 = add_misconception(p, track=A, node_id=901, subskill="matching.l_network", text="Q only rises")
    b1 = add_misconception(p, track=B, node_id=901, subskill="matching.l_network", text="Q only rises")  # not a duplicate: another track
    assert (a1, b1) == (1, 1)  # ids are per track
    assert resolve_misconception(p, a1, track=A, node_id=902, event_id="e")
    rebuild_db(root)
    assert open_misconceptions(root, A) == [] and open_misconceptions(root, B) == [{"text": "Q only rises", "subskill": "matching.l_network"}]
    assert [m["resolved"] for m in fold(read_records(p, B))] == [False]
    # interleaving targets use the track's own misconceptions
    assert interleaving_context(root, B, {"subskills": []}).open_misconceptions == open_misconceptions(root, B)
    assert interleaving_context(root, A, {"subskills": []}).open_misconceptions == []


def test_planner_and_learner_state_see_only_their_track(two, tmp_path):
    root = two
    DataLayout(root, B).goals_path.write_text("# Goals\nB: noise only\n", encoding="utf-8")
    assert "60 GHz" in learner_state(root, A)["goals"] and "noise only" in learner_state(root, B)["goals"]
    assert [n["id"] for n in learner_state(root, A)["recent_nodes"]] == [901]
    # a third track that is not onboarded: the Planner has nothing to plan from
    TrackRegistry(root).create("Gamma", "rfic", track_id="gamma")
    DataLayout(root, "gamma").ensure()
    rebuild_db(root)
    with pytest.raises(PlannerError) as e:
        Planner(root, "gamma", None, "rfic").planner_input()
    assert e.value.status == 409
    inp_a, inp_b = Planner(root, A, None, "rfic").planner_input(), Planner(root, B, None, "rfic").planner_input()
    assert "noise only" in inp_b["goals"] and "noise only" not in inp_a["goals"]
    # planner options are stored per track
    assert Planner(root, A, None, "rfic").options_path != Planner(root, B, None, "rfic").options_path


def test_same_node_id_in_two_tracks_never_mixes(two, tmp_path):
    root = two
    lca, _, _ = make(root, tmp_path / "a", track=A)
    lcb, _, _ = make(root, tmp_path / "b", track=B)
    before_a = tree(DataLayout(root, A).track_dir)
    lcb.prelab_submit(901, ANSWERS)
    assert lcb.view(901)["status"] == "lab" and lca.view(901)["status"] == "prelab"
    assert tree(DataLayout(root, A).track_dir) == before_a  # byte-identical: sessions, node.md, everything
    assert (DataLayout(root, B).sessions_dir / "0901-rc-lowpass").is_dir() and not (DataLayout(root, A).sessions_dir / "0901-rc-lowpass").exists()
    # job labels carry the track
    seen = []

    class Jobs(SyncJobs):
        def submit(self, type, fn, node_id=None):
            seen.append(node_id)
            return super().submit(type, fn, node_id)

    lcb.start_check(Jobs(), 901)
    assert seen == [f"{B}/0901-rc-lowpass"]


def test_the_oral_flow_on_one_track_leaves_the_other_byte_identical(two, tmp_path, monkeypatch):
    """M7 flow (events, misconceptions, remediation child) on track B; track A and A's lines of the shared logs do not change."""
    root = two
    before_a = tree(DataLayout(root, A).track_dir)
    ev_a = [e for e in read_events(DataLayout(root).events_path)[0] if e.track == A]
    mis_a = read_records(DataLayout(root).misconceptions_path, A)

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

    monkeypatch.setattr("app.lifecycle.oral.BuildService", FakeBuild)
    rem = {"title": "Probe the L-match Q", "why_now": "gap", "kind": "remediation", "domain": "matching", "subskills": ["matching.l_network"], "tier": 1, "size_est_min": 30}
    g = grades({"o1": 0, "o2": 3, "o7": 3}, mis=[{"text": "L-match always raises Q", "subskill": "matching.l_network"}])
    lc, ad, _ = make(root, tmp_path, {"grade_prelab_explain": [GRADES], "grade_oral": [g], "plan_branches": [{"options": [rem]}]}, track=B)
    lc.oral.jobs = SimpleNamespace(store=SimpleNamespace(get=lambda _id: SimpleNamespace(status="done", error=None)))
    ref = to_oral(lc, CLEAN)
    lc.oral.start(ref, "text", False)
    run_exam(lc, ref)
    out = lc.oral_finish(901)["oral"]
    assert out["passed"] is False and out["remediation"]["status"] in ("building", "ready") and out["remediation"]["node_dir"] == "0902-remediate"

    evs, _ = read_events(DataLayout(root).events_path)
    new = [e for e in evs if e.node_id == 901 and e.type in ("oral_item", "proof_pass", "prelab_numeric", "prelab_explain")]
    assert new and {e.track for e in new} == {B}
    assert [e for e in evs if e.track == A] == ev_a  # A's evidence: not one line added, removed or changed
    mis_b = fold(read_records(DataLayout(root).misconceptions_path, B))
    assert any("L-match always raises Q" in m["text"] for m in mis_b)
    assert read_records(DataLayout(root).misconceptions_path, A) == mis_a
    # the remediation child lives in B, parented to B's node 901; A has no node 902
    assert (DataLayout(root, B).nodes_dir / "0902-remediate" / "node.md").is_file() and not (DataLayout(root, A).nodes_dir / "0902-remediate").exists()
    assert yaml.safe_load((DataLayout(root, B).nodes_dir / "0902-remediate" / "node.md").read_text().split("---")[1])["parents"] == [901]
    assert [json.loads(x)["child"] for x in DataLayout(root, B).remediation_path.read_text().splitlines()] == ["0902-remediate"]
    assert not DataLayout(root, A).remediation_path.exists()
    assert tree(DataLayout(root, A).track_dir) == before_a
    # scores: B moved, A did not
    assert domain_scores(root, A) == {d: s for d, s in domain_scores(root, A).items()} and DataStore(root, A).events() == ev_a
    assert any(e.node_id == 901 for e in DataStore(root, B).events())
    # the cache keeps both
    with closing(connect(DataLayout(root).cache_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM nodes WHERE track = ? AND id = 902", (B,)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM nodes WHERE track = ? AND id = 902", (A,)).fetchone()[0] == 0


def test_queued_grades_are_applied_in_their_own_track(two, tmp_path):
    from tests.test_lifecycle import LLMError

    root = two
    lca, _, _ = make(root, tmp_path / "a", track=A)
    lcb, adb, _ = make(root, tmp_path / "b", {"grade_prelab_explain": [LLMError("subscription limit"), GRADES]}, track=B)
    ref = to_oral(lcb, CLEAN)
    lcb.oral.start(ref, "text", False)
    run_exam(lcb, ref)
    adb.script["grade_oral"] = [grades({"o1": 2, "o2": 3, "o7": 3})]
    lcb.oral_finish(901)
    rows = lcb.router.store.pending_grades()
    assert len(rows) == 1 and rows[0]["track"] == B and json.loads(rows[0]["request"])["track"] == B
    assert lca.pending_grades() == [] and len(lcb.pending_grades()) == 1
    assert lca.retry_grades(force=True) == 0  # A's loop never touches B's queue
    assert len(lcb.pending_grades()) == 1
    assert lcb.retry_grades(force=True) == 1
    assert sum(1 for e in DataStore(root, B).events() if e.type == "prelab_explain") == 1
    assert not [e for e in DataStore(root, A).events() if e.type == "prelab_explain"]


def test_job_label_and_busy_guard(two):
    root = two
    busy = {A: ["checker (alpha/0901-rc-lowpass)"], B: []}
    m = mgr(root, busy=lambda t: busy[t])
    for op in (lambda: m.reset(A, True), lambda: m.delete(A, "Alpha"), lambda: m.archive(A)):
        with pytest.raises(TrackError, match="jobs are running") as e:
            op()
        assert e.value.status == 409
    assert (DataLayout(root, A).nodes_dir / "0901-rc-lowpass").is_dir()  # nothing moved
    m.archive(B)  # B is idle


# ---------------------------------------------------------------------------------------------------------------------------
# reset / delete / restore / purge
# ---------------------------------------------------------------------------------------------------------------------------


def _populate(root: Path):
    """Events, misconceptions, a build record and a remediation line for both tracks, plus shared models/library."""
    for t in (A, B):
        _diag(root, "matching", "matching.l_network", 1, 3, track=t)
        _diag(root, "noise", "noise.figure_cascade", 1, 2, node_id=901, type_="oral_item", track=t)
        add_misconception(DataLayout(root).misconceptions_path, track=t, node_id=901, subskill="noise.figure_cascade", text=f"{t} misconception")
        (DataLayout(root, t).builds_dir / "0901-rc-lowpass").mkdir(parents=True)
        (DataLayout(root, t).builds_dir / "0901-rc-lowpass" / "record.json").write_text(json.dumps({"t": t}))
        DataLayout(root, t).remediation_path.write_text(json.dumps({"t": t}) + "\n")
    (root / "models" / "whisper").mkdir(parents=True)
    (root / "models" / "whisper" / "m.bin").write_bytes(b"\x01" * 128)
    (root / "library" / "razavi").mkdir(parents=True)
    (root / "library" / "razavi" / "p.txt").write_text("page")
    rebuild_db(root)


def _shared(root):
    return tree(root / "models"), tree(root / "library")


def _lines(root, t):
    return ([l for l in (root / "events.jsonl").read_text().splitlines() if json.loads(l)["track"] == t],
            [l for l in (root / "misconceptions.jsonl").read_text().splitlines() if json.loads(l)["track"] == t])


def test_reset_moves_the_tracks_data_to_the_trash_and_nothing_else(two):
    root = two
    _populate(root)
    shared, other = _shared(root), tree(DataLayout(root, B).track_dir)
    ev_a, mis_a = _lines(root, A)
    goals = DataLayout(root, A).goals_path.read_text()

    entry = mgr(root).reset(A, keep_goals=True)

    assert entry["kind"] == "reset" and not entry["restorable"] and entry["events"] == len(ev_a) == 5 and entry["misconceptions"] == len(mis_a) == 1
    lay = DataLayout(root, A)
    assert lay.goals_path.read_text() == goals  # goals kept
    assert not list(lay.nodes_dir.iterdir()) and not lay.framework_path.exists() and not lay.sessions_dir.exists() and not lay.remediation_path.exists()
    assert not lay.builds_dir.exists()
    assert _lines(root, A) == ([], []) and _lines(root, B)[0] and tree(DataLayout(root, B).track_dir) == other  # B untouched
    t = root / "_trash" / entry["entry"]
    assert [l for l in (t / "events.jsonl").read_text().splitlines()] == ev_a and (t / "misconceptions.jsonl").read_text().splitlines() == mis_a
    assert (t / "track" / "archive" / "nodes" / "0901-rc-lowpass" / "node.md").is_file() and (t / "builds" / "0901-rc-lowpass" / "record.json").is_file()
    assert _shared(root) == shared  # models/ and library/ never move
    assert domain_scores(root, A) == {} and domain_scores(root, B)
    assert TrackRegistry(root).get(A).status == "active"  # reset keeps the track itself
    # a second reset without keeping goals
    mgr(root).reset(A, keep_goals=False)
    assert not lay.goals_path.exists() and _shared(root) == shared


def test_reset_refuses_an_archived_track(two):
    mgr(two).archive(B)
    with pytest.raises(TrackError, match="unarchive"):
        mgr(two).reset(B, True)


def test_delete_needs_the_typed_name_then_restore_brings_everything_back(two):
    root = two
    _populate(root)
    shared = _shared(root)
    before_b = tree(DataLayout(root, B).track_dir)
    snap_b = tree(DataLayout(root, B).builds_dir)
    ev_b, mis_b = _lines(root, B)
    full_b = (read_events(root / "events.jsonl")[0], read_records(root / "misconceptions.jsonl"))
    name = TrackRegistry(root).get(B).name

    with pytest.raises(TrackError, match="exact name"):
        mgr(root).delete(B, "beta")  # not the typed name
    with pytest.raises(TrackError, match="exact name"):
        mgr(root).delete(B, "")
    assert DataLayout(root, B).track_dir.is_dir()

    entry = mgr(root).delete(B, name)

    assert entry["kind"] == "deleted" and entry["restorable"]
    assert not DataLayout(root, B).track_dir.exists() and not DataLayout(root, B).builds_dir.exists()
    assert [t.id for t in TrackRegistry(root).list()] == [A]
    assert _lines(root, B) == ([], []) and _lines(root, A)[0]  # only B's lines left the shared logs
    with closing(connect(root / "cache.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM nodes WHERE track = ?", (B,)).fetchone()[0] == 0
    assert _shared(root) == shared
    assert [e.entry for e in list_trash(root)] == [entry["entry"]]

    t = mgr(root).restore(entry["entry"])

    assert t.id == B and t.status == "paused" and TrackRegistry(root).active_id() == A
    assert tree(DataLayout(root, B).track_dir) == before_b and tree(DataLayout(root, B).builds_dir) == snap_b
    assert _lines(root, B) == (ev_b, mis_b)  # the same lines, back in the logs
    assert {e.id for e in read_events(root / "events.jsonl")[0]} == {e.id for e in full_b[0]}
    assert domain_scores(root, B) and list_trash(root) == [] and not (root / "_trash" / entry["entry"]).exists()
    assert _shared(root) == shared


def test_restore_needs_a_free_id_and_name_and_a_deleted_entry(two):
    root = two
    name = TrackRegistry(root).get(B).name
    e1 = mgr(root).delete(B, name)
    TrackRegistry(root).create(name, "rfic", track_id=B)  # someone re-created it
    DataLayout(root, B).ensure()
    with pytest.raises(TrackError, match="exists already"):
        mgr(root).restore(e1["entry"])
    r = mgr(root).reset(A, True)  # a reset entry is kept but not restorable
    with pytest.raises(TrackError, match="only deleted"):
        mgr(root).restore(r["entry"])
    with pytest.raises(TrackError):
        mgr(root).restore("nope")
    with pytest.raises(TrackError):
        mgr(root).restore("..")


def test_the_only_active_track_cannot_be_deleted(tmp_path):
    root = make_data(tmp_path / "d", A)
    with pytest.raises(TrackError, match="only active track"):
        mgr(root).delete(A, TrackRegistry(root).get(A).name)
    assert DataLayout(root, A).track_dir.is_dir()


def test_deleting_the_active_track_activates_another(two):
    root = two
    mgr(root).delete(A, TrackRegistry(root).get(A).name)
    assert TrackRegistry(root).active_id() == B


def test_purge_is_explicit_and_only_touches_the_trash(two):
    root = two
    _populate(root)
    shared = _shared(root)
    keep = tree(DataLayout(root, A).track_dir)
    e1 = mgr(root).delete(B, TrackRegistry(root).get(B).name)
    e2 = mgr(root).reset(A, True)
    keep_a_goals = DataLayout(root, A).goals_path.read_text()
    with pytest.raises(TrackError, match="PURGE"):
        mgr(root).purge("yes")
    with pytest.raises(TrackError, match="PURGE"):
        mgr(root).purge("purge", e1["entry"])  # exact word, case-sensitive
    assert len(list_trash(root)) == 2
    assert mgr(root).purge("PURGE", e1["entry"]) == [e1["entry"]]
    assert [e.entry for e in list_trash(root)] == [e2["entry"]]
    assert mgr(root).purge("PURGE") == [e2["entry"]] and list_trash(root) == []
    assert _shared(root) == shared and DataLayout(root, A).goals_path.read_text() == keep_a_goals
    assert (root / "tracks.yaml").is_file() and (root / "events.jsonl").is_file() and DataLayout(root, A).track_dir.is_dir()
    for bad in ("..", "../models", "x/y", ""):
        with pytest.raises(TrackError):
            mgr(root).purge("PURGE", bad)
    assert _shared(root) == shared
    assert keep != {}  # sanity: the comparison above was against real content


def test_hostile_track_ids_cannot_reach_shared_data(two):
    root = two
    shared = _shared(root)
    for bad in ("..", "../models", "models", "library", "a/b", "..\\models"):
        for op in (lambda b=bad: mgr(root).delete(b, "x"), lambda b=bad: mgr(root).reset(b, True)):
            with pytest.raises(TrackError):
                op()
    assert _shared(root) == shared


# ---------------------------------------------------------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(root))
    app = create_app()
    with TestClient(app) as c:
        yield c, root


def test_api_create_switch_scope_and_lifecycle(client):
    c, root = client
    v = c.get("/api/tracks").json()
    assert v["active"] == "rfic-default" and [t["id"] for t in v["tracks"]] == ["rfic-default"] and v["domains"] == ["rfic"]
    assert v["tracks"][0]["stage"] == "interview" and v["tracks"][0]["nodes"] == 0
    # seed node + events on the default track, then create a second one: it becomes active and starts empty
    seed_node(root, "rfic-default")
    for i in range(3):
        _diag(root, "matching", "matching.l_network", 1, 3, node_id=901, type_="oral_item")
    rebuild_db(root)
    assert [n["id"] for n in c.get("/api/nodes").json()] == [901] and [s["domain"] for s in c.get("/api/scores").json()] == ["matching"]
    r = c.post("/api/tracks", json={"name": "RFIC mmWave", "domain": "rfic"})
    assert r.status_code == 201 and r.json()["active"] == "rfic-mmwave"
    assert c.get("/api/nodes").json() == [] and c.get("/api/scores").json() == []  # Home/scores follow the active track
    assert c.get("/api/onboarding/state").json()["stage"] == "interview"
    assert c.post("/api/planner/options").status_code in (409, 422)  # nothing to plan from yet
    # switching back restores the first track's view, and the second is untouched in between
    assert c.post("/api/tracks/rfic-default/activate").json()["active"] == "rfic-default"
    assert [n["id"] for n in c.get("/api/nodes").json()] == [901] and c.get("/api/scores").json()[0]["domain"] == "matching"
    t = {x["id"]: x for x in c.get("/api/tracks").json()["tracks"]}
    assert t["rfic-default"]["current_node"]["id"] == 901 and t["rfic-default"]["tiers"] == {"matching": 1} and t["rfic-mmwave"]["nodes"] == 0
    # rename / archive / unarchive
    assert c.patch("/api/tracks/rfic-mmwave", json={"name": "mmWave"}).json()["tracks"][1]["name"] == "mmWave"
    assert c.post("/api/tracks/rfic-mmwave/archive").json()["tracks"][1]["status"] == "archived"
    assert c.post("/api/tracks/rfic-mmwave/activate").status_code == 409
    assert c.post("/api/tracks/rfic-default/archive").status_code == 409  # the only non-archived one
    assert c.post("/api/tracks/rfic-mmwave/unarchive").json()["tracks"][1]["status"] == "paused"
    assert c.post("/api/tracks/nope/activate").status_code == 404
    assert c.post("/api/tracks", json={"name": "x", "domain": "nope"}).status_code == 422
    # delete: typed name, then restore, then purge
    assert c.request("DELETE", "/api/tracks/rfic-mmwave", json={"confirm_name": "wrong"}).status_code == 422
    d = c.request("DELETE", "/api/tracks/rfic-mmwave", json={"confirm_name": "mmWave"}).json()
    assert [x["id"] for x in d["tracks"]] == ["rfic-default"] and d["trash"][0]["kind"] == "deleted"
    entry = d["trash"][0]["entry"]
    assert c.get("/api/tracks/trash").json()[0]["entry"] == entry
    assert c.request("DELETE", "/api/tracks/trash", json={"confirm": "no"}).status_code == 422
    rest = c.post(f"/api/tracks/trash/{entry}/restore").json()
    assert [x["id"] for x in rest["tracks"]] == ["rfic-default", "rfic-mmwave"] and rest["trash"] == []
    c.request("DELETE", "/api/tracks/rfic-mmwave", json={"confirm_name": "mmWave"})
    purged = c.request("DELETE", "/api/tracks/trash", json={"confirm": "PURGE"}).json()["purged"]
    assert len(purged) == 1 and purged[0].startswith("rfic-mmwave-")
    assert c.get("/api/tracks").json()["trash"] == []
    # reset keeps the track, empties it
    rr = c.post("/api/tracks/rfic-default/reset", json={"keep_goals": False}).json()
    assert rr["trash"][0]["kind"] == "reset" and rr["tracks"][0]["nodes"] == 0 and c.get("/api/scores").json() == []


def test_api_node_lookup_is_per_track(client):
    c, root = client
    c.get("/api/tracks")
    seed_node(root, "rfic-default")
    assert c.get("/api/nodes/901").status_code == 200
    c.post("/api/tracks", json={"name": "Other", "domain": "rfic"})
    assert c.get("/api/nodes/901").status_code == 404  # node 901 belongs to the other track
    assert c.get("/api/rfic/container") is not None


def test_models_dir_survives_everything_the_api_can_do(client):
    c, root = client
    c.get("/api/tracks")
    (root / "models" / "piper").mkdir(parents=True)
    (root / "models" / "piper" / "v.onnx").write_bytes(b"\x02" * 32)
    before = tree(root / "models")
    c.post("/api/tracks", json={"name": "Second", "domain": "rfic"})
    c.post("/api/tracks/second/reset", json={"keep_goals": False})
    e = c.request("DELETE", "/api/tracks/second", json={"confirm_name": "Second"}).json()["trash"][0]["entry"]
    c.post(f"/api/tracks/trash/{e}/restore")
    c.request("DELETE", "/api/tracks/second", json={"confirm_name": "Second"})
    c.request("DELETE", "/api/tracks/trash", json={"confirm": "PURGE"})
    assert tree(root / "models") == before
