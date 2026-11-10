import json
from pathlib import Path

from app.main import app

out = Path(__file__).resolve().parent.parent / "ui" / "openapi.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(app.openapi(), indent=2), encoding="utf-8")
print(f"wrote {out}")
