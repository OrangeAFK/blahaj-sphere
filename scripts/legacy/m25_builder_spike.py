"""M2.5 Builder spike (manual, LIVE: calls `claude -p` with the rfic MCP server). Not run by pytest.

    uv run python -m scripts.legacy.m25_builder_spike [--only a,b,c] [--model claude-sonnet-5-5] [--no-fallback]

Builds three nodes with domains/rfic/prompts/builder.md, gates each (app/builder/gate.py), retries up
to 2x with the gate errors. A node still failing on the primary model is moved to data/spike/failed/
and rebuilt once from scratch on the fallback model. Results -> data/spike/m25_results.json.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.builder.gate import GateContext, GateResult, run_gate
from app.llm.adapters.claude_code import ClaudeCodeAdapter, clean_env
from app.llm.types import Request
from app.settings import data_dir
from domains.rfic.container import default_tracks_root
from domains.rfic.domain import run_checker
from domains.rfic.tools_mcp.config import tool_names, write_mcp_config

REPO = Path(__file__).resolve().parents[2]
BUILDER_MD = REPO / "domains" / "rfic" / "prompts" / "builder.md"
PRIMARY, FALLBACK = "claude-sonnet-5-5", "claude-opus-5-5"
MAX_RETRIES = 2
ATTEMPT_TIMEOUT_S = 45 * 60

SPECS = {
    "a": {
        "dir": "0901-sky130-lmatch-2g4", "id": 901, "pdk": "sky130",
        "option": {"title": "Hand-design a lumped L-match at 2.4 GHz into a sky130 device load", "kind": "challenge",
                   "domain": "matching", "subskills": ["matching.l_network", "smith_chart.navigation"], "tier": 2,
                   "size_est_min": 40,
                   "why_now": "Circuit-level matching with a real PDK device as the load: the learner extracts the load "
                              "impedance from simulation, then sizes an ideal series/shunt L-C network to 50 ohm."},
        "context": {"open_misconceptions": [], "stale_subskills": ["sparams.reflection_coefficient"]},
    },
    "b": {
        "dir": "0902-ihp-hbt-ce-gain", "id": 902, "pdk": "ihp-sg13g2",
        "option": {"title": "Bias an SG13G2 npn13G2 common-emitter stage and predict its small-signal gain", "kind": "challenge",
                   "domain": "amplifiers", "subskills": ["devices.bjt_bias", "amplifiers.small_signal_gain"], "tier": 2,
                   "size_est_min": 40,
                   "why_now": "Set a collector current with a resistive bias network, then hand-predict Ic, gm and the "
                              "low-frequency voltage gain and check them with DC operating point + AC analysis."},
        "context": {"open_misconceptions": [{"text": "an L-match can only step resistance up", "subskill": "matching.l_network"}],
                    "stale_subskills": []},
    },
    "c": {
        "dir": "0903-openems-msline-50ohm", "id": 903, "pdk": "none",
        "option": {"title": "Size a 50-ohm microstrip line and extract its S-parameters with openEMS", "kind": "challenge",
                   "domain": "transmission_lines", "subskills": ["transmission_lines.microstrip", "sparams.two_port"],
                   "tier": 2, "size_est_min": 45,
                   "why_now": "First EM simulation: choose the strip width on a given substrate, simulate a short "
                              "line, and extract Z0 and effective permittivity from S-parameters."},
        "context": {"open_misconceptions": [], "stale_subskills": ["smith_chart.navigation"]},
    },
}  # fmt: skip


@dataclass
class Attempt:
    n: int
    model_requested: str
    wall_s: float = 0.0
    claude: dict = field(default_factory=dict)  # trimmed claude -p JSON
    gate: dict = field(default_factory=dict)
    error: str | None = None


def build_prompt(spec: dict, node_dir: Path, created: str, gate_errors: list[str] | None) -> str:
    inp = {
        "option": spec["option"],
        "node": {"id": spec["id"], "dir": spec["dir"], "created": created, "pdk": spec["pdk"]},
        "interleaving_targets": spec["context"],
        "learner": {"goals": "Become fluent in RFIC design: matching, LNAs, mixers, EM passives (spike: placeholder goals).",
                    "summary": "No completed nodes yet.", "scores": {}, "library_chunks": []},
    }  # fmt: skip
    parts = [BUILDER_MD.read_text(encoding="utf-8"), "---", "## Input", "```json", json.dumps(inp, indent=1), "```"]
    if gate_errors:
        parts += [
            "## Previous attempt FAILED the gate",
            "Your files from the previous attempt are still in the node directory. Read what you need, fix every "
            "error below, re-run the self-checks, then finish.",
            *[f"- {e}" for e in gate_errors],
        ]
    return "\n".join(parts) + "\n"


def invoke_claude(prompt: str, node_dir: Path, mcp_path: Path, model: str) -> tuple[dict, float]:
    """Run `claude -p` with the MCP server; returns (full JSON output, wall seconds)."""
    req = Request(task_type="build_challenge", messages=[], cwd=str(node_dir), tools=tool_names(), mcp_config=str(mcp_path))
    cmd = ClaudeCodeAdapter().build_cmd(req, model)
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=node_dir, env=clean_env(), timeout=ATTEMPT_TIMEOUT_S)  # fmt: skip
    except subprocess.TimeoutExpired:
        return {"is_error": True, "result": f"timeout after {ATTEMPT_TIMEOUT_S} s"}, time.monotonic() - t0
    wall = time.monotonic() - t0
    try:
        return json.loads(p.stdout), wall
    except ValueError:
        return {"is_error": True, "result": f"exit {p.returncode}, unparseable output: {(p.stderr or p.stdout)[-1500:]}"}, wall


def trim_claude(out: dict) -> dict:
    keep = ("is_error", "subtype", "num_turns", "duration_ms", "duration_api_ms", "total_cost_usd", "usage", "modelUsage", "session_id")
    t = {k: out.get(k) for k in keep if k in out}
    t["permission_denials"] = len(out.get("permission_denials") or [])
    t["result_tail"] = (out.get("result") or "")[-1500:]
    return t


def run_with_retries(
    invoke: Callable[[list[str] | None], tuple[dict, float]],
    gate: Callable[[], GateResult],
    max_retries: int = MAX_RETRIES,
) -> tuple[bool, list[tuple[dict, float, GateResult]]]:
    """invoke(gate_errors_or_None) -> (claude json, wall s); gate() -> GateResult. At most 1 + max_retries attempts."""
    history: list[tuple[dict, float, GateResult]] = []
    errors: list[str] | None = None
    for _ in range(1 + max_retries):
        out, wall = invoke(errors)
        res = gate()
        history.append((out, wall, res))
        if res.passed:
            return True, history
        errors = res.errors
    return False, history


def used_models(claude: dict) -> list[str]:
    return sorted((claude.get("modelUsage") or {}).keys())


def sum_usage(attempts: list[Attempt]) -> dict:
    tot = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
           "num_turns": 0, "notional_cost_usd": 0.0, "claude_duration_s": 0.0}  # fmt: skip
    for a in attempts:
        u = a.claude.get("usage") or {}
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            tot[k] += int(u.get(k) or 0)
        tot["num_turns"] += int(a.claude.get("num_turns") or 0)
        tot["notional_cost_usd"] += float(a.claude.get("total_cost_usd") or 0)
        tot["claude_duration_s"] += (a.claude.get("duration_ms") or 0) / 1000
    tot["notional_cost_usd"] = round(tot["notional_cost_usd"], 2)
    tot["claude_duration_s"] = round(tot["claude_duration_s"], 1)
    return tot


def build_node(key: str, model: str, tracks_root: Path, spike_dir: Path, log) -> dict:
    spec = SPECS[key]
    node_dir = tracks_root / spec["dir"]
    node_dir.mkdir(parents=True, exist_ok=True)
    mcp_path = write_mcp_config(spike_dir / f"{spec['dir']}.mcp.json", tracks_root, spec["dir"], data_dir())
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    ctx = GateContext.from_dict(spec["context"])
    attempts: list[Attempt] = []
    t0 = time.monotonic()

    def invoke(errors):
        n = len(attempts) + 1
        log(f"[{key}] attempt {n} on {model} ({'retry with ' + str(len(errors)) + ' gate errors' if errors else 'fresh'})")
        out, wall = invoke_claude(build_prompt(spec, node_dir, created, errors), node_dir, mcp_path, model)
        attempts.append(Attempt(n=n, model_requested=model, wall_s=round(wall, 1), claude=trim_claude(out)))
        log(f"[{key}]   claude done in {wall:.0f} s, turns={out.get('num_turns')}, is_error={out.get('is_error')}, models={used_models(out)}")
        return out, wall

    def gate():
        try:
            res = run_gate(node_dir, run_checker, ctx)
        except Exception as e:  # noqa: BLE001 - a gate crash is a failed attempt, not a dead spike
            res = GateResult(errors=[f"gate crashed: {e.__class__.__name__}: {e}"])
        attempts[-1].gate = res.to_dict()
        log(f"[{key}]   gate {'PASS' if res.passed else 'FAIL'} sim_runtime_s={res.sim_runtime_s} errors={len(res.errors)}")
        for e in res.errors:
            log(f"[{key}]     - {e[:400]}")
        return res

    passed, _ = run_with_retries(invoke, gate)
    actual = sorted({m for a in attempts for m in used_models(a.claude)})
    last = attempts[-1].gate if attempts else {}
    return {
        "node": key, "dir": spec["dir"], "model_requested": model, "models_used": actual, "passed": passed,
        "retries": len(attempts) - 1, "wall_s": round(time.monotonic() - t0, 1),
        "sim_runtime_s": last.get("sim_runtime_s"), "usage": sum_usage(attempts),
        "final_errors": last.get("errors", []), "warnings": last.get("warnings", []),
        "attempts": [a.__dict__ for a in attempts],
    }  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="a,b,c")
    ap.add_argument("--model", default=PRIMARY)
    ap.add_argument("--fallback-model", default=FALLBACK)
    ap.add_argument("--no-fallback", action="store_true")
    a = ap.parse_args()
    tracks_root = default_tracks_root()
    spike_dir = data_dir() / "spike"
    spike_dir.mkdir(parents=True, exist_ok=True)
    out_path = spike_dir / "m25_results.json"
    log_path = spike_dir / "m25.log"
    results: dict = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {"runs": []}

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def save() -> None:
        out_path.write_text(json.dumps(results, indent=1), encoding="utf-8")

    for key in [k.strip() for k in a.only.split(",") if k.strip()]:
        spec = SPECS[key]
        node_dir = tracks_root / spec["dir"]
        if node_dir.exists() and any(node_dir.iterdir()):
            log(f"[{key}] {node_dir} is not empty; refusing to build over it (move it away first)")
            return 2
        r = build_node(key, a.model, tracks_root, spike_dir, log)
        results["runs"].append(r)
        save()
        if not r["passed"] and not a.no_fallback:
            dest = spike_dir / "failed" / f"{spec['dir']}-{a.model.split('-')[1]}"
            if dest.exists():
                dest = dest.with_name(dest.name + f"-{int(time.time())}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(node_dir), str(dest))
            log(f"[{key}] failed on {a.model}; moved to {dest}; rebuilding once on {a.fallback_model}")
            r2 = build_node(key, a.fallback_model, tracks_root, spike_dir, log)
            r2["fallback_for"] = a.model
            results["runs"].append(r2)
            save()

    by_node: dict[str, dict] = {}
    for r in results["runs"]:
        by_node.setdefault(r["node"], {})[r["model_requested"]] = r
    primary_pass = sum(1 for v in by_node.values() if v.get(a.model, {}).get("passed"))
    any_pass = sum(1 for v in by_node.values() if any(x["passed"] for x in v.values()))
    results["summary"] = {"primary_model": a.model, "primary_passes": primary_pass, "passes_with_fallback": any_pass,
                          "nodes": len(by_node), "go": any_pass >= 2}  # fmt: skip
    save()
    log(f"SUMMARY: {a.model} passes {primary_pass}/{len(by_node)}; with fallback {any_pass}/{len(by_node)}; "
        f"{'GO' if any_pass >= 2 else 'NO-GO'}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
