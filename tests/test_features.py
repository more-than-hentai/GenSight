"""Tests for quality analysis, trash/organize and authentication."""
import sys
import time
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, files, quality  # noqa: E402


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


def _item(path, **params):
    return {
        "file": str(path), "filename": Path(path).name, "tool": "a1111",
        "prompt": "p", "negative_prompt": "", "params": params, "error": None,
    }


# ---------------------------------------------------------------- quality


def test_quality_sharp_image_scores_high(tmp_path):
    p = tmp_path / "sharp.png"
    Image.effect_noise((600, 600), 64).convert("RGB").save(p)
    score, issues = quality.analyze(str(p))
    assert score >= 80
    assert "blurry" not in issues


def test_quality_flat_dark_image_flags_issues(tmp_path):
    p = tmp_path / "flat.png"
    Image.new("RGB", (256, 256), (10, 10, 10)).save(p)
    score, issues = quality.analyze(str(p))
    assert {"blurry", "too_dark", "low_contrast", "low_resolution"} <= set(issues)
    assert score < 50


def test_quality_settings_flags():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.png"
        Image.effect_noise((700, 700), 64).convert("RGB").save(p)
        _, issues = quality.analyze(str(p), {"Steps": "6", "CFG scale": "20",
                                             "Model": "plainModel"})
        assert "low_steps" in issues and "cfg_too_high" in issues
        # Turbo models are exempt from the low-steps rule
        _, issues = quality.analyze(str(p), {"Steps": "6", "CFG scale": "1.0",
                                             "Model": "sdxl_turbo"})
        assert "low_steps" not in issues and "cfg_too_low" not in issues


def test_quality_api(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    p = tmp_path / "q.png"
    Image.new("RGB", (128, 128), (5, 5, 5)).save(p)
    db.upsert_image(_item(p))

    r = client.post("/api/quality/run", json={})
    assert r.status_code == 200
    for _ in range(50):
        s = client.get("/api/quality/status").json()
        if not s["job"] or s["job"]["status"] != "running":
            break
        time.sleep(0.1)
    assert s["job"]["status"] == "done"

    data = client.get("/api/library", params={"quality": "low"}).json()
    assert data["total"] == 1
    assert data["items"][0]["quality_score"] < 50
    assert "too_dark" in data["items"][0]["quality_issues"]


# ---------------------------------------------------------------- trash


def test_trash_restore_purge(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    p = tmp_path / "t.png"
    Image.new("RGB", (64, 64), "red").save(p)
    db.upsert_image(_item(p))
    db.set_meta(str(p), rating=5, favorite=True)

    # trash: file moves, library row disappears
    r = client.post("/api/trash", json={"path": str(p)})
    assert r.status_code == 200, r.text
    assert not p.exists()
    assert db.get_image(str(p)) is None
    entries = client.get("/api/trash").json()["items"]
    assert len(entries) == 1
    assert Path(entries[0]["trash_path"]).exists()

    # restore: file returns with rating/favorite intact
    r = client.post(f"/api/trash/{entries[0]['id']}/restore")
    assert r.status_code == 200
    assert p.exists()
    restored = db.get_image(str(p))
    assert restored["rating"] == 5 and restored["favorite"] is True
    assert client.get("/api/trash").json()["items"] == []

    # trash again and purge permanently
    client.post("/api/trash", json={"path": str(p)})
    entry = client.get("/api/trash").json()["items"][0]
    r = client.delete("/api/trash")
    assert r.json()["purged"] == 1
    assert not Path(entry["trash_path"]).exists()


def test_trash_thumbnail_is_servable(tmp_path, monkeypatch):
    """Regression: trashed files must remain viewable in the trash UI."""
    client = _client(tmp_path, monkeypatch)
    p = tmp_path / "tv.png"
    Image.new("RGB", (64, 64), "teal").save(p)
    db.upsert_image(_item(p))
    client.post("/api/trash", json={"path": str(p)})
    entry = client.get("/api/trash").json()["items"][0]
    r = client.get("/api/image", params={"path": entry["trash_path"], "thumb": "true"})
    assert r.status_code == 200


def test_analyze_upload_joins_library(tmp_path, monkeypatch):
    """Regression: drag & drop uploads must be persisted like scans."""
    import io as _io
    from PIL import PngImagePlugin as _png

    client = _client(tmp_path, monkeypatch)
    img = Image.new("RGB", (64, 64), "olive")
    info = _png.PngInfo()
    info.add_text("parameters", "uploaded cat\nSteps: 20, Sampler: Euler a")
    buf = _io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)

    r = client.post("/api/analyze",
                    files={"file": ("up.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200
    body = r.json()
    assert body["phash"]  # hashed like scanned files
    data = client.get("/api/library", params={"q": "uploaded cat"}).json()
    assert data["total"] == 1
    assert data["items"][0]["file"] == body["file"]


def test_trash_unknown_path(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/trash", json={"path": "/no/such.png"}).status_code == 404


def test_trash_missing_file_drops_stale_row(tmp_path, monkeypatch):
    """A file deleted outside the app must not create a phantom trash
    entry — the stale library row is removed instead."""
    client = _client(tmp_path, monkeypatch)
    p = tmp_path / "ghost.png"
    Image.new("RGB", (32, 32)).save(p)
    db.upsert_image(_item(p))
    p.unlink()

    r = client.post("/api/trash", json={"path": str(p)})
    assert r.status_code == 404
    assert db.get_image(str(p)) is None
    assert client.get("/api/trash").json()["items"] == []


def test_library_cleanup_missing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    keep = tmp_path / "keep.png"
    gone = tmp_path / "gone.png"
    for f in (keep, gone):
        Image.new("RGB", (32, 32)).save(f)
        db.upsert_image(_item(f))
    gone.unlink()

    r = client.post("/api/library/cleanup")
    assert r.json()["removed"] == 1
    assert db.get_image(str(keep)) and db.get_image(str(gone)) is None


# ---------------------------------------------------------------- organize


def test_organize_dry_run_and_apply(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    p = src_dir / "img.png"
    Image.new("RGB", (64, 64), "blue").save(p)
    db.upsert_image(_item(p, Model="coolModel"))

    target = tmp_path / "organized"
    body = {"target_root": str(target), "template": "{model}", "dry_run": True}
    plan = client.post("/api/organize", json=body).json()
    assert plan["count"] == 1
    assert plan["moves"][0]["to"].endswith("coolModel/img.png")
    assert p.exists()  # dry run does not move

    body["dry_run"] = False
    result = client.post("/api/organize", json=body).json()
    assert result["count"] == 1 and not result["errors"]
    moved = target / "coolModel" / "img.png"
    assert moved.exists() and not p.exists()
    assert db.get_image(str(moved))  # DB path updated
    assert db.get_image(str(p)) is None


def test_organize_rejects_bad_template(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/organize", json={
        "target_root": str(tmp_path), "template": "{bogus}", "dry_run": True,
    })
    assert r.status_code == 400


# ---------------------------------------------------------------- auth


def test_auth_full_flow(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    s = client.get("/api/auth/status").json()
    assert s["enabled"] is False and s["authenticated"] is True

    # enable (allowed without session while disabled), auto-login cookie set
    r = client.post("/api/auth/setup",
                    json={"username": "admin", "password": "secret1"})
    assert r.status_code == 200
    assert client.get("/api/library").status_code == 200  # session cookie

    # fresh client without cookie is locked out of the API...
    client.cookies.clear()
    assert client.get("/api/library").status_code == 401
    # ...but auth endpoints and static shell stay reachable
    assert client.get("/api/auth/status").status_code == 200

    # wrong then right password
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": "nope"}
                       ).status_code == 401
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": "secret1"}
                       ).status_code == 200
    assert client.get("/api/library").status_code == 200

    # logout locks it again
    client.post("/api/auth/logout")
    assert client.get("/api/library").status_code == 401

    # disable requires the password; afterwards everything is open
    assert client.post("/api/auth/disable",
                       json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/disable",
                       json={"password": "secret1"}).status_code == 200
    assert client.get("/api/library").status_code == 200


def test_settings_never_leak_auth_secrets(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "u", "password": "pass1"})
    s = client.get("/api/settings").json()
    assert "password_hash" not in s["auth"] and "salt" not in s["auth"]
    assert s["auth"]["enabled"] is True
    client.post("/api/auth/disable", json={"password": "pass1"})
