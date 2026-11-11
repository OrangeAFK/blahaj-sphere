"""HTTP API for the RFIC toolchain: container, GUI/Jupyter launch, image build, sim jobs."""

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.jobs.deps import get_running_manager
from app.jobs.manager import JobManager
from app.jobs.store import Job, JobHandle
from domains.rfic.container import ContainerError, ContainerManager, default_tracks_root
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


def get_containers(request: Request) -> ContainerManager:
    return containers_for(request.app)


Containers = Annotated[ContainerManager, Depends(get_containers)]
Jobs = Annotated[JobManager, Depends(get_running_manager)]


class ContainerStatus(BaseModel):
    image: str
    image_present: bool
    container: str
    running: bool
    tracks_root: str
    gui_port: int
    jupyter_port: int


class GuiRequest(BaseModel):
    tool: Literal["xschem", "klayout", "qucs-s", "desktop"] = "xschem"
    node: str | None = None
    file: str | None = None  # path relative to the node directory (when `node` is set) or nodes root
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


@router.get("/container", response_model=ContainerStatus, operation_id="getRficContainer")
def container_status(c: Containers) -> dict:
    return _guard(c.status)


@router.post("/container/start", response_model=ContainerStatus, operation_id="startRficContainer")
def container_start(c: Containers) -> dict:
    _guard(c.ensure_running)
    return c.status()


@router.post("/container/stop", response_model=ContainerStatus, operation_id="stopRficContainer")
def container_stop(c: Containers) -> dict:
    _guard(c.stop)
    return c.status()


@router.post("/gui", response_model=UrlResponse, operation_id="launchRficGui")
def launch_gui(body: GuiRequest, c: Containers) -> UrlResponse:
    """Start a GUI tool on the container's VNC display and open noVNC in the system browser."""
    node = node_token(body.node)
    file = f"{node}/{body.file}" if node and body.file else body.file
    url = _guard(lambda: c.launch_gui(body.tool, node=node, file=file, pdk=body.pdk, open_browser=body.open_browser))
    return UrlResponse(url=url)


@router.post("/jupyter", response_model=UrlResponse, operation_id="launchRficJupyter")
def launch_jupyter(body: JupyterRequest, c: Containers) -> UrlResponse:
    return UrlResponse(url=_guard(lambda: c.launch_jupyter(node_token(body.node) or '', open_browser=body.open_browser)))


def start_image_build(containers: ContainerManager, jobs: JobManager) -> Job:
    def run(h: JobHandle) -> None:
        h.log(f"building {containers.image} from {DOCKERFILE.name}")
        containers.build_image(DOCKERFILE, on_line=h.log, on_progress=h.progress)

    return jobs.submit("image_build", run)


@router.post("/image/build", response_model=Job, operation_id="buildRficImage")
def build_image(c: Containers, jobs: Jobs) -> Job:
    return start_image_build(c, jobs)


@router.post("/run", response_model=Job, operation_id="runRficSim")
def run_sim(body: RunRequest, c: Containers, jobs: Jobs) -> Job:
    """Run a sim as a job (streams to /api/jobs/stream). Path rules as for the MCP tools, pinned to `node`."""
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
