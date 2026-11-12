"""HTTP API for the RFIC toolchain: container, GUI/Jupyter launch, image build, sim jobs."""

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.jobs.deps import get_running_manager
from app.jobs.manager import JobManager
from app.jobs.store import Job, JobHandle
from app.settings import dev_routes_enabled
from domains.rfic.container import ContainerManager, default_tracks_root
from domains.rfic.learner import LearnerContainer, learner_for
from domains.rfic.tools_mcp.tools import TOOL_ERRORS, RficTools

DOCKERFILE = Path(__file__).resolve().parent / "docker" / "Dockerfile"

router = APIRouter(prefix="/rfic")


def containers_for(app) -> ContainerManager:
    if not hasattr(app.state, "rfic_containers"):
        app.state.rfic_containers = ContainerManager(default_tracks_root())
    return app.state.rfic_containers


def node_token(node: str | None) -> str | None:
    """The UI names a node by its directory in the ACTIVE track; the container names it `<track>/archive/nodes/<dir>`.
    A value that already contains a `/` is taken as a token as it is (it is validated by the path sandbox)."""
    if not node or "/" in node:
        return node
    from app.tracks.scope import current_scope

    return f"{current_scope().track}/archive/nodes/{node}"


def require_dev_routes() -> None:
    """404 (as if the route did not exist) unless BLAHAJ_DEV_ROUTES=1: the check runs per request, so it cannot be left on by an import."""
    if not dev_routes_enabled():
        raise HTTPException(404, "Not Found")


def get_containers(request: Request) -> ContainerManager:
    return containers_for(request.app)


def get_learner() -> LearnerContainer:
    return learner_for(default_tracks_root())


Containers = Annotated[ContainerManager, Depends(get_containers)]
Learner = Annotated[LearnerContainer, Depends(get_learner)]
Jobs = Annotated[JobManager, Depends(get_running_manager)]


class LearnerStatus(BaseModel):
    container: str
    running: bool
    node: str | None  # token of the node whose workspace/ is mounted (and nothing else)
    gui_port: int
    jupyter_port: int


class ContainerStatus(BaseModel):
    """The Builder/checker container (mounts all of data/tracks) and the learner container (one node's workspace/)."""

    image: str
    image_present: bool
    container: str
    running: bool
    tracks_root: str
    learner: LearnerStatus


class GuiRequest(BaseModel):
    tool: Literal["xschem", "klayout", "qucs-s", "desktop"] = "xschem"
    node: str | None = None
    file: str | None = None  # path in the node's workspace/ (relative to the node directory, e.g. workspace/amp.sch); needs `node`
    pdk: str | None = None
    open_browser: bool = True


class JupyterRequest(BaseModel):
    node: str
    open_browser: bool = True


class UrlResponse(BaseModel):
    url: str


class RunRequest(BaseModel):
    tool: Literal["ngspice", "openems", "python"]
    node: str
    path: str  # relative to the node directory, e.g. workspace/amp.sp
    pdk: str | None = None
    timeout_min: float | None = None


def _guard(fn):
    try:
        return fn()
    except TOOL_ERRORS as e:
        raise HTTPException(400, str(e)) from None


def _status(c: ContainerManager, learner: LearnerContainer) -> dict:
    return {**c.status(), "learner": learner.status()}


@router.get("/container", response_model=ContainerStatus, operation_id="getRficContainer")
def container_status(c: Containers, learner: Learner) -> dict:
    return _guard(lambda: _status(c, learner))


@router.post("/container/start", response_model=ContainerStatus, operation_id="startRficContainer")
def container_start(c: Containers, learner: Learner) -> dict:
    """Start the Builder/checker container; the learner container starts when a learner tool needs it (it needs a node)."""
    _guard(c.ensure_running)
    return _status(c, learner)


@router.post("/container/stop", response_model=ContainerStatus, operation_id="stopRficContainer")
def container_stop(c: Containers, learner: Learner) -> dict:
    _guard(c.stop)
    _guard(learner.stop)
    return _status(c, learner)


@router.post("/gui", response_model=UrlResponse, operation_id="launchRficGui")
def launch_gui(body: GuiRequest, learner: Learner) -> UrlResponse:
    """Start a GUI tool on the learner container's VNC display (it mounts only `node`'s workspace/) and open noVNC in the system browser."""
    node = node_token(body.node)
    url = _guard(lambda: learner.launch_gui(body.tool, node=node, file=body.file, pdk=body.pdk, open_browser=body.open_browser))
    return UrlResponse(url=url)


@router.post("/jupyter", response_model=UrlResponse, operation_id="launchRficJupyter")
def launch_jupyter(body: JupyterRequest, learner: Learner) -> UrlResponse:
    """JupyterLab in the learner container, rooted at `node`'s workspace/ (the only directory it can see)."""
    return UrlResponse(url=_guard(lambda: learner.launch_jupyter(node_token(body.node) or "", open_browser=body.open_browser)))


def start_image_build(containers: ContainerManager, jobs: JobManager) -> Job:
    def run(h: JobHandle) -> None:
        h.log(f"building {containers.image} from {DOCKERFILE.name}")
        containers.build_image(DOCKERFILE, on_line=h.log, on_progress=h.progress)

    return jobs.submit("image_build", run)


@router.post("/image/build", response_model=Job, operation_id="buildRficImage")
def build_image(c: Containers, jobs: Jobs) -> Job:
    return start_image_build(c, jobs)


@router.post("/run", response_model=Job, operation_id="runRficSim", include_in_schema=False, dependencies=[Depends(require_dev_routes)])
def run_sim(body: RunRequest, c: Containers, jobs: Jobs) -> Job:
    """DEV ONLY (BLAHAJ_DEV_ROUTES=1; 404 otherwise, and absent from the OpenAPI schema): run a sim as a job in the Builder container, which
    also sees `_hidden/` incl. the reference solution, so it must never be reachable in a learner's session. Path rules as for the MCP tools,
    pinned to `node`. The learner's Run simulation is the lifecycle `run_commands`, in the learner container."""
    node = node_token(body.node)
    tools = RficTools(c, node=node)
    method, kwargs = {
        "ngspice": (tools.run_ngspice, {"netlist": body.path, "pdk": body.pdk}),
        "openems": (tools.run_openems, {"script": body.path}),
        "python": (tools.run_python, {"path": body.path}),
    }[body.tool]
    kwargs["timeout_min"] = body.timeout_min

    def run(h: JobHandle) -> None:
        result = method(**kwargs, job=h)
        if not result["ok"]:
            raise RuntimeError(f"{body.tool} failed (exit {result['exit_code']})")

    return jobs.submit(f"sim.{body.tool}", run, node_id=c.job_label(node) if node else None)
