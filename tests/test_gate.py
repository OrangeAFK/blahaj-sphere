import json

import pytest
import yaml

from app.builder.gate import GateContext, main, run_gate
from app.builder.nodefile import read_node_md

NODE_MD = """---
id: 901
slug: rc-lowpass
title: "Set an RC low-pass corner"
parents: []
kind: challenge
domain: filters
subskills: [filters.rc_corner, sparams.reflection_coefficient]
tier: 1
size_est_min: 20
sim_runtime_s: null   # the gate fills this
image: rfic:1
pdk: none
run: ["python3 run_sim.py"]
proof_artifacts: ["rc.sp"]
vocab: ["corner frequency", "RC"]
status: prelab
provisional: false
created: 2026-11-10T05:00:00-06:00
references: []
---
# Brief
Set the corner to the spec, somewhere between the two limits. Edit `rc.sp`.
"""

PRELAB_KEY = {
    "items": [
        {"id": "p1", "kind": "numeric", "subskill": "filters.rc_corner", "unit": "Hz", "metric": "f3db_hz", "tolerance": {"rel": 0.15},
         "hand_estimate": 150000.0},
        {"id": "p2", "kind": "numeric", "subskill": "filters.rc_corner", "unit": "ohm", "metric": "r_ohm", "tolerance": {"abs": 5.0},
         "hand_estimate": 1002.0},
        {"id": "p3", "kind": "explain", "subskill": "sparams.reflection_coefficient", "anchors": {0: "a", 1: "b", 2: "c", 3: "d"}},
    ]
}
ORAL_KEY = {
    "items": [
        {"id": "o1", "subskill": "filters.rc_corner", "tier": 1, "question": "why?", "anchors": {0: "a", 1: "b", 2: "c", 3: "d"},
         "probes": ["and then?"], "interleaved": False},
        {"id": "o2", "subskill": "sparams.reflection_coefficient", "tier": 1, "question": "what if?",
         "anchors": {0: "a", 1: "b", 2: "c", 3: "d"}, "probes": ["more?"], "interleaved": False},
    ]
}  # fmt: skip
HINTS = {"ladders": [{"subskill": s, "hints": ["think", "narrower", "method"]} for s in ("filters.rc_corner", "sparams.reflection_coefficient")]}
REF = {"pass": True, "tests": [{"name": "f3db", "pass": True, "measured": 1.0, "spec": "x", "unit": "Hz"}],
       "metrics": {"f3db_hz": 153918.2, "r_ohm": 1000.0}, "runtime_s": 3.2}  # fmt: skip
CHECKER = 'METRIC_SOURCES = {"f3db_hz": "out.txt", "r_ohm": "out.txt"}  # metric -> output file of the run\n'
STARTER = {"pass": False, "tests": [{"name": "f3db", "pass": False, "measured": 2.0, "spec": "x", "unit": "Hz"}], "metrics": {}, "runtime_s": 3.0}


def _y(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def node(tmp_path):
    n = tmp_path / "0901-rc-lowpass"
    (n / "workspace").mkdir(parents=True)
    (n / "workspace" / "rc.sp").write_text("R1 a b 10k\n")
    (n / "_hidden" / "reference_solution").mkdir(parents=True)
    (n / "_hidden" / "reference_solution" / "rc.sp").write_text("R1 a b 1k\n")
    (n / "node.md").write_text(NODE_MD, encoding="utf-8")
    (n / "prelab.md").write_text("**p1** corner?\n**p2** R?\n**p3** explain\n", encoding="utf-8")
    (n / "checker.py").write_text(CHECKER)
    _y(n / "_hidden" / "prelab_key.yaml", PRELAB_KEY)
    _y(n / "_hidden" / "oral_key.yaml", ORAL_KEY)
    _y(n / "hints.yaml", HINTS)
    return n


class FakeRunner:
    def __init__(self, ref=None, starter=None, ref_exit=0, starter_exit=1, seconds=4.2, outputs=None):
        self.ref, self.starter = ref or REF, starter or STARTER
        self.ref_exit, self.starter_exit, self.seconds = ref_exit, starter_exit, seconds
        self.outputs = outputs  # files the "run" writes into the work dir; default: out.txt with every metric
        self.calls = []

    def __call__(self, node_dir, work_rel, out_rel, timeout_s):
        self.calls.append((work_rel, out_rel))
        assert (node_dir / work_rel).is_dir()
        is_ref = work_rel == ".gate/reference"
        result, code = (self.ref, self.ref_exit) if is_ref else (self.starter, self.starter_exit)
        outputs = self.outputs if self.outputs is not None else (
            {"out.txt": "".join(f"{k} {v!r}\n" for k, v in result["metrics"].items())} if isinstance(result, dict) and isinstance(result.get("metrics"), dict) else {})
        for name, text in outputs.items():
            (node_dir / work_rel / name).write_text(text)
        if result is not None:
            (node_dir / out_rel).parent.mkdir(parents=True, exist_ok=True)
            (node_dir / out_rel).write_text(json.dumps(result))
        verdict = {0: "pass", 1: "fail"}.get(code, "error")
        return {"verdict": verdict, "exit_code": code, "seconds": self.seconds, "result": result, "problem": None, "stderr_tail": "boom" if code == 2 else ""}


def _flag(node, item_id="o2", value=True):
    ok = yaml.safe_load((node / "_hidden" / "oral_key.yaml").read_text())
    for it in ok["items"]:
        if it["id"] == item_id:
            it["interleaved"] = value
    _y(node / "_hidden" / "oral_key.yaml", ok)


def _prelab(node, **changes):
    pk = json.loads(json.dumps(PRELAB_KEY))
    for item_id, fields in changes.items():
        it = next(i for i in pk["items"] if i["id"] == item_id)
        for k, v in fields.items():
            if v is None:
                it.pop(k, None)
            else:
                it[k] = v
    _y(node / "_hidden" / "prelab_key.yaml", pk)


def test_valid_node_passes_and_gate_writes_values(node):
    _flag(node)
    r = FakeRunner()
    res = run_gate(node, r, GateContext(stale_subskills=["sparams.reflection_coefficient"]))
    assert res.passed, res.errors
    assert res.sim_runtime_s == 5 and res.checker_runtime_s == 3.2  # wall time rounded up
    fm, body = read_node_md(node / "node.md")
    assert fm["sim_runtime_s"] == 5 and "# Brief" in body and 'run: ["python3 run_sim.py"]' in (node / "node.md").read_text()
    key = yaml.safe_load((node / "_hidden" / "prelab_key.yaml").read_text())
    assert [it.get("value") for it in key["items"]] == [153918.2, 1000.0, None]
    assert list(key["items"][0])[:3] == ["id", "kind", "subskill"]  # order kept
    assert r.calls[0] == (".gate/reference", "_hidden/ref_result.json")
    assert r.calls[1][0] == ".gate/starter" and not (node / ".gate").exists()  # copies used, then removed
    assert (node / "workspace" / "rc.sp").read_text() == "R1 a b 10k\n"
    assert run_gate(node, FakeRunner(), GateContext(stale_subskills=["sparams.reflection_coefficient"])).passed  # re-runnable


@pytest.mark.parametrize(
    "setup, needle",
    [
        (lambda n: (n / "workspace" / "peek.py").write_text("print(1)"), "only in workspace: ['peek.py']"),
        (lambda n: (n / "_hidden" / "reference_solution" / "dbg.py").write_text(""), "empty files in _hidden/reference_solution/: ['dbg.py']"),
        (lambda n: [(n / d / "notes.txt").write_text("x") for d in ("workspace", "_hidden/reference_solution")], "undeclared files (not named in node.md): ['notes.txt']"),
        (lambda n: (n / "scratch.py").write_text("x"), "outside the node layout (SPEC §4.1): ['scratch.py']"),
        (lambda n: [(n / d / "et").write_text("x") for d in ("workspace", "_hidden/reference_solution")], "['et']"),  # not "between"
        (lambda n: [(n / d / "rc.s").write_text("x") for d in ("workspace", "_hidden/reference_solution")], "['rc.s']"),  # not "rc.sp"
        (lambda n: (n / "_hidden" / "answers.txt").write_text("x"), "['_hidden/answers.txt']"),
    ],
)
def test_empty_or_undeclared_files_are_rejected(node, setup, needle):
    setup(node)
    r = FakeRunner()
    res = run_gate(node, r)
    assert not res.passed and r.calls == []  # structure gate: no sims
    assert any(needle in e for e in res.errors), res.errors


def test_declared_and_infra_files_are_fine(node):
    for d in ("workspace", "_hidden/reference_solution"):
        (node / d / "xschemrc").write_text("source x")  # infra, need not be in the brief
        (node / d / "sub").mkdir()
        (node / d / "sub" / "model.lib").write_text("x")
    (node / "node.md").write_text(NODE_MD + "\nThe model is in `sub/model.lib`.\n", encoding="utf-8")
    (node / ".checker").mkdir()
    (node / ".checker" / "workspace_result.json").write_text("{}")
    assert run_gate(node, FakeRunner()).passed


def test_structure_errors_stop_before_sims(node):
    (node / "hints.yaml").unlink()
    (node / "prelab_answers.yaml").write_text("x")
    r = FakeRunner()
    res = run_gate(node, r)
    assert not res.passed and r.calls == []
    assert any("missing file: hints.yaml" in e for e in res.errors)
    assert any("prelab_answers.yaml must not be created" in e for e in res.errors)


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda t: t.replace('run: ["python3 run_sim.py"]', "run:\n  - python3 run_sim.py"), "single-line JSON list"),
        (lambda t: t.replace("kind: challenge", "kind: quest"), "kind must be one of"),
        (lambda t: t.replace("pdk: none", "pdk: sky130"), "xschemrc is required"),
        (lambda t: t.replace("tier: 1\n", ""), "missing keys: ['tier']"),
        (lambda t: "no frontmatter", "YAML frontmatter"),
    ],
)
def test_node_md_structure(node, mutate, needle):
    (node / "node.md").write_text(mutate(NODE_MD), encoding="utf-8")
    res = run_gate(node, FakeRunner())
    assert any(needle in e for e in res.errors), res.errors


def test_key_schema_errors(node):
    pk = json.loads(json.dumps(PRELAB_KEY))
    pk["items"][0]["tolerance"] = {"rel": 0.1, "abs": 1}
    pk["items"][2]["anchors"] = {0: "a", 3: "d"}
    _y(node / "_hidden" / "prelab_key.yaml", pk)
    (node / "prelab.md").write_text("**p1** only\n")
    res = run_gate(node, FakeRunner())
    assert any("tolerance must be" in e for e in res.errors)
    assert any("anchors" in e for e in res.errors)
    assert any("does not mention item p2" in e for e in res.errors)


def test_gate1_reference_must_pass(node):
    res = run_gate(node, FakeRunner(ref={**REF, "pass": False, "tests": [{"name": "f3db", "pass": False, "measured": 9, "spec": "<1", "unit": "Hz"}]}, ref_exit=1))
    assert any(e.startswith("gate 1") and "f3db: measured 9" in e for e in res.errors)
    assert read_node_md(node / "node.md")[0]["sim_runtime_s"] is None


def test_gate1_contract_shape(node):
    res = run_gate(node, FakeRunner(ref={"pass": True, "tests": [], "metrics": {"x": "NaN"}}))
    assert any("checker contract" in e for e in res.errors)


@pytest.mark.parametrize("code, needle", [(0, "already passes"), (2, "crashed on the starter")])
def test_gate2_starter_must_fail_not_crash(node, code, needle):
    res = run_gate(node, FakeRunner(starter={**STARTER, "pass": code == 0}, starter_exit=code))
    assert any(needle in e for e in res.errors) and not (node / ".gate").exists()


def test_gate3_needs_two_numeric_items_with_known_metrics(node):
    res = run_gate(node, FakeRunner(ref={**REF, "metrics": {"f3db_hz": 1.0}}))
    assert any("gate 3" in e and "p2:r_ohm" in e for e in res.errors)
    pk = json.loads(json.dumps(PRELAB_KEY))
    pk["items"][1]["kind"] = "explain"
    pk["items"][1]["anchors"] = {0: "a", 1: "b", 2: "c", 3: "d"}
    _y(node / "_hidden" / "prelab_key.yaml", pk)
    res = run_gate(node, FakeRunner())
    assert any("at least 2 numeric" in e for e in res.errors)


def test_gate4_interleaving(node):
    assert run_gate(node, FakeRunner(), GateContext()).passed  # vacuous without targets
    ctx = GateContext(open_misconceptions=[{"text": "x", "subskill": "noise.figure_cascade"}])
    res = run_gate(node, FakeRunner(), ctx)
    assert any("gate 4" in e and "no prelab or oral item targets" in e and "noise.figure_cascade" in e for e in res.errors)
    # an item targets the stale subskill, but no oral item is flagged
    res = run_gate(node, FakeRunner(), GateContext(stale_subskills=["sparams.reflection_coefficient"]))
    assert [e for e in res.errors if "gate 4" in e] == [
        "gate 4 (interleaving): at least one oral_key item must have interleaved: true and a subskill in "
        "['sparams.reflection_coefficient'] (SPEC §5.4)"
    ]
    _flag(node)
    assert run_gate(node, FakeRunner(), GateContext(stale_subskills=["sparams.reflection_coefficient"])).passed


@pytest.mark.parametrize("ctx", [GateContext(), GateContext(stale_subskills=["sparams.reflection_coefficient"])])
def test_gate4_interleaved_flag_must_target_a_target(node, ctx):
    _flag(node, "o1")  # filters.rc_corner is the node's own subskill, never a target here
    res = run_gate(node, FakeRunner(), ctx)
    assert any("must target an interleaving subskill" in e and "o1:filters.rc_corner" in e for e in res.errors), res.errors


def test_hand_estimate_is_required(node):
    _prelab(node, p1={"hand_estimate": None})
    r = FakeRunner()
    res = run_gate(node, r)
    assert r.calls == [] and any("p1: numeric items need `hand_estimate`" in e for e in res.errors)


@pytest.mark.parametrize(
    "changes, needle",
    [
        ({"p1": {"hand_estimate": 120000.0}}, "needs rel >= 0.33"),  # 22 % off, rel 0.15 too tight
        ({"p2": {"hand_estimate": 1004.0}}, "needs tolerance abs >= 6"),  # 4 ohm off, abs 5 < 6
    ],
)
def test_tolerance_must_cover_hand_estimate_with_margin(node, changes, needle):
    _prelab(node, **changes)
    res = run_gate(node, FakeRunner())
    assert not res.passed and any(e.startswith("gate 3") and needle in e for e in res.errors), res.errors


def test_tolerance_edge_cases(node):
    _prelab(node, p1={"hand_estimate": 153918.2, "tolerance": {"rel": 0.6}})
    res = run_gate(node, FakeRunner())
    assert res.passed and any("p1: tolerance rel 0.6" in w for w in res.warnings)
    _prelab(node, p1={"hand_estimate": 0.0})
    res = run_gate(node, FakeRunner(ref={**REF, "metrics": {"f3db_hz": 0.0, "r_ohm": 1000.0}}))
    assert any("rel tolerance is meaningless" in e for e in res.errors)
    _prelab(node, p1={"hand_estimate": 0.01, "tolerance": {"abs": 0.1}})
    assert run_gate(node, FakeRunner(ref={**REF, "metrics": {"f3db_hz": 0.0, "r_ohm": 1000.0}})).passed


def test_gate5_hints_and_vocab(node):
    _y(node / "hints.yaml", {"ladders": [{"subskill": "filters.rc_corner", "hints": ["one"]}]})
    (node / "node.md").write_text(NODE_MD.replace('vocab: ["corner frequency", "RC"]', "vocab: []"), encoding="utf-8")
    res = run_gate(node, FakeRunner())
    assert any("no non-empty hint ladder for subskill sparams.reflection_coefficient" in e for e in res.errors)
    assert any("vocab" in e for e in res.errors)
    assert any("1 hints (3 expected)" in w for w in res.warnings)


def test_hint_leak_is_a_warning(node):
    _y(node / "hints.yaml", {"ladders": [{"subskill": s, "hints": ["a", "b", "use R = 1000.00 ohm"]} for s in ("filters.rc_corner", "sparams.reflection_coefficient")]})
    res = run_gate(node, FakeRunner())
    assert res.passed and any("leak prelab value p2" in w for w in res.warnings)


def test_gate6_long_run_warning(node):
    res = run_gate(node, FakeRunner(seconds=700))
    assert any("gate 6" in e for e in res.errors)
    (node / "node.md").write_text(NODE_MD + "\n> **Long run warning:** ~12 min per simulation.\n", encoding="utf-8")
    assert run_gate(node, FakeRunner(seconds=700)).passed


def test_errors_aggregate_across_gates(node):
    _y(node / "hints.yaml", {"ladders": []})
    res = run_gate(node, FakeRunner(starter_exit=0, starter={**STARTER, "pass": True}, seconds=900))
    gates = {e.split(":")[0].split(" (")[0] for e in res.errors}
    assert {"gate 2", "gate 5", "gate 6"} <= gates


def test_cli_uses_domain_runner(node, monkeypatch, capsys):
    import app.builder.gate as g

    monkeypatch.setattr(g, "_domain_runner", lambda: FakeRunner())
    ctx = node.parent / "ctx.json"
    ctx.write_text(json.dumps({"stale_subskills": ["sparams.reflection_coefficient"]}))
    _flag(node)
    assert main([str(node), "--context", str(ctx)]) == 0
    assert "GATE PASS" in capsys.readouterr().out
    monkeypatch.setattr(g, "_domain_runner", lambda: FakeRunner(starter_exit=0, starter={**STARTER, "pass": True}))
    assert main([str(node), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


@pytest.mark.parametrize("starter_text", ["R1 a b 1000\n", "R1 a b 1.0e3\n", "* corner 153.9182 kHz\nR1 a b 10k\n"])
def test_starter_holding_an_answer_is_a_hard_fail(node, starter_text):
    (node / "workspace" / "rc.sp").write_text(starter_text)  # the answer already sits in the starter (any SI scaling)
    res = run_gate(node, FakeRunner())
    assert not res.passed and any(e.startswith("gate 3: the starter contains the answer to") and "workspace/rc.sp" in e for e in res.errors), res.errors


def test_trivial_and_merely_close_values_are_not_answers(node):
    ref = {**REF, "metrics": {"f3db_hz": 153918.2, "r_ohm": 50.0}}  # 50 ohm is a reference value, exempt
    pk = json.loads(json.dumps(PRELAB_KEY))
    pk["items"][1]["hand_estimate"] = 50.0
    _y(node / "_hidden" / "prelab_key.yaml", pk)
    (node / "workspace" / "rc.sp").write_text("R1 a b 10k\n* port 50 ohm, corner near 150 kHz\n")  # 150 kHz is 2.5 % off: not the answer
    assert run_gate(node, FakeRunner(ref=ref)).passed


def test_metric_sources_must_be_declared_for_numeric_metrics_and_not_be_inputs(node):
    (node / "checker.py").write_text('METRIC_SOURCES = {"f3db_hz": "rc.sp"}\n')
    r = FakeRunner()
    res = run_gate(node, r)
    assert r.calls == []
    assert any("lacks numeric prelab metrics ['r_ohm']" in e for e in res.errors)
    assert any("'rc.sp' is a source file the learner edits" in e for e in res.errors)
    (node / "checker.py").write_text("import json\nprint(1)\n")
    assert any("must declare a literal `METRIC_SOURCES" in e for e in run_gate(node, FakeRunner()).errors)
    (node / "checker.py").write_text('METRIC_SOURCES = {"f3db_hz": "../x.txt", "r_ohm": "/tmp/o"}\n')
    assert sum("must be a path relative to the work dir" in e for e in run_gate(node, FakeRunner()).errors) == 2


def test_provenance_needs_a_produced_file_that_holds_the_value(node):
    res = run_gate(node, FakeRunner(outputs={}))
    assert sum("was not produced by the reference run" in e for e in res.errors) == 2
    res = run_gate(node, FakeRunner(outputs={"out.txt": "f3db_hz 1.539182e5\nr_ohm 990\n"}))
    assert [e for e in res.errors if "provenance" in e] == [
        "gate 1 (provenance): metric r_ohm = 1000 does not appear in 'out.txt' (any SI scaling, 0.1%); the checker must take "
        "each metric from the run's output, written there at full precision"]
    assert run_gate(node, FakeRunner(outputs={"out.txt": "f = 0.1539182 MHz, R = 1.000e3"})).passed  # SI-scaled forms count
