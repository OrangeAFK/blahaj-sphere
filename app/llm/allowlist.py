from app.llm.types import ModelNotAllowed


def is_free_model(model_id: str) -> bool:
    return model_id.endswith(":free") or model_id == "openrouter/free"


def check_free_model(model_id: str) -> None:
    if not is_free_model(model_id):
        raise ModelNotAllowed(f"OpenRouter model {model_id!r} is not free (must end ':free' or be 'openrouter/free')")
