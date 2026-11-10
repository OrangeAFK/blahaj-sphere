import json
import re


def flatten_messages(messages: list[dict[str, str]], schema: dict | None) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "user")
        parts.append(m["content"] if role == "user" and len(messages) == 1 else f"[{role}]\n{m['content']}")
    if schema is not None:
        parts.append("Respond with ONLY a JSON value matching this JSON Schema, no prose, no code fences:\n" + json.dumps(schema))
    return "\n\n".join(parts)


def parse_json(text: str):
    """Parse JSON from model text, tolerating code fences or surrounding prose."""
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i : j + 1])
            except json.JSONDecodeError:
                continue
    return None
