from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class Health(BaseModel):
    status: str


@router.get("/health", response_model=Health, operation_id="getHealth")
def health() -> Health:
    return Health(status="ok")
