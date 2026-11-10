from fastapi import APIRouter

from app.llm.router import Router

router = APIRouter()


@router.get("/llm/usage", operation_id="getLlmUsage")
def get_llm_usage() -> dict:
    """Today's per-model request counts and the number of grades awaiting retry."""
    r = Router(adapters={})
    return {"usage": r.store.usage_today(), "pending_grades": len(r.store.pending_grades())}
