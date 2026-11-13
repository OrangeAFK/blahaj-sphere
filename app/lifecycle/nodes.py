"""Node access, stage state machine and per-node session state (SPEC §7, §4.1).

Stages live in node.md `status`: prelab -> lab -> proof -> oral -> debrief -> done. Every transition validates the move, rewrites
`status`, refreshes the cache and auto-commits `data/` (SPEC §4.1). Runtime state that is not part of a node (tutor chat, checker
runs, proof submissions, grading) lives in data/sessions/<node dir>/*.json.
"""

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.builder.nodefile import NodeFileError, read_node_md, set_frontmatter_scalar
from app.datastore.cache import rebuild_db
from app.datastore.layout import DataLayout, commit_all, init_repo

STAGES = ("prelab", "lab", "proof", "oral", "debrief", "done")
TRANSITIONS = {"prelab": {"lab"}, "lab": {"proof"}, "proof": {"oral"}, "oral": {"debrief"}, "debrief": {"done"}}
RESCOPABLE = ("prelab", "lab", "proof")  # §7.3: a Lab button; the diagram also lets a prelab go back to the Builder
WORKING = ("lab", "proof")  # workspace unlocked: checker, tutor, show-me
NEXT_ACTION = {  # "every screen shows one primary next-action button" (§7)
    "prelab": "submit_prelab", "lab": "submit_proof", "proof": "submit_proof", "oral": "start_oral",
    "debrief": "finish_debrief", "done": "plan_next", "rescoped": "plan_next", "abandoned": "plan_next",
}
_NODE_DIR = re.compile(r"^(\d{4,})-")
_locks: dict[tuple[str, int], threading.RLock] = {}
_locks_guard = threading.Lock()


class LifecycleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def node_lock(track: str, node_id: int) -> threading.RLock:
    """One lock per node (node ids restart per track): stage moves and event writes of a node never interleave."""
    with _locks_guard:
        return _locks.setdefault((track, node_id), threading.RLock())


@dataclass(frozen=True)
class NodeRef:
    id: int
    path: Path
    track: str

    @property
    def name(self) -> str:
        return self.path.name


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def autocommit(root: Path, message: str) -> bool:
    """Commit data/ at a stage transition. A git problem must never block the learner: False = nothing committed."""
    try:
        init_repo(root)
        return commit_all(root, message)
    except (subprocess.CalledProcessError, OSError):
        return False


class NodeStore:
    def __init__(self, data_root: Path, track: str):
        self.track = track
        self.layout = DataLayout(data_root, track)

    @property
    def root(self) -> Path:
        return self.layout.root

    def job_label(self, ref: "NodeRef") -> str:
        return self.layout.job_label(ref.name)

    def find(self, node_id: int) -> NodeRef:
        if self.layout.nodes_dir.is_dir():
            for p in sorted(self.layout.nodes_dir.iterdir()):
                m = _NODE_DIR.match(p.name)
                if p.is_dir() and m and int(m.group(1)) == node_id and (p / "node.md").is_file():
                    ref = NodeRef(node_id, p, self.track)
                    if self.frontmatter(ref).get("kind") == "diagnostic":
                        raise LifecycleError(409, "the onboarding diagnostic has no lifecycle")
                    return ref
        raise LifecycleError(404, f"no node {node_id}")

    def all(self) -> list[NodeRef]:
        out = []
        if self.layout.nodes_dir.is_dir():
            for p in sorted(self.layout.nodes_dir.iterdir()):
                m = _NODE_DIR.match(p.name)
                if p.is_dir() and m and (p / "node.md").is_file():
                    try:
                        if read_node_md(p / "node.md")[0].get("kind") != "diagnostic":
                            out.append(NodeRef(int(m.group(1)), p, self.track))
                    except (NodeFileError, OSError, UnicodeDecodeError):
                        continue
        return out

    def frontmatter(self, ref: NodeRef) -> dict:
        return read_node_md(ref.path / "node.md")[0]

    def brief(self, ref: NodeRef) -> str:
        return read_node_md(ref.path / "node.md")[1]

    def status(self, ref: NodeRef) -> str:
        return str(self.frontmatter(ref).get("status"))

    def set_scalar(self, ref: NodeRef, key: str, value) -> None:
        set_frontmatter_scalar(ref.path / "node.md", key, value)

    # --- stage machine ----------------------------------------------------------------------------------
    def require(self, ref: NodeRef, *stages: str) -> str:
        if self.session(ref, "rescope", {}).get("editing"):
            raise LifecycleError(409, f"node {ref.id} is being edited by a rescope; wait for it to finish")
        st = self.status(ref)
        if st not in stages:
            raise LifecycleError(409, f"node {ref.id} is in stage {st!r}; this needs {' or '.join(stages)}")
        return st

    def transition(self, ref: NodeRef, to: str, commit: bool = True) -> None:
        """Validate and apply one stage move, refresh the cache, auto-commit data/."""
        with node_lock(ref.track, ref.id):
            cur = self.status(ref)
            if to not in TRANSITIONS.get(cur, set()):
                raise LifecycleError(409, f"node {ref.id}: cannot move from {cur!r} to {to!r}")
            self.set_scalar(ref, "status", to)
            rebuild_db(self.root)
            if commit:
                autocommit(self.root, f"node {ref.id}: {cur} -> {to}")

    # --- session state ----------------------------------------------------------------------------------
    def session_dir(self, ref: NodeRef) -> Path:
        return self.layout.sessions_dir / ref.name

    def session(self, ref: NodeRef, name: str, default):
        return read_json(self.session_dir(ref) / f"{name}.json", default)

    def save_session(self, ref: NodeRef, name: str, obj) -> None:
        write_json(self.session_dir(ref) / f"{name}.json", obj)

    # --- key files (hidden: only the code paths that need them call these) ---------------------------------
    def prelab_key(self, ref: NodeRef) -> list[dict]:
        path = ref.path / "_hidden" / "prelab_key.yaml"
        if not path.is_file():  # a tier exam has no prelab (§8.4)
            return []
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [it for it in doc.get("items") or [] if isinstance(it, dict)]

    def oral_key(self, ref: NodeRef) -> list[dict]:
        doc = yaml.safe_load((ref.path / "_hidden" / "oral_key.yaml").read_text(encoding="utf-8")) or {}
        return [it for it in doc.get("items") or [] if isinstance(it, dict) and it.get("id")]

    def prelab_prompts(self, ref: NodeRef) -> dict[str, str]:
        """item id -> its question text, cut out of prelab.md at the `**pN**` markers."""
        p = ref.path / "prelab.md"
        text = p.read_text(encoding="utf-8") if p.is_file() else ""
        marks = list(re.finditer(r"\*\*([A-Za-z][\w-]*)\*\*", text))
        out = {}
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out[m.group(1)] = text[m.end():end].strip()
        return out
