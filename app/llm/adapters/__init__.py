from typing import Protocol

from app.llm.types import Request, Result


class Adapter(Protocol):
    name: str

    def complete(self, req: Request, model: str, timeout_s: float) -> Result: ...
