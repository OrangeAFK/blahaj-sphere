"""The RFIC domain as the core app sees it (app/domains.py)."""

from pathlib import Path

from app.domains import BuilderHooks, Domain
from app.jobs.manager import JobManager
from app.jobs.store import Job, JobStore, default_db_path
from app.preflight import Check
from domains.rfic.container import IMAGE, ContainerError, ContainerManager, PathError, default_tracks_root
from domains.rfic.routes import router, start_image_build

# Extracted on-disk size of the image (docker images, M0 measurement); `docker inspect .Size` only
# reports the compressed content size, so it can't drive the disk-space preflight.
IMAGE_SIZE_GB = 17.1


def _image_check() -> list[Check]:
    label = f"Toolchain image {IMAGE}"
    try:
        present = ContainerManager(default_tracks_root()).image_present()
    except ContainerError as e:
        return [Check(id="image", label=label, status="fail", detail=str(e), fix="Start Docker, then re-run the check.")]
    if present:
        return [Check(id="image", label=label, status="ok", detail="present")]
    return [
        Check(
            id="image", label=label, status="fail", detail="not built yet",
            fix="Build it (pulls hpretl/iic-osic-tools first; several GB, a few minutes).", fix_action="build_image",
        )  # fmt: skip
    ]


def _build_image_fix(jobs: JobManager) -> Job:
    return start_image_build(ContainerManager(default_tracks_root()), jobs)


def _node_token(containers: ContainerManager, node_dir: Path) -> str:
    """`<track>/archive/nodes/<dir>` of a host node directory; the container only mounts the tracks root."""
    try:
        return containers.node_token(Path(node_dir).resolve())
    except PathError as e:
        raise ContainerError(f"{node_dir} is not a node directory under {containers.tracks_root} (the container only mounts that)") from e


def run_checker(node_dir: Path, work_rel: str, out_rel: str, timeout_s: float) -> dict:
    """Release-gate checker runner: node_dir must be a node of some track under the mounted tracks root."""
    from domains.rfic.tools_mcp.tools import RficTools

    containers = ContainerManager(default_tracks_root().resolve())
    node = _node_token(containers, node_dir)
    try:
        jobs: JobStore | None = JobStore(default_db_path())
    except Exception:  # noqa: BLE001 - job tracking is optional
        jobs = None
    return RficTools(containers, jobs).run_checker_at(node, work_rel, out_rel, timeout_s)


def run_commands(node_dir: Path, commands: list[str], timeout_s: float, job=None) -> dict:
    """Lab "Run simulation": node_dir must be a node of some track under the mounted tracks root."""
    from domains.rfic.tools_mcp.tools import RficTools

    containers = ContainerManager(default_tracks_root().resolve())
    node = _node_token(containers, node_dir)
    return RficTools(containers, None, node=node).run_commands(node, commands, timeout_s, job)


def _builder_mcp_config(node_dir: Path, out_path: Path, call_log: Path) -> Path:
    """MCP config pinned to one node; the node must live under the mounted tracks root (SPEC §4.2)."""
    from app.settings import data_dir
    from domains.rfic.tools_mcp.config import write_mcp_config

    root = default_tracks_root().resolve()
    return write_mcp_config(out_path, root, _node_token(ContainerManager(root), node_dir), data_dir(), call_log)


def _tool_names() -> list[str]:
    from domains.rfic.tools_mcp.config import tool_names

    return tool_names()


DOMAIN = Domain(
    name="rfic",
    image=IMAGE,
    image_size_gb=IMAGE_SIZE_GB,
    router=router,
    checks=_image_check,
    fixes={"build_image": _build_image_fix},
    run_checker=run_checker,
    run_commands=run_commands,
    framework_template=Path(__file__).parent / "framework_template.yaml",
    vocab=Path(__file__).parent / "vocab.txt",
    builder=BuilderHooks(prompt=Path(__file__).parent / "prompts" / "builder.md", tool_names=_tool_names, mcp_config=_builder_mcp_config),
)
