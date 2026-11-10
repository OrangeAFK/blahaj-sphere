"""Manual M1 live smoke test (NOT run by pytest): `uv run python -m scripts.smoke_llm`.

1. claude_code live call  2. openrouter/free live call
3. forced 429 on the first free model -> failover to the real openrouter/free
"""
from app.llm.config import parse_routing
from app.llm.router import Router, default_adapters
from app.llm.types import RetryableError

MSG = [{"role": "user", "content": "Reply with exactly the single word: pong"}]


class Forced429:
    """Wraps the real OpenRouter adapter but 429s the listed models (no network call for them)."""

    name = "openrouter"

    def __init__(self, real, bad):
        self.real, self.bad = real, set(bad)

    def complete(self, req, model, timeout_s):
        if model in self.bad:
            raise RetryableError(f"forced 429 for {model}")
        return self.real.complete(req, model, timeout_s)


def show(label, res, router):
    print(f"[{label}] backend={res.backend} model={res.model} usage={res.usage} latency_ms={res.latency_ms}")
    print(f"         text={res.text.strip()[:80]!r}")


def main():
    cfg = parse_routing({
        "timeout_s": {"claude_code": 120, "openrouter": 60},
        "tasks": {
            "smoke_claude": {"chain": [{"backend": "claude_code"}]},
            "smoke_free": {"chain": [{"backend": "openrouter", "model": "openrouter/free"}]},
            "smoke_failover": {"chain": [
                {"backend": "openrouter", "model": "google/gemma-4-31b-it:free"},
                {"backend": "openrouter", "model": "openrouter/free"},
            ]},
        },
    })
    ad = default_adapters()
    r = Router(cfg, adapters=ad)
    show("claude_code", r.complete("smoke_claude", MSG), r)
    show("openrouter/free", r.complete("smoke_free", MSG), r)
    ad["openrouter"] = Forced429(ad["openrouter"], ["google/gemma-4-31b-it:free"])
    show("failover", r.complete("smoke_failover", MSG), r)
    print("--- llm_calls (newest first)")
    for c in r.store.calls(6):
        print(c["task_type"], c["backend"], c["model"], c["status"], c["input_tokens"], c["output_tokens"], c["latency_ms"], (c["error"] or "")[:60])
    print("--- usage today:", r.store.usage_today())


if __name__ == "__main__":
    main()
