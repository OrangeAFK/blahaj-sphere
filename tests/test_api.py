from fastapi.testclient import TestClient

from app.main import create_app


def test_health():
    c = TestClient(create_app())
    assert c.get("/api/health").json() == {"status": "ok"}


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BLAHAJ_DATA_DIR", str(tmp_path))
    c = TestClient(create_app())
    assert c.get("/api/settings").json()["openrouter_api_key"] == ""
    body = {"openrouter_api_key": "k", "allow_library_to_free_models": True, "claude_model": "m"}
    assert c.put("/api/settings", json=body).json() == body
    assert (tmp_path / "settings.json").exists()
    assert c.get("/api/settings").json() == body
