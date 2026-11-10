from pathlib import Path

import pytest

from domains.rfic.container import ContainerManager, ExecResult


class FakeContainers(ContainerManager):
    """ContainerManager whose docker calls are recorded/canned. No docker needed."""

    def __init__(self, nodes_root: Path):
        super().__init__(nodes_root, name="fake-rfic")
        self.exec_calls: list[dict] = []
        self.next_result = ExecResult(0, "", "")
        self.exec_lines: list[str] = []  # fed to on_line before returning
        self.exec_hook = None  # optional callable(argv, workdir) run "inside the container"
        self.opened: list[str] = []
        self._open_url = self.opened.append

    def ensure_running(self) -> None:
        self.nodes_root.mkdir(parents=True, exist_ok=True)

    def exec(self, argv, *, workdir=None, pdk=None, env=None, timeout=None, input=None, on_line=lambda s: None):
        self.exec_calls.append({"argv": argv, "workdir": workdir, "pdk": pdk, "env": env, "timeout": timeout, "input": input})
        for line in self.exec_lines:
            on_line(line)
        if self.exec_hook:
            self.exec_hook(argv, workdir)
        return self.next_result


@pytest.fixture
def nodes_root(tmp_path) -> Path:
    root = tmp_path / "archive" / "nodes"
    (root / "n1" / "workspace").mkdir(parents=True)
    (root / "n2" / "workspace").mkdir(parents=True)
    return root


@pytest.fixture
def fake(nodes_root) -> FakeContainers:
    return FakeContainers(nodes_root)
