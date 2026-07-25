"""Tests for library consolidation: directory filter, export, stats
cache, auto-created directories and MCP auth gating."""
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, stats  # noqa: E402


def _use_tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "data" / "uploads")


def _client(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _item(path, prompt="p", **params):
    return {
        "file": str(path), "filename": Path(path).name, "tool": "a1111",
        "prompt": prompt, "negative_prompt": "", "params": params,
        "error": None,
    }


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), "navy").save(path)
    return path


def test_directory_filter(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db.upsert_image(_item(_png(tmp_path / "a" / "1.png"), "alpha"))
    db.upsert_image(_item(_png(tmp_path / "b" / "2.png"), "beta"))

    d = client.get("/api/library", params={"directory": str(tmp_path / "a")}).json()
    assert d["total"] == 1 and d["items"][0]["prompt"] == "alpha"


def test_library_export(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db.upsert_image(_item(_png(tmp_path / "e.png"), "exported cat",
                          Sampler="Euler a", Seed="7"))

    r = client.get("/api/library/export", params={"format": "json"})
    assert r.status_code == 200
    assert "exported cat" in r.text

    r = client.get("/api/library/export", params={"format": "csv", "q": "exported"})
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert len(lines) == 2  # header + one row
    assert "Euler a" in lines[1]

    # Filters apply: no match -> only header
    r = client.get("/api/library/export", params={"format": "csv", "q": "zzz"})
    assert len(r.text.strip().splitlines()) == 1


def test_stats_cache_invalidation(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_image(_item(_png(tmp_path / "s1.png"), "one, two"))
    first = stats.collect(top=10)
    assert first["images"] == 1
    # Cached: same object returned for the same version+top
    assert stats.collect(top=10) is first
    # Mutation bumps the version -> cache invalidated
    db.upsert_image(_item(_png(tmp_path / "s2.png"), "one, three"))
    second = stats.collect(top=10)
    assert second is not first
    assert second["images"] == 2


def test_add_directory_creates_missing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    new_dir = tmp_path / "made" / "by" / "settings"
    r = client.post("/api/settings/directories", json={"path": str(new_dir)})
    assert r.status_code == 200
    assert new_dir.is_dir()
    assert str(new_dir) in r.json()["directories"]


def test_add_watch_creates_missing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    new_dir = tmp_path / "watched" / "new"
    r = client.post("/api/watches", json={"directory": str(new_dir)})
    assert r.status_code == 200
    assert new_dir.is_dir()


def test_legacy_job_result_endpoints_removed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/jobs/xyz/results").status_code == 404
    assert client.get("/api/jobs/xyz/export").status_code == 404


def test_mcp_auth_gate(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    from app import auth, mcp_server

    monkeypatch.setattr(mcp_server, "_unlocked", False)
    monkeypatch.delenv("GENSIGHT_MCP_PASSWORD", raising=False)

    # Auth disabled -> open
    assert mcp_server.require_auth() is None

    auth.set_credentials("admin", "secret1")
    try:
        assert "error" in mcp_server.require_auth()
        assert mcp_server.authorize("admin", "wrong") is False
        assert "error" in mcp_server.require_auth()
        assert mcp_server.authorize("admin", "secret1") is True
        assert mcp_server.require_auth() is None
    finally:
        auth.disable()
        monkeypatch.setattr(mcp_server, "_unlocked", False)


def test_mcp_auth_env_password(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    from app import auth, mcp_server

    monkeypatch.setattr(mcp_server, "_unlocked", False)
    auth.set_credentials("admin", "secret2")
    try:
        monkeypatch.setenv("GENSIGHT_MCP_PASSWORD", "secret2")
        assert mcp_server.require_auth() is None
    finally:
        auth.disable()
        monkeypatch.setattr(mcp_server, "_unlocked", False)