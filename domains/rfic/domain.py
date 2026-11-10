"""The RFIC domain as the core app sees it (app/domains.py)."""

from pathlib import Path

from app.domains import Domain
from app.jobs.manager import JobManager
from app.jobs.store import Job, JobStore, default_db_path
from app.preflight import Check
from domains.rfic.container import IMAGE, ContainerError, ContainerManager, default_nodes_root
from domains.rfic.routes import router, start_image_build

# Extracted on-disk size of the image (docker images, M0 measurement); `docker inspect .Size` only
# reports the compressed content size, so it can't drive the disk-space preflight.
IMAGE_SIZE_GB = 17.1


def _image_check() -> list[Check]:
    label = f"Toolchain image {IMAGE}"
    try:
        present = ContainerManager(default_nodes_root()).image_present()
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
    return start_image_build(ContainerManager(default_nodes_root()), jobs)


def run_checker(node_dir: Path, work_rel: str, out_rel: str, timeout_s: float) -> dict:
    """Release-gate checker runner: node_dir must live under the mounted nodes root."""
    from domains.rfic.tools_mcp.tools import RficTools

    root = default_nodes_root().resolve()
    node_dir = Path(node_dir).resolve()
    if node_dir.parent != root:
        raise ContainerError(f"{node_dir} is not a node directory under {root} (the container only mounts that)")
    try:
        jobs: JobStore | None = JobStore(default_db_path())
    except Exception:  # noqa: BLE001 - job tracking is optional
        jobs = None
    return RficTools(ContainerManager(root), jobs).run_checker_at(node_dir.name, work_rel, out_rel, timeout_s)


DOMAIN = Domain(
    name="rfic",
    image=IMAGE,
    image_size_gb=IMAGE_SIZE_GB,
    router=router,
    checks=_image_check,
    fixes={"build_image": _build_image_fix},
    run_checker=run_checker,
)
