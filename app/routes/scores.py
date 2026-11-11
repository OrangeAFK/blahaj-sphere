from fastapi import APIRouter
from pydantic import BaseModel

from app.builder.context import load_framework
from app.datastore.cache import DataStore
from app.tracks.scope import current_scope

router = APIRouter()


class DomainScoreView(BaseModel):
    domain: str
    name: str
    tier: int
    mastery: float  # 0-100, SPEC §8.3
    n_window: int
    unlocked: bool  # tier exam available
    subskills_evidenced: list[str]


@router.get("/scores", response_model=list[DomainScoreView], operation_id="getScores")
def get_scores() -> list[dict]:
    """Per-domain tier and mastery of the ACTIVE track, from events.jsonl (the cache's scores table)."""
    import json

    sc = current_scope()
    fw = load_framework(sc.root, sc.track)
    names = {d.id: d.name for d in fw.domains} if fw else {}
    return [
        {"domain": d, "name": names.get(d, d), "tier": r["tier"], "mastery": round(r["mastery"], 1), "n_window": r["n_window"],
         "unlocked": bool(r["unlocked"]), "subskills_evidenced": json.loads(r["subskills_evidenced"])}
        for d, r in DataStore(sc.root, sc.track).scores().items()
    ]
