"""Builder release gate (SPEC §7.1), enforced by code.

`run_gate(node_dir, runner, ctx)` checks a Builder-produced node directory (layout SPEC §4.1) and
collects every error, so one Builder retry sees all of them. The checker is run through an injected
`runner` (the domain supplies it), so nothing here is domain-specific.

CLI: `uv run python -m app.builder.gate <node_dir> [--context ctx.json] [--json]` -> exit 0 pass, 1 fail.
"""

import argparse
import json
import math
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from app.builder.nodefile import NodeFileError, read_node_md, set_frontmatter_scalar, split_frontmatter

# runner(node_dir, work_rel, out_rel, timeout_s) -> {"verdict": pass|fail|error, "exit_code", "seconds",
#                                                    "result": dict|None, "problem": str|None, "stderr_tail", ...}
CheckerRunner = Callable[[Path, str, str, float], dict]

REFERENCE_DIR = "_hidden/reference_solution"
REF_RESULT = "_hidden/ref_result.json"
PRELAB_KEY = "_hidden/prelab_key.yaml"
ORAL_KEY = "_hidden/oral_key.yaml"
STARTER_COPY = ".gate/starter"
REFERENCE_COPY = ".gate/reference"
# Everything the Builder may leave at the node root and in _hidden/ (SPEC §4.1, plus the checker's
# scratch dirs: .checker/ = learner's own runs, _hidden/.checker/ = reference runs). Anything else is undeclared.
ROOT_ALLOWED = {"node.md", "prelab.md", "hints.yaml", "checker.py", "workspace", "_hidden", ".checker", ".gate"}
HIDDEN_ALLOWED = {"reference_solution", "prelab_key.yaml", "oral_key.yaml", "ref_result.json", ".checker"}
INFRA_FILES = {"xschemrc"}  # required by the gate itself, need not be named in the brief
LONG_RUN_S = 600
CHECKER_TIMEOUT_S = 3600

REQUIRED_FILES = ("node.md", "prelab.md", "hints.yaml", "checker.py", PRELAB_KEY, ORAL_KEY)
REQUIRED_DIRS = ("workspace", REFERENCE_DIR)
# Learner-owned or later-stage paths (SPEC §4.1): the Builder must not create them. (REF_RESULT is the
# gate's own output; it is deleted before gate 1 so a re-run never sees a stale one.)
FORBIDDEN = ("prelab_answers.yaml", "proof", "oral.md", "debrief.md")
NODE_KEYS = (
    "id", "slug", "title", "parents", "kind", "domain", "subskills", "tier", "size_est_min", "sim_runtime_s",
    "image", "pdk", "run", "proof_artifacts", "vocab", "status", "provisional", "created", "references",
)  # fmt: skip
KINDS = {"challenge", "remediation", "tier_exam", "diagnostic"}
PDKS = {"ihp-sg13g2", "sky130", "none"}
STATUSES = {"prelab", "lab", "proof", "oral", "debrief", "done", "rescoped", "abandoned"}
ANCHOR_KEYS = {0, 1, 2, 3}
_RUN_LINE = re.compile(r"^run:[ \t]*(.*?)[ \t]*(?:#.*)?$", re.M)
_LONG_RUN = re.compile(r"long[- ]run", re.I)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


@dataclass
class GateContext:
    """Gate 4 input: what the learner's history says needs interleaving."""

    open_misconceptions: list[dict] = field(default_factory=list)  # [{text, subskill}]
    stale_subskills: list[str] = field(default_factory=list)  # not evidenced in the last 3 nodes

    def targets(self) -> set[str]:
        return {m["subskill"] for m in self.open_misconceptions if m.get("subskill")} | set(self.stale_subskills)

    @classmethod
    def from_dict(cls, d: dict | None) -> "GateContext":
        d = d or {}
        return cls(list(d.get("open_misconceptions") or []), list(d.get("stale_subskills") or []))


@dataclass
class GateResult:
    passed: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sim_runtime_s: int | None = None
    checker_runtime_s: float | None = None
    ref_metrics: dict = field(default_factory=dict)
    reference: dict | None = None  # raw runner output (minus the result), for logs
    starter: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _load_yaml(path: Path, errors: list[str]):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        errors.append(f"{path.name}: cannot parse YAML: {e}")
        return None


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _items(doc, name: str, errors: list[str]) -> list[dict]:
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list) or not doc["items"]:
        errors.append(f"{name}: must be a mapping with a non-empty `items` list")
        return []
    items = [it for it in doc["items"] if isinstance(it, dict)]
    if len(items) != len(doc["items"]):
        errors.append(f"{name}: every item must be a mapping")
    ids = [it.get("id") for it in items]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        errors.append(f"{name}: every item needs a unique non-empty string `id` (got {ids})")
    return items


def _check_anchors(item: dict, where: str, errors: list[str]) -> None:
    a = item.get("anchors")
    if not isinstance(a, dict) or {int(k) for k in a if str(k).isdigit()} != ANCHOR_KEYS or not all(
        isinstance(v, str) and v.strip() for v in a.values()
    ):
        errors.append(f"{where}: `anchors` must map exactly 0,1,2,3 to non-empty strings")


# ---- G0: structure ----


def _structure(node: Path, errors: list[str], warnings: list[str]) -> dict | None:
    """Static checks. Returns parsed pieces, or None if too broken to continue."""
    for f in REQUIRED_FILES:
        if not (node / f).is_file():
            errors.append(f"missing file: {f}")
    for d in REQUIRED_DIRS:
        p = node / d
        if not p.is_dir() or not any(x.is_file() for x in p.rglob("*")):
            errors.append(f"missing or empty directory: {d}/")
    for f in FORBIDDEN:
        if (node / f).exists():
            errors.append(f"{f} must not be created by the Builder (it belongs to the learner or a later stage)")
    if not (node / "node.md").is_file():
        return None

    try:
        fm, body = read_node_md(node / "node.md")
    except NodeFileError as e:
        errors.append(str(e))
        return None
    _file_hygiene(node, (node / "node.md").read_text(encoding="utf-8"), errors)
    missing = [k for k in NODE_KEYS if k not in fm]
    if missing:
        errors.append(f"node.md frontmatter missing keys: {missing}")
    if fm.get("kind") not in KINDS:
        errors.append(f"node.md kind must be one of {sorted(KINDS)}, got {fm.get('kind')!r}")
    if str(fm.get("pdk")) not in PDKS:
        errors.append(f"node.md pdk must be one of {sorted(PDKS)}, got {fm.get('pdk')!r}")
    if fm.get("status") not in STATUSES:
        errors.append(f"node.md status must be one of {sorted(STATUSES)}, got {fm.get('status')!r}")
    for k in ("id", "tier", "size_est_min"):
        if not isinstance(fm.get(k), int) or isinstance(fm.get(k), bool):
            errors.append(f"node.md {k} must be an integer, got {fm.get(k)!r}")
    subskills = fm.get("subskills")
    if not isinstance(subskills, list) or not subskills or not all(isinstance(s, str) and s for s in subskills):
        errors.append("node.md subskills must be a non-empty list of strings")
        subskills = []

    # `run` must be a one-line JSON list: checkers read it without a YAML parser (DECISIONS M2.5).
    m = _RUN_LINE.search(split_frontmatter((node / "node.md").read_text(encoding="utf-8"))[0])
    run = None
    if m:
        try:
            run = json.loads(m.group(1))
        except ValueError:
            pass
    if not (isinstance(run, list) and run and all(isinstance(c, str) and c.strip() for c in run)):
        errors.append('node.md run must be a single-line JSON list of command strings, e.g. run: ["python3 run_sim.py"]')
    elif run != fm.get("run"):
        errors.append("node.md run: the JSON line and the YAML value disagree")
    if str(fm.get("pdk")) not in ("none", "None") and not (node / "workspace" / "xschemrc").is_file():
        errors.append("workspace/xschemrc is required when pdk is not none (Xschem needs the PDK symbols)")

    pk = _load_yaml(node / PRELAB_KEY, errors) if (node / PRELAB_KEY).is_file() else None
    ok = _load_yaml(node / ORAL_KEY, errors) if (node / ORAL_KEY).is_file() else None
    hints = _load_yaml(node / "hints.yaml", errors) if (node / "hints.yaml").is_file() else None
    prelab_items = _items(pk, "prelab_key.yaml", errors) if pk is not None else []
    oral_items = _items(ok, "oral_key.yaml", errors) if ok is not None else []

    for it in prelab_items:
        where = f"prelab_key.yaml {it.get('id')}"
        if it.get("kind") not in ("numeric", "explain"):
            errors.append(f"{where}: kind must be numeric or explain")
        if not isinstance(it.get("subskill"), str) or not it.get("subskill"):
            errors.append(f"{where}: subskill is required")
        if it.get("kind") == "numeric":
            for k in ("unit", "metric"):
                if not isinstance(it.get(k), str) or not it.get(k):
                    errors.append(f"{where}: numeric items need a string `{k}`")
            tol = it.get("tolerance")
            if not (isinstance(tol, dict) and len(tol) == 1 and set(tol) <= {"rel", "abs"} and _is_num(next(iter(tol.values())))
                    and next(iter(tol.values())) > 0):  # fmt: skip
                errors.append(f"{where}: tolerance must be {{rel: x}} or {{abs: x}} with x > 0")
            if not _is_num(it.get("hand_estimate")):
                errors.append(f"{where}: numeric items need `hand_estimate`: your own hand calculation, a number in `unit` (SPEC §5.3)")
            if "value" in it:
                warnings.append(f"{where}: Builder wrote a `value`; the gate overwrites it from ref_result.json")
        elif it.get("kind") == "explain":
            _check_anchors(it, where, errors)
    for it in oral_items:
        where = f"oral_key.yaml {it.get('id')}"
        for k in ("subskill", "question"):
            if not isinstance(it.get(k), str) or not it.get(k):
                errors.append(f"{where}: `{k}` is required")
        if not isinstance(it.get("tier"), int):
            errors.append(f"{where}: `tier` must be an integer")
        if not isinstance(it.get("interleaved"), bool):
            errors.append(f"{where}: `interleaved` must be true or false")
        if not isinstance(it.get("probes"), list) or not it["probes"]:
            errors.append(f"{where}: `probes` must be a non-empty list")
        _check_anchors(it, where, errors)

    if (node / "prelab.md").is_file():
        prelab_text = (node / "prelab.md").read_text(encoding="utf-8")
        for it in prelab_items:
            if isinstance(it.get("id"), str) and not re.search(rf"\b{re.escape(it['id'])}\b", prelab_text):
                errors.append(f"prelab.md does not mention item {it['id']} from prelab_key.yaml")
    return {"fm": fm, "body": body, "subskills": subskills, "pk": pk, "prelab": prelab_items, "oral": oral_items, "hints": hints}


def _named(name: str, text: str) -> bool:
    """`name` appears in `text` as a whole file name (so "et" does not match inside "between")."""
    return re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w/-]|\.\w)", text) is not None


def _files(d: Path) -> set[str]:
    return {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()} if d.is_dir() else set()


def _file_hygiene(node: Path, node_md_text: str, errors: list[str]) -> None:
    """Reject empty or undeclared files. Workspace and reference must hold the same files, each named in
    node.md (brief or frontmatter) unless it is gate-required infrastructure; nothing outside the §4.1
    layout may sit at the node root or in _hidden/. Delete scratch files and simulation outputs."""
    extra_root = sorted(p.name for p in node.iterdir() if p.name not in ROOT_ALLOWED)
    hidden = node / "_hidden"
    extra_hidden = sorted(p.name for p in hidden.iterdir() if p.name not in HIDDEN_ALLOWED) if hidden.is_dir() else []
    if extra_root or extra_hidden:
        errors.append(f"undeclared files outside the node layout (SPEC §4.1): {extra_root + ['_hidden/' + x for x in extra_hidden]}; delete them")
    ws, ref = _files(node / "workspace"), _files(node / REFERENCE_DIR)
    for rel_dir, names in (("workspace", ws), (REFERENCE_DIR, ref)):
        empty = sorted(n for n in names if (node / rel_dir / n).stat().st_size == 0)
        if empty:
            errors.append(f"empty files in {rel_dir}/: {empty}; delete them or give them content")
    if ws != ref:
        errors.append(f"workspace/ and {REFERENCE_DIR}/ must hold the same files; only in workspace: {sorted(ws - ref)}, "
                      f"only in reference: {sorted(ref - ws)} (delete scratch files and simulation outputs)")  # fmt: skip
    undeclared = sorted(
        n for n in ws | ref
        if n.split("/")[-1] not in INFRA_FILES and not _named(n, node_md_text) and not _named(n.split("/")[-1], node_md_text)
    )  # fmt: skip
    if undeclared:
        errors.append(f"undeclared files (not named in node.md): {undeclared}; list each learner file in the brief, delete scratch files and outputs")


# ---- G4, G5: static content ----


def _interleaving(s: dict, ctx: GateContext, errors: list[str]) -> None:
    """Gate 4. `interleaved: true` earns 1.5x weight and resolves misconceptions (SPEC §7.7, §8.2), so the flag is only
    valid on an item whose subskill is one of the given targets; when targets exist, at least one such oral item is required."""
    targets = ctx.targets()
    flagged = [it for it in s["oral"] if it.get("interleaved") is True]
    wrong = [f"{it.get('id')}:{it.get('subskill')}" for it in flagged if it.get("subskill") not in targets]
    if wrong:
        allowed = f"one of {sorted(targets)}" if targets else "none (no interleaving targets were given; set interleaved: false)"
        errors.append(f"gate 4 (interleaving): oral items marked interleaved: true must target an interleaving subskill; {wrong} do not (allowed: {allowed})")
    if not targets:
        return
    hit = [it["id"] for it in s["prelab"] + s["oral"] if it.get("subskill") in targets]
    if not hit:
        errors.append(f"gate 4 (interleaving): no prelab or oral item targets any of {sorted(targets)}; add one using the exact subskill id")
    if not any(it.get("subskill") in targets for it in flagged):
        errors.append(f"gate 4 (interleaving): at least one oral_key item must have interleaved: true and a subskill in {sorted(targets)} (SPEC §5.4)")


def _hints_vocab(s: dict, errors: list[str], warnings: list[str]) -> None:
    ladders = (s["hints"] or {}).get("ladders") if isinstance(s["hints"], dict) else None
    if not isinstance(ladders, list):
        errors.append("gate 5: hints.yaml must have a `ladders` list")
        ladders = []
    by_sub = {}
    for lad in ladders:
        if isinstance(lad, dict) and isinstance(lad.get("subskill"), str):
            hints = [h for h in (lad.get("hints") or []) if isinstance(h, str) and h.strip()]
            by_sub.setdefault(lad["subskill"], []).extend(hints)
    for sub in s["subskills"]:
        if not by_sub.get(sub):
            errors.append(f"gate 5: hints.yaml has no non-empty hint ladder for subskill {sub}")
        elif len(by_sub[sub]) < 3:
            warnings.append(f"hints.yaml ladder for {sub} has {len(by_sub[sub])} hints (3 expected)")
    vocab = s["fm"].get("vocab")
    if not isinstance(vocab, list) or not any(isinstance(v, str) and v.strip() for v in vocab):
        errors.append("gate 5: node.md vocab must be a non-empty list of strings")
    s["_hint_text"] = "\n".join(h for hs in by_sub.values() for h in hs)


# ---- G1, G2: checker runs ----


def _shape_errors(result) -> list[str]:
    if not isinstance(result, dict):
        return ["result is not a JSON object"]
    errs = []
    if not isinstance(result.get("pass"), bool):
        errs.append("`pass` must be a boolean")
    tests = result.get("tests")
    if not isinstance(tests, list) or not tests or not all(isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("pass"), bool) for t in tests):
        errs.append("`tests` must be a non-empty list of {name, pass, ...}")
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        errs.append("`metrics` must be an object")
    else:
        bad = [k for k, v in metrics.items() if not _is_num(v)]
        if bad:
            errs.append(f"metrics must be finite numbers; bad: {bad}")
    if not _is_num(result.get("runtime_s")):
        errs.append("`runtime_s` must be a number")
    return errs


def _brief(run: dict) -> dict:
    return {k: v for k, v in run.items() if k != "result"}


def _failing_tests(result) -> str:
    if not isinstance(result, dict):
        return ""
    fails = [t for t in result.get("tests") or [] if isinstance(t, dict) and not t.get("pass")]
    return "; ".join(f"{t.get('name')}: measured {t.get('measured')} {t.get('unit', '')} vs spec {t.get('spec')}" for t in fails[:6])


def _run_copy(node: Path, runner: CheckerRunner, src: str, copy: str, out_rel: str) -> dict:
    """Run the checker on a throwaway copy of `src`, so gate runs never add outputs to the node."""
    shutil.rmtree(node / copy, ignore_errors=True)
    try:
        shutil.copytree(node / src, node / copy)
        return runner(node, copy, out_rel, CHECKER_TIMEOUT_S)
    finally:
        shutil.rmtree(node / copy, ignore_errors=True)
        if (node / ".gate").is_dir() and not any((node / ".gate").iterdir()):
            (node / ".gate").rmdir()


def _run_reference(node: Path, runner: CheckerRunner, res: GateResult) -> dict | None:
    (node / REF_RESULT).unlink(missing_ok=True)
    run = _run_copy(node, runner, REFERENCE_DIR, REFERENCE_COPY, REF_RESULT)
    res.reference = _brief(run)
    result = run.get("result")
    detail = run.get("problem") or (run.get("stderr_tail") or "")[-1500:]
    if run.get("verdict") != "pass":
        why = _failing_tests(result) if run.get("verdict") == "fail" else detail
        res.errors.append(f"gate 1: checker on {REFERENCE_DIR} must exit 0, got exit {run.get('exit_code')} ({run.get('verdict')}): {why}")
        return None
    shape = _shape_errors(result)
    if shape:
        res.errors.append(f"gate 1: reference result does not follow the checker contract (SPEC §5.2): {'; '.join(shape)}")
        return None
    if result["pass"] is not True:
        res.errors.append("gate 1: checker exited 0 on the reference but reported pass: false")
        return None
    res.sim_runtime_s = max(1, math.ceil(run.get("seconds") or result["runtime_s"]))
    res.checker_runtime_s = result["runtime_s"]
    res.ref_metrics = dict(result["metrics"])
    set_frontmatter_scalar(node / "node.md", "sim_runtime_s", res.sim_runtime_s)
    return result


def _run_starter(node: Path, runner: CheckerRunner, res: GateResult) -> None:
    run = _run_copy(node, runner, "workspace", STARTER_COPY, "_hidden/.checker/gate_starter_result.json")
    res.starter = _brief(run)
    v = run.get("verdict")
    if v == "pass":
        res.errors.append("gate 2: the unmodified starter already passes the checker; it must fail at least one spec")
    elif v == "error":
        detail = run.get("problem") or (run.get("stderr_tail") or "")[-1500:]
        res.errors.append(f"gate 2: checker crashed on the starter (exit {run.get('exit_code')}); it must fail a spec (exit 1), not error: {detail}")
    elif isinstance(run.get("result"), dict) and run["result"].get("pass") is not False:
        res.errors.append("gate 2: checker exited 1 on the starter but its result does not say pass: false")


# ---- G3: prelab key values ----


def _prelab_values(node: Path, s: dict, res: GateResult, metrics: dict | None) -> None:
    numeric = [it for it in s["prelab"] if it.get("kind") == "numeric"]
    if len(numeric) < 2:
        res.errors.append(f"gate 3: need at least 2 numeric prelab items, found {len(numeric)}")
    if metrics is None:
        return
    missing = [f"{it['id']}:{it.get('metric')}" for it in numeric if it.get("metric") not in metrics]
    if missing:
        res.errors.append(f"gate 3: prelab metrics not in the reference result's metrics: {missing}; available: {sorted(metrics)}")
        return
    for it in numeric:
        it["value"] = metrics[it["metric"]]
        _tolerance_margin(it, res)
    with open(node / PRELAB_KEY, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(s["pk"], f, sort_keys=False, allow_unicode=True)
    # Heuristic: a number in the hints or the starter files within 1 % of a key value (ignoring tiny integers like 2 or 3).
    # A starter that already holds the answer usually means the metric is read back from a design file, not simulated.
    hint_nums = _numbers(s.get("_hint_text", ""))
    starter_nums = {p.relative_to(node).as_posix(): _numbers(_text_or_empty(p)) for p in sorted((node / "workspace").rglob("*")) if p.is_file()}
    for it in numeric:
        v = float(it["value"])
        if v == 0 or (v.is_integer() and abs(v) < 10):
            continue
        if any(abs(n - v) <= 0.01 * abs(v) for n in hint_nums):
            res.warnings.append(f"hints.yaml may leak prelab value {it['id']}={v:g}")
        files = [f for f, nums in starter_nums.items() if any(abs(n - v) <= 0.01 * abs(v) for n in nums)]
        if files:
            res.warnings.append(f"starter files {files} contain prelab value {it['id']}={v:g}: is metric {it['metric']} simulated, "
                                f"or read back from a design file the learner edits?")  # fmt: skip


def _numbers(text: str) -> list[float]:
    return [n for n in map(float, _NUMBER.findall(text)) if not (n.is_integer() and abs(n) < 10)]


def _text_or_empty(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _tolerance_margin(it: dict, res: GateResult) -> None:
    """A correct hand calculation must land inside the tolerance with margin (DECISIONS M2.5 #70, SPEC §5.3):
    rel >= 1.5 x |hand - sim| / |sim|, abs >= 1.5 x |hand - sim|."""
    h, v, tol = it.get("hand_estimate"), float(it["value"]), it.get("tolerance") or {}
    if not _is_num(h) or not tol:
        return  # already reported by G0
    where = f"gate 3: prelab_key.yaml {it['id']}"
    err = abs(float(h) - v)
    if "abs" in tol:
        need = 1.5 * err
        if tol["abs"] < need:
            res.errors.append(f"{where}: hand_estimate {h:g} vs simulated {v:g} {it.get('unit', '')} needs tolerance abs >= {need:.4g} "
                              f"(1.5 x the hand-calc error), got {tol['abs']:g}; widen it or fix the hand estimate")  # fmt: skip
        return
    if v == 0:
        res.errors.append(f"{where}: simulated value is 0, so a rel tolerance is meaningless; use {{abs: x}}")
        return
    need = 1.5 * err / abs(v)
    if tol["rel"] < need:
        res.errors.append(f"{where}: hand_estimate {h:g} vs simulated {v:g} {it.get('unit', '')} is {err / abs(v):.1%} off; tolerance "
                          f"needs rel >= {need:.3f} (1.5 x that), got {tol['rel']:g}; widen it or fix the hand estimate")  # fmt: skip
    elif tol["rel"] > 0.5:
        res.warnings.append(f"prelab_key.yaml {it['id']}: tolerance rel {tol['rel']:g} > 0.5 makes the prediction weakly informative")


# ---- driver ----


def run_gate(node_dir: Path, runner: CheckerRunner, ctx: GateContext | None = None) -> GateResult:
    node = Path(node_dir)
    ctx = ctx or GateContext()
    res = GateResult()
    s = _structure(node, res.errors, res.warnings)
    if s is None or res.errors:
        res.errors.insert(0, "gate 0 (structure) failed; simulations not run")
        return res
    _interleaving(s, ctx, res.errors)
    _hints_vocab(s, res.errors, res.warnings)
    result = _run_reference(node, runner, res)
    _run_starter(node, runner, res)
    _prelab_values(node, s, res, result["metrics"] if result else None)
    if res.sim_runtime_s is not None and res.sim_runtime_s > LONG_RUN_S and not _LONG_RUN.search(s["body"]):
        res.errors.append(f"gate 6: reference run took {res.sim_runtime_s} s (> {LONG_RUN_S} s); the node.md brief must contain a long-run warning")
    res.passed = not res.errors
    return res


def _domain_runner() -> CheckerRunner:
    from app.domains import load_domain

    dom = load_domain()
    if dom is None or dom.run_checker is None:
        raise SystemExit("the active domain has no checker runner")
    return dom.run_checker


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gate", description="SPEC §7.1 release gate for one node directory")
    ap.add_argument("node_dir", type=Path)
    ap.add_argument("--context", type=Path, help="JSON {open_misconceptions: [{text, subskill}], stale_subskills: [...]}")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args(argv)
    ctx = GateContext.from_dict(json.loads(a.context.read_text(encoding="utf-8")) if a.context else None)
    res = run_gate(a.node_dir.resolve(), _domain_runner(), ctx)
    if a.json:
        print(json.dumps(res.to_dict(), indent=1))
    else:
        print("GATE PASS" if res.passed else "GATE FAIL", f"(sim_runtime_s={res.sim_runtime_s})")
        for e in res.errors:
            print("  ERROR", e)
        for w in res.warnings:
            print("  warn ", w)
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
