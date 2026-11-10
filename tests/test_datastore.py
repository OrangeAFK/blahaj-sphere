"""data/ layout, events.jsonl, SQLite cache, rebuild_db."""

import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from app.competency.engine import score_all
from app.datastore.cache import DataStore, connect, rebuild_db, snapshot
from app.datastore.events import EventError, append_event, new_event, read_events
from app.datastore.layout import DataLayout, commit_all, init_repo

FIXTURES = Path(__file__).parent / "fixtures" / "spike_nodes"


def mk(i=0, **kw):
    base = dict(node_id=1, domain="matching", subskill=f"matching.s{i % 3}", tier=1, type="oral_item", value=0.8, weight=1.0)
    return new_event(**{**base, **kw})


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    shutil.copytree(FIXTURES, root / "archive" / "nodes")
    DataLayout(root).ensure()
    return root


def test_layout(tmp_path):
    root = tmp_path / "data"
    DataLayout(root).ensure()
    for p in ("profile", "framework", "archive/nodes", "library", "events.jsonl", ".gitignore"):
        assert (root / p).exists(), p
    ignore = (root / ".gitignore").read_text()
    assert "settings.json" in ignore and "*.raw" in ignore and "__pycache__/" in ignore
    (root / "events.jsonl").write_text("keep\n")
    (root / ".gitignore").write_text("mine\n")
    DataLayout(root).ensure()  # idempotent, never overwrites
    assert (root / "events.jsonl").read_text() == "keep\n" and (root / ".gitignore").read_text() == "mine\n"
    assert DataLayout(root).node_dir(7, "l-match-60ghz").name == "0007-l-match-60ghz"


def test_data_repo_commit(tmp_path):
    root = tmp_path / "data"
    DataLayout(root).ensure()
    init_repo(root)
    assert commit_all(root, "first") and not commit_all(root, "nothing")
    (root / "cache.db").write_text("x")
    (root / "settings.json").write_text("{}")
    assert not commit_all(root, "ignored files only")
    assert subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True).stdout.count("first") == 1


def test_append_and_read(tmp_path):
    p = tmp_path / "events.jsonl"
    a, b = mk(0), mk(1, interleaved=True)
    append_event(p, a)
    append_event(p, b)
    lines = p.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["interleaved"] is True
    events, bad = read_events(p)
    assert events == [a, b] and bad == []


def test_append_only_never_rewrites(tmp_path):
    p = tmp_path / "events.jsonl"
    append_event(p, mk(0))
    before = p.read_bytes()
    append_event(p, mk(1))
    assert p.read_bytes().startswith(before)


def test_torn_last_line_is_reported_and_next_append_survives(tmp_path):
    p = tmp_path / "events.jsonl"
    append_event(p, mk(0))
    with open(p, "a") as f:
        f.write('{"id": "torn", "ts"')
    events, bad = read_events(p)
    assert len(events) == 1 and bad[0][0] == 2
    append_event(p, mk(1))
    events, bad = read_events(p)
    assert len(events) == 2 and len(bad) == 1


def test_validation_rejects_bad_events():
    for kw in (dict(type="bogus"), dict(tier=0), dict(tier=6), dict(value=1.2), dict(weight=0), dict(domain="")):
        with pytest.raises(EventError):
            mk(**kw)


def test_rebuild_handles_spike_nodes(data):
    (data / "archive" / "nodes" / "ems_sample" / "workspace").mkdir(parents=True)  # no node.md
    bad = data / "archive" / "nodes" / "9999-broken"
    bad.mkdir()
    (bad / "node.md").write_text("no frontmatter here")
    dup = data / "archive" / "nodes" / "0902-zcopy"
    shutil.copytree(data / "archive" / "nodes" / "0902-ihp-hbt-ce-gain", dup)
    r = rebuild_db(data)
    assert r.nodes == 3 and r.skipped_dirs == ["ems_sample"] and set(r.node_issues) == {"9999-broken", "0902-zcopy"}
    with closing(connect(DataLayout(data).cache_path)) as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM nodes")}
        assert set(rows) == {901, 902, 903} and rows[901]["domain"] == "matching" and rows[901]["pdk"] == "sky130"
        assert json.loads(rows[901]["subskills"]) == ["matching.l_network", "smith_chart.navigation"]
        subs = {r["name"]: r for r in conn.execute("SELECT * FROM subskills")}
        assert subs["matching.l_network"]["n_nodes"] >= 1 and subs["smith_chart.navigation"]["domain"] == "matching"


def test_rebuild_on_empty_dir(tmp_path):
    r = rebuild_db(tmp_path / "fresh")
    assert (r.nodes, r.events, r.domains) == (0, 0, 0)


def _seed(store):
    for i in range(13):
        store.append_event(mk(i, node_id=901, value=0.8))
    store.append_event(mk(0, node_id=901, domain="noise", subskill="noise.nf", value=1.0, interleaved=True))
    store.append_event(mk(0, node_id=902, type="prelab_numeric", value=0.5, weight=0.5, assist_reveal=True))
    for i in range(6):
        store.append_event(mk(i, node_id=903, type="tier_exam_item", value=1.0, subskill=f"matching.s{i % 3}"))
    for i in range(3):
        store.append_event(mk(i, node_id=903, tier=2, value=0.9))


def test_scores_match_pure_engine(data):
    store = DataStore(data)
    store.ensure()
    _seed(store)
    expected = score_all(store.events())
    got = store.scores()
    assert set(got) == set(expected)
    for d, s in expected.items():
        assert got[d]["tier"] == s.tier and got[d]["mastery"] == pytest.approx(s.mastery) and bool(got[d]["unlocked"]) == s.unlocked
    assert got["matching"]["tier"] == 2  # the seeded exam passed


def test_rebuild_reproduces_identical_scores(data):
    store = DataStore(data)
    store.ensure()
    _seed(store)
    cache = DataStore(data).layout.cache_path
    live = snapshot(cache)
    assert live["scores"] and live["tiers"]
    cache.unlink()
    store.rebuild_db()
    assert snapshot(cache) == live  # exact equality, floats included
    store.rebuild_db()  # and again over an existing cache
    assert snapshot(cache) == live


def test_rebuild_from_events_file_alone_matches(data, tmp_path):
    """Node files + events.jsonl are all a rebuild needs: copy just those to a new dir."""
    store = DataStore(data)
    store.ensure()
    _seed(store)
    other = tmp_path / "other"
    shutil.copytree(data / "archive", other / "archive")
    shutil.copy(data / "events.jsonl", other / "events.jsonl")
    rebuild_db(other)
    assert snapshot(DataLayout(other).cache_path) == snapshot(DataLayout(data).cache_path)


def test_cache_is_disposable(data):
    store = DataStore(data)
    store.ensure()
    _seed(store)
    layout = DataLayout(data)
    with closing(sqlite3.connect(layout.cache_path)) as c, c:
        c.execute("UPDATE scores SET mastery = -1")  # corrupt derived data
    store.rebuild_db()
    assert all(s["mastery"] >= 0 for s in store.scores().values())
    assert (data / "events.jsonl").read_text().count("\n") == 13 + 1 + 1 + 6 + 3


def test_cli_rebuild(data, capsys):
    from app.datastore.__main__ import main

    assert main(["rebuild_db", "--data-dir", str(data)]) == 0
    assert "3 nodes" in capsys.readouterr().out
