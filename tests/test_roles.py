"""Role-based access control tests (admin vs restricted user)."""
import io
import sys
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, config, db  # noqa: E402


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


def _png_bytes():
    img = Image.new("RGB", (48, 48), "teal")
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "role test cat\nSteps: 20, Sampler: Euler a")
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def test_legacy_single_admin_migrates(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    salt, digest = auth.hash_password("oldpass")
    config.update_settings({"auth": {
        "enabled": True, "username": "olduser", "salt": salt,
        "password_hash": digest, "users": [],
    }})
    users = auth.get_users()
    assert users == [{"username": "olduser", "salt": salt,
                      "password_hash": digest, "role": "admin"}]
    assert auth.authenticate("olduser", "oldpass") == "admin"
    auth.disable()


def test_role_access_matrix(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    imgdir.mkdir()
    img = imgdir / "x.png"
    img.write_bytes(_png_bytes())

    # admin setup + scan an image while unrestricted
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    r = client.post("/api/scan", json={"directory": str(imgdir)})
    assert r.status_code == 200
    import time
    for _ in range(50):
        if client.get("/api/library").json().get("total"):
            break
        time.sleep(0.1)

    # admin adds a restricted user
    assert client.post("/api/auth/users", json={
        "username": "guest", "password": "pass1", "role": "user",
    }).status_code == 200

    # switch to the restricted user
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "guest", "password": "pass1"})
    status = client.get("/api/auth/status").json()
    assert status["role"] == "user"

    # allowed: library browse/patch, similar, stats, analyze, image, i18n
    lib = client.get("/api/library").json()
    assert lib["total"] == 1
    path = lib["items"][0]["file"]
    assert client.patch("/api/library/item",
                        json={"path": path, "rating": 4}).status_code == 200
    assert client.get("/api/stats/prompts").status_code == 200
    assert client.get("/api/image",
                      params={"path": path, "thumb": "true"}).status_code == 200
    up = client.post("/api/analyze",
                     files={"file": ("u.png", _png_bytes(), "image/png")})
    assert up.status_code == 200

    # denied: everything touching settings/paths/system state
    denied = [
        ("GET", "/api/settings", None),
        ("PUT", "/api/settings", {"page_size": 10}),
        ("POST", "/api/scan", {"directory": str(imgdir)}),
        ("GET", "/api/jobs", None),
        ("GET", "/api/gpus", None),
        ("GET", "/api/watches", None),
        ("POST", "/api/watches", {"directory": str(imgdir)}),
        ("GET", "/api/groups", None),
        ("POST", "/api/trash", {"path": path}),
        ("GET", "/api/trash", None),
        ("POST", "/api/organize",
         {"target_root": str(tmp_path), "dry_run": True}),
        ("POST", "/api/quality/run", {}),
        ("GET", "/api/tagger/status", None),
        ("POST", "/api/library/cleanup", None),
        ("GET", "/api/auth/users", None),
        ("POST", "/api/auth/users",
         {"username": "evil", "password": "evil1", "role": "admin"}),
        ("POST", "/api/auth/setup",
         {"username": "evil", "password": "evil1"}),
    ]
    for method, url, body in denied:
        r = client.request(method, url, json=body)
        assert r.status_code == 403, f"{method} {url} -> {r.status_code}"

    # cleanup: back to admin, disable
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "boss", "password": "root1"})
    assert client.post("/api/auth/disable",
                       json={"password": "root1"}).status_code == 200


def test_user_management_guards(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})

    # cannot delete yourself
    assert client.delete("/api/auth/users/boss").status_code == 400
    # cannot delete the last admin (even via another admin session)
    client.post("/api/auth/users", json={
        "username": "boss2", "password": "root2", "role": "admin"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "boss2", "password": "root2"})
    assert client.delete("/api/auth/users/boss").status_code == 200
    assert client.delete("/api/auth/users/boss2").status_code == 400  # self
    # unknown user -> 404
    assert client.delete("/api/auth/users/nobody").status_code == 404
    client.post("/api/auth/disable", json={"password": "root2"})


def test_disable_keeps_accounts(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    auth.set_credentials("boss", "root1")
    auth.add_user("guest", "pass1", "user")
    auth.disable()
    assert not auth.enabled()
    assert {u["username"] for u in auth.get_users()} == {"boss", "guest"}
    # settings API never exposes hashes even with users stored
    from app.routers.system import public_settings
    assert public_settings()["auth"] == {"enabled": False}
