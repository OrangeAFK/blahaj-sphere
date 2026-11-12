"""Graph (SPEC §11): the active track's node DAG. Read-only."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.lifecycle.graph import graph_view
from app.tracks.scope import current_scope

router = APIRouter()


class GraphNode(BaseModel):
    id: int
    dir: str
    title: str | None = None
    kind: str | None = None  # challenge | remediation | tier_exam | diagnostic
    domain: str | None = None
    tier: int | None = None
    status: str  # prelab | lab | proof | oral | debrief | done | rescoped | abandoned
    provisional: bool
    outcome: str | None = None  # success | oral_failed | proof_failed | too_hard | in_progress | abandoned | rescoped (not for the diagnostic)
    size_est_min: int | None = None
    subskills: list[str]
    parents: list[int]


class GraphEdge(BaseModel):
    source: int  # parent node id
    target: int  # child node id


class GraphView(BaseModel):
    track: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


@router.get("/graph", response_model=GraphView, operation_id="getGraph")
def get_graph() -> dict:
    """Nodes + parent edges of the ACTIVE track, with status, domain, tier and (for finished work) outcome."""
    sc = current_scope()
    return graph_view(sc.root, sc.track)
