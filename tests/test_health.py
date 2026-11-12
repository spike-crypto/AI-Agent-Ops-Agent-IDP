from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "broken"


def test_version_comes_from_render_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "VERSION_FILE", tmp_path / "missing.txt")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    assert main._version() == "abc123"


def test_version_file_wins(monkeypatch, tmp_path):
    f = tmp_path / "version.txt"
    f.write_text("from-file\n")
    monkeypatch.setattr(main, "VERSION_FILE", f)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    assert main._version() == "from-file"
