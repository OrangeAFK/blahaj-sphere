"""Production Builder (SPEC §7.1): a chosen Planner option -> `build_challenge` background job -> heavy agent with the
domain's MCP tools -> release gate -> retry with the gate errors (max 2) -> node indexed, or moved aside on failure.

Generic: the domain supplies the prompt, tool allow-list and MCP config (Domain.builder) and the checker (Domain.run_checker).
Build records (attempts, usage, tool calls, gate results) live in data/builds/<node dir>/, outside the node (§4.1).
"""

import json
import re
import shutil
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import yaml

from app.builder.context import interleaving_context, learner_state
from app.builder.gate import GateContext, GateResult, run_gate
from app.builder.nodefile import NodeFileError, read_node_md
from app.datastore.cache import rebuild_db
from app.datastore.layout import DataLayout
from app.domains import Domain
from app.llm.types import LLMError

MAX_RETRIES = 2  # §7.1
TASK = "build_challenge"
_ID_DIR = re.compile(r"^(\d{4,})-")
_alloc_lock = threading.Lock()


class BuildError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def slugify(title: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len]
    return s or "node"


@dataclass
class BuildPlan:
    node_id: int
    node_dir: Path
    option: dict
    ctx: GateContext
    created: str

    @property
    def name(self) -> str:
        return self.node_dir.name


class BuildService:
    def __init__(self, data_root: Path, router, domain: Domain, max_retries: int = MAX_RETRIES):
        if domain.builder is None or domain.run_checker is None:
            raise BuildError(501, f"domain {domain.name} has no Builder")
        self.layout = DataLayout(data_root)
        self.router = router
        self.domain = domain
        self.max_retries = max_retries

    @property
    def builds_dir(self) -> Path:
        return self.layout.root / "builds"

    def record_path(self, node_dir_name_: str) -> Path:
        return self.builds_dir / node_dir_name_ / "record.json"

    # --- allocation --------------------------------------------------------------------------------
    def _next_id(self) -> int:
        """Max id over node dirs and build dirs (failed builds keep their id, so llm_calls never mix two nodes) + 1."""
        ids = [0]
        for parent in (self.layout.nodes_dir, self.builds_dir, self.builds_dir / "failed"):
            if parent.is_dir():
                ids += [int(m.group(1)) for p in parent.iterdir() if (m := _ID_DIR.match(p.name))]
        return max(ids) + 1

    def prepare(self, option: dict) -> BuildPlan:
        """Allocate the node id + directory and compute the gate-4 targets. Thread-safe."""
        with _alloc_lock:
            nid = self._next_id()
            d = self.layout.node_dir(nid, slugify(option["title"]))
            d.mkdir(parents=True)
            (self.builds_dir / d.name).mkdir(parents=True, exist_ok=True)
        ctx = interleaving_context(self.layout.root, option)
        return BuildPlan(nid, d, option, ctx, datetime.now().astimezone().isoformat(timespec="seconds"))

    def start(self, jobs, option: dict):
        """Queue the build as a background job (SPEC §6.3). -> (Job, BuildPlan)."""
        plan = self.prepare(option)
        job = jobs.submit(TASK, lambda handle: self.run(plan, handle), node_id=plan.name)
        return job, plan

    # --- the build -----------------------------------------------------------------------------------
    def prompt(self, plan: BuildPlan, gate_errors: list[str] | None) -> str:
        learner = learner_state(self.layout.root)
        inp = {
            "option": plan.option,
            "node": {"id": plan.node_id, "dir": plan.name, "slug": plan.name.split("-", 1)[1], "created": plan.created},
            "interleaving_targets": {"open_misconceptions": plan.ctx.open_misconceptions, "stale_subskills": plan.ctx.stale_subskills},
            "learner": {"goals": learner["goals"], "summary": learner["summary"], "scores": learner["scores"],
                        "open_misconceptions": learner["open_misconceptions"], "library_chunks": []},
        }  # fmt: skip
        assert self.domain.builder is not None
        parts = [self.domain.builder.prompt.read_text(encoding="utf-8"), "---", "## Input", "```json", json.dumps(inp, indent=1, ensure_ascii=False), "```"]
        if gate_errors:
            parts += [
                "## Previous attempt FAILED the gate",
                "Your files from the previous attempt are still in the node directory. Read what you need, fix every "
                "error below, re-run the self-checks, then finish.",
                *[f"- {e}" for e in gate_errors],
            ]
        return "\n".join(parts) + "\n"

    def run(self, plan: BuildPlan, handle=None) -> dict:
        """Build + gate with retries. Returns the build record; raises BuildError if the node never passes."""
        hooks = self.domain.builder
        assert hooks is not None and self.domain.run_checker is not None
        log = handle.log if handle else (lambda _s: None)
        bdir = self.builds_dir / plan.name
        bdir.mkdir(parents=True, exist_ok=True)
        call_log = bdir / "mcp_calls.jsonl"
        mcp = hooks.mcp_config(plan.node_dir, bdir / "mcp.json", call_log)
        record = {
            "node_dir": plan.name, "node_id": plan.node_id, "option": plan.option, "created": plan.created,
            "context": asdict(plan.ctx), "passed": False, "attempts": [],
        }  # fmt: skip
        t_start = time.monotonic()
        errors: list[str] | None = None
        res = GateResult()
        for n in range(1, self.max_retries + 2):
            if handle:
                handle.progress((n - 1) / (self.max_retries + 1))
            self._abort_if_env_down(plan, record, log, "before attempt %d" % n)
            log(f"attempt {n}: {'retry with ' + str(len(errors)) + ' gate errors' if errors else 'fresh build'}")
            calls_before = _count_lines(call_log)
            t0 = time.monotonic()
            att: dict = {"n": n}
            try:
                out = self.router.complete(TASK, [{"role": "user", "content": self.prompt(plan, errors)}], cwd=str(plan.node_dir),
                                           tools=hooks.tool_names(), mcp_config=str(mcp), node_id=plan.node_id)  # fmt: skip
                att.update(backend=out.backend, model=out.model, usage=out.usage, meta=out.meta, result_tail=(out.text or "")[-800:])
            except LLMError as e:
                att["llm_error"] = str(e)[:1000]
                log(f"attempt {n}: builder call failed: {e}")
            att["agent_wall_s"] = round(time.monotonic() - t0, 1)
            calls = _read_calls(call_log)[calls_before:]
            att["tool_calls"] = dict(Counter(c.get("tool") for c in calls))
            att["tool_errors"] = sum(1 for c in calls if c.get("is_error"))
            log(f"attempt {n}: agent done in {att['agent_wall_s']:.0f} s, {len(calls)} tool calls; gating")
            record["attempts"].append(att)
            self._abort_if_env_down(plan, record, log, "after the agent of attempt %d" % n)
            record["attempts"].pop()
            t1 = time.monotonic()
            try:
                res = run_gate(plan.node_dir, self.domain.run_checker, plan.ctx)
            except Exception as e:  # noqa: BLE001 - a gate crash is a failed attempt, not a dead job
                res = GateResult(errors=[f"gate crashed: {e.__class__.__name__}: {e}"])
            mismatch = self._option_mismatch(plan)
            if mismatch:
                res.errors += mismatch
                res.passed = False
            att["gate_wall_s"] = round(time.monotonic() - t1, 1)
            att["gate"] = res.to_dict()
            record["attempts"].append(att)
            self._save(record)
            log(f"attempt {n}: gate {'PASS' if res.passed else 'FAIL'} ({len(res.errors)} errors)")
            for e in res.errors:
                log(f"  - {e[:300]}")
            if res.passed:
                break
            errors = list(res.errors)
            if att.get("llm_error"):
                errors.insert(0, f"your previous run ended with an error before finishing: {att['llm_error'][:300]}")
        record["wall_s"] = round(time.monotonic() - t_start, 1)
        record["passed"] = res.passed
        record["sim_runtime_s"] = res.sim_runtime_s
        if res.passed:
            record["gate4_hits"] = self._gate4_hits(plan)
            self._save(record)
            rebuild_db(self.layout.root)
            log(f"node {plan.name} released in {record['wall_s']:.0f} s")
            return record
        self._move_aside(plan, record)
        raise BuildError(502, f"node failed the release gate after {len(record['attempts'])} attempts: " + " | ".join(res.errors))

    def _move_aside(self, plan: BuildPlan, record: dict) -> None:
        """A node that is not released never stays in archive/nodes (it would be indexed as a real node)."""
        dest = self.builds_dir / "failed" / f"{plan.name}-{int(time.time())}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plan.node_dir), str(dest))
        record["moved_to"] = str(dest.relative_to(self.layout.root).as_posix())
        self._save(record)

    def _abort_if_env_down(self, plan: BuildPlan, record: dict, log, when: str) -> None:
        """A dead toolchain (e.g. Docker stopped) is not the Builder's fault: abort instead of burning a gate retry on it
        (DECISIONS M5, Docker Desktop crashed mid-build in the acceptance run)."""
        try:
            bad = [f"{c.label}: {c.detail}" for c in self.domain.checks() if c.status == "fail"]
        except Exception as e:  # noqa: BLE001 - a crashing check means we cannot trust the environment
            bad = [f"environment check crashed: {e}"]
        if not bad:
            return
        record["aborted"] = {"when": when, "problems": bad}
        log(f"toolchain unavailable {when}: {bad}")
        self._move_aside(plan, record)
        raise BuildError(503, f"toolchain unavailable {when}, build aborted (fix it and build again): " + "; ".join(bad))

    def _option_mismatch(self, plan: BuildPlan) -> list[str]:
        """node.md must carry the allocated id/slug and the chosen option unchanged (the gate itself cannot know them)."""
        try:
            fm, _ = read_node_md(plan.node_dir / "node.md")
        except (NodeFileError, OSError):
            return []  # reported by gate 0
        want = {"id": plan.node_id, "slug": plan.name.split("-", 1)[1], "kind": plan.option["kind"], "domain": plan.option["domain"],
                "tier": plan.option["tier"], "size_est_min": plan.option["size_est_min"]}  # fmt: skip
        errs = [f"node.md {k} must be {v!r} (given), got {fm.get(k)!r}" for k, v in want.items() if fm.get(k) != v]
        if sorted(fm.get("subskills") or []) != sorted(plan.option["subskills"]):
            errs.append(f"node.md subskills must be exactly the given {plan.option['subskills']}, got {fm.get('subskills')!r}")
        return errs

    def _gate4_hits(self, plan: BuildPlan) -> list[dict]:
        """Which key items target an interleaving subskill (ids + subskills only: the record holds no key content)."""
        targets = plan.ctx.targets()
        hits = []
        for fname, kind in (("prelab_key.yaml", "prelab"), ("oral_key.yaml", "oral")):
            doc = yaml.safe_load((plan.node_dir / "_hidden" / fname).read_text(encoding="utf-8")) or {}
            for it in doc.get("items") or []:
                if isinstance(it, dict) and it.get("subskill") in targets:
                    hits.append({"key": kind, "id": it.get("id"), "subskill": it["subskill"], "interleaved": it.get("interleaved")})
        return hits

    def _save(self, record: dict) -> None:
        p = self.record_path(record["node_dir"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")

    def load_record(self, node_dir: str) -> dict | None:
        p = self.record_path(node_dir)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _count_lines(p: Path) -> int:
    return len(_read_calls(p))


def _read_calls(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out

