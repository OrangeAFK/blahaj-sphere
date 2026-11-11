from fastapi import APIRouter

from app.llm.config import Entry
from app.llm.router import Router

router = APIRouter()


@router.get("/llm/usage", operation_id="getLlmUsage")
def get_llm_usage() -> dict:
    """Today's per-model request counts with their caps, per-backend totals with caps and remaining, and grades awaiting retry."""
    r = Router(adapters={})
    rows = r.store.usage_today()
    cfg = r.config
    usage = [{**row, "cap": cfg.cap_for(Entry(row["backend"], row["model"]))} for row in rows]
    backends = []
    for b in sorted({row["backend"] for row in rows} | set(cfg.backend_daily_caps)):
        used = r.store.backend_count(b)
        cap = cfg.backend_daily_caps.get(b)
        backends.append({"backend": b, "count": used, "cap": cap, "remaining": None if cap is None else max(0, cap - used)})
    return {"usage": usage, "backends": backends, "pending_grades": len(r.store.pending_grades())}
