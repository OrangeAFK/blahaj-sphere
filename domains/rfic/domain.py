"""The RFIC domain as the core app sees it (app/domains.py)."""

from app.domains import Domain
from app.jobs.manager import JobManager
from app.jobs.store import Job
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


DOMAIN = Domain(
    name="rfic",
    image=IMAGE,
    image_size_gb=IMAGE_SIZE_GB,
    router=router,
    checks=_image_check,
    fixes={"build_image": _build_image_fix},
)
