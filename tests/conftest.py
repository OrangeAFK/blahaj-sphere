from pathlib import Path

import pytest

from domains.rfic.container import ContainerManager, ExecResult
from domains.rfic.learner import LearnerContainer


class FakeContainers(ContainerManager):
    """ContainerManager whose docker calls are recorded/canned. No docker needed."""

    def __init__(self, tracks_root: Path):
        super().__init__(tracks_root, name="fake-rfic")
        self.exec_calls: list[dict] = []
        self.next_result = ExecResult(0, "", "")
        self.exec_lines: list[str] = []  # fed to on_line before returning
        self.exec_hook = None  # optional callable(argv, workdir) run "inside the container"
        self.opened: list[str] = []
        self._open_url = self.opened.append

    def ensure_running(self) -> None:
        self.tracks_root.mkdir(parents=True, exist_ok=True)

    def exec(self, argv, *, workdir=None, pdk=None, env=None, timeout=None, input=None, on_line=lambda s: None):
        self.exec_calls.append({"argv": argv, "workdir": workdir, "pdk": pdk, "env": env, "timeout": timeout, "input": input})
        for line in self.exec_lines:
            on_line(line)
        if self.exec_hook:
            self.exec_hook(argv, workdir)
        return self.next_result


class FakeLearner(LearnerContainer):
    """LearnerContainer with no docker: records the nodes it was bound to and the execs it ran."""

    def __init__(self, tracks_root: Path):
        super().__init__(tracks_root, name="fake-rfic-learner")
        self.bound: list[str | None] = []
        self.exec_calls: list[dict] = []
        self.next_result = ExecResult(0, "", "")
        self.exec_lines: list[str] = []
        self.exec_hook = None  # optional callable(argv, node) run "inside the container"
        self.opened: list[str] = []
        self._open_url = self.opened.append

    def ensure_running(self, node=None) -> None:
        self.tracks_root.mkdir(parents=True, exist_ok=True)
        if node:
            self.workspace_host(node)  # same validation as the real thing
        self.bound.append(node)

    def bound_node(self):
        return next((n for n in reversed(self.bound) if n), None)

    def exec(self, argv, *, node=None, workdir=None, pdk=None, env=None, timeout=None, input=None, on_line=lambda s: None):
        self.ensure_running(node)
        self.exec_calls.append({"argv": argv, "node": node, "workdir": workdir, "pdk": pdk, "env": env, "timeout": timeout})
        for line in self.exec_lines:
            on_line(line)
        if self.exec_hook:
            self.exec_hook(argv, node)
        return self.next_result


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch):
    """No test may touch the real data/ (the tracks migration would run on it): default to an empty temp data dir.
    Tests that need a particular data dir still call monkeypatch.setenv themselves."""
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path_factory.mktemp("data")))


NODE_REL = "t1/archive/nodes"  # nodes of track t1 under the tracks root (SPEC §4.2)


@pytest.fixture
def tracks_root(tmp_path) -> Path:
    root = tmp_path / "tracks"
    (root / NODE_REL / "n1" / "workspace").mkdir(parents=True)
    (root / NODE_REL / "n2" / "workspace").mkdir(parents=True)
    return root


@pytest.fixture
def nodes_root(tracks_root) -> Path:
    """Host dir holding track t1's nodes: `tracks_root/t1/archive/nodes`."""
    return tracks_root / NODE_REL


@pytest.fixture
def node1() -> str:
    """Node token of the first fixture node."""
    return f"{NODE_REL}/n1"


@pytest.fixture
def node2() -> str:
    return f"{NODE_REL}/n2"


@pytest.fixture
def fake(tracks_root) -> FakeContainers:
    return FakeContainers(tracks_root)


@pytest.fixture
def fake_learner(tracks_root) -> FakeLearner:
    return FakeLearner(tracks_root)
