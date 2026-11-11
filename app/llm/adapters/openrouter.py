import time

import httpx

from app.llm.adapters.util import parse_json
from app.llm.allowlist import check_free_model
from app.llm.types import LLMError, RateLimited, Request, Result, RetryableError

URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterAdapter:
    name = "openrouter"

    def __init__(self, api_key=lambda: "", url: str = URL, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.url = url
        self.transport = transport

    def complete(self, req: Request, model: str, timeout_s: float) -> Result:
        check_free_model(model)  # defense in depth; the router checks too, before counting
        key = self.api_key()
        if not key:
            raise RetryableError("OpenRouter API key not set in settings")  # lets the chain fall through to the claude_code fallback
        messages = list(req.messages)
        body: dict = {"model": model, "messages": messages}
        if req.schema is not None:
            messages.append({"role": "user", "content": "Respond with ONLY JSON matching this schema, no prose."})
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        try:
            with httpx.Client(transport=self.transport, timeout=timeout_s) as c:
                r = c.post(self.url, json=body, headers={"Authorization": f"Bearer {key}"})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RetryableError(f"openrouter transport error: {e}") from e
        if r.status_code == 429:
            raise RateLimited(f"openrouter HTTP 429: {r.text[:300]}")
        if r.status_code >= 500:
            raise RetryableError(f"openrouter HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code != 200:
            raise LLMError(f"openrouter HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError as e:
            raise RetryableError(f"openrouter returned non-JSON body: {r.text[:200]}") from e
        if "error" in data:  # OpenRouter sometimes returns 200 with an error body
            code = (data["error"] or {}).get("code")
            cls = RateLimited if code == 429 else RetryableError if code in (502, 503, 504, 408) else LLMError
            raise cls(f"openrouter error: {data['error']}")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RetryableError(f"openrouter malformed response: {str(data)[:200]}") from e
        u = data.get("usage") or {}
        return Result(
            text=text, json=parse_json(text) if req.schema is not None else None,
            usage={"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")},
            backend=self.name, model=data.get("model") or model, latency_ms=int((time.monotonic() - t0) * 1000),
        )
