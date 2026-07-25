"""API tests for scan, analyze and image-serving endpoints."""
import io
import json
import sys
import time
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _client(tmp_path, monkeypatch):
    """TestClient with an isolated data directory."""
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "data" / "uploads")

    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _a1111_png_bytes() -> bytes:
    img = Image.new("RGB", (64, 64), "navy")
    info = PngImagePlugin.PngInfo()
    info.add_text(
        "parameters",
        "a cat\nNegative prompt: dog\nSteps: 20, Sampler: Euler a, CFG scale: 7, Seed: 1",
    )
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def test_scan_unregistered_directory(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    imgdir.mkdir()
    (imgdir / "x.png").write_bytes(_a1111_png_bytes())

    r = client.post("/api/scan", json={"directory": str(imgdir), "workers": 2})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    for _ in range(50):
        s = client.get(f"/api/jobs/{job_id}").json()
        if s["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert s["status"] == "done"
    assert s["processed"] == 1

    results = client.get("/api/library", params={"directory": str(imgdir)}).json()
    assert results["total"] == 1
    assert results["items"][0]["prompt"] == "a cat"

    # Files in a scanned (unregistered) directory are servable
    r = client.get("/api/image", params={"path": str(imgdir / "x.png")})
    assert r.status_code == 200


def test_scan_missing_directory(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/scan", json={"directory": "/nonexistent/nowhere"})
    assert r.status_code == 400


def test_analyze_upload(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/analyze",
        files={"file": ("sample.png", _a1111_png_bytes(), "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool"] == "a1111"
    assert body["prompt"] == "a cat"
    assert body["uploaded"] is True

    # Uploaded file must be servable (thumbnail too)
    r = client.get("/api/image", params={"path": body["file"], "thumb": "true"})
    assert r.status_code == 200


def test_analyze_rejects_bad_type(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/analyze", files={"file": ("evil.sh", b"#!/bin/sh", "text/plain")})
    assert r.status_code == 415


def test_image_path_outside_roots(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/image", params={"path": "/etc/passwd"})
    assert r.status_code == 403


def test_thumbnail_of_corrupt_image(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs2"
    imgdir.mkdir()
    bad = imgdir / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")
    client.post("/api/scan", json={"directory": str(imgdir)})
    time.sleep(0.3)
    r = client.get("/api/image", params={"path": str(bad), "thumb": "true"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


def test_export_bad_format(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/library/export", params={"format": "xml"})
    assert r.status_code == 400
