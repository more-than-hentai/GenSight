"""Regression tests for the security findings from the Codex review of PR #3.

1. Restricted users could read any file under a configured/scanned root
   through /api/image (arbitrary file disclosure).
2. Replacing an account left its old sessions — and old role — alive.
3. /api/analyze persisted arbitrary bytes with no decode check or limit.
"""
import io
import sys
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, config  # noqa: E402


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


def _png_bytes(text="sec test"):
    img = Image.new("RGB", (48, 48), "teal")
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", f"{text}\nSteps: 20, Sampler: Euler a")
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def _seed_library(client, imgdir):
    """Scan one image so the library has an indexed entry."""
    import time

    imgdir.mkdir(parents=True, exist_ok=True)
    (imgdir / "indexed.png").write_bytes(_png_bytes())
    client.post("/api/settings/directories", json={"path": str(imgdir)})
    client.post("/api/scan", json={"directory": str(imgdir)})
    for _ in range(50):
        if client.get("/api/library").json().get("total"):
            break
        time.sleep(0.1)


# ------------------------------------------------- 1. file disclosure


def test_restricted_user_cannot_read_non_image_files(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    _seed_library(client, imgdir)

    secret = imgdir / ".env"
    secret.write_text("OPENAI_API_KEY=sk-secret\n")
    sibling = imgdir / "backup.sql"
    sibling.write_text("-- dump\n")

    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/users",
                json={"username": "guest", "password": "pass1", "role": "user"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "guest", "password": "pass1"})

    for target in (secret, sibling):
        r = client.get("/api/image", params={"path": str(target)})
        assert r.status_code == 403, f"{target.name} leaked: {r.status_code}"
        assert "sk-secret" not in r.text

    # the indexed image itself still works
    assert client.get(
        "/api/image", params={"path": str(imgdir / "indexed.png")}
    ).status_code == 200


def test_restricted_user_cannot_read_unindexed_image(tmp_path, monkeypatch):
    """Even an image file is off limits if it was never scanned — a
    guest must not be able to walk a scan root."""
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    _seed_library(client, imgdir)
    stray = imgdir / "not_scanned.png"
    stray.write_bytes(_png_bytes("stray"))

    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/users",
                json={"username": "guest", "password": "pass1", "role": "user"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "guest", "password": "pass1"})

    assert client.get("/api/image",
                      params={"path": str(stray)}).status_code == 403


def test_non_image_extension_blocked_even_for_admin(tmp_path, monkeypatch):
    """Defense in depth: the endpoint serves images, nothing else."""
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    _seed_library(client, imgdir)
    secret = imgdir / "id_rsa"
    secret.write_text("PRIVATE KEY")
    r = client.get("/api/image", params={"path": str(secret)})
    assert r.status_code == 403
    assert "PRIVATE KEY" not in r.text


def test_secret_with_image_extension_not_served(tmp_path, monkeypatch):
    """Round 2: a secret named like an image gets indexed (so scan
    counts stay honest) but must never be served — it never decoded."""
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    _seed_library(client, imgdir)
    evil = imgdir / "secrets.env.png"
    evil.write_text("OPENAI_API_KEY=sk-leak-via-name\n")
    client.post("/api/scan", json={"directory": str(imgdir)})
    import time
    for _ in range(50):
        if client.get("/api/library").json()["total"] >= 2:
            break
        time.sleep(0.1)

    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/users",
                json={"username": "guest", "password": "pass1", "role": "user"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "guest", "password": "pass1"})

    r = client.get("/api/image", params={"path": str(evil)})
    assert r.status_code == 403
    assert "sk-leak-via-name" not in r.text


def test_symlink_to_secret_is_refused(tmp_path, monkeypatch):
    """Round 2: serving opens with O_NOFOLLOW, so an indexed name that
    became a symlink cannot be used to read the target."""
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    _seed_library(client, imgdir)
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("TOP SECRET")

    indexed = imgdir / "indexed.png"
    swapped = imgdir / "swapped.png"
    swapped.write_bytes(_png_bytes("swap me"))
    client.post("/api/scan", json={"directory": str(imgdir)})
    import time
    for _ in range(50):
        if client.get("/api/library").json()["total"] >= 2:
            break
        time.sleep(0.1)

    # after indexing, replace the file with a symlink to the secret
    swapped.unlink()
    swapped.symlink_to(secret)

    r = client.get("/api/image", params={"path": str(swapped)})
    assert r.status_code in (403, 404), r.status_code
    assert "TOP SECRET" not in r.text
    # the untouched image still serves
    assert client.get("/api/image",
                      params={"path": str(indexed)}).status_code == 200


# ------------------------------------------------- 2. session revocation


def test_demotion_revokes_existing_session(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/users",
                json={"username": "mole", "password": "pass1", "role": "admin"})

    from fastapi.testclient import TestClient
    from app.main import app

    mole = TestClient(app, raise_server_exceptions=False)
    mole.post("/api/auth/login", json={"username": "mole", "password": "pass1"})
    assert mole.get("/api/settings").status_code == 200

    # demote via replace -> the old admin session must die
    client.post("/api/auth/users",
                json={"username": "mole", "password": "pass2", "role": "user"})
    assert mole.get("/api/settings").status_code == 401


def test_password_reset_revokes_sessions(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/users",
                json={"username": "guest", "password": "pass1", "role": "user"})

    from fastapi.testclient import TestClient
    from app.main import app

    guest = TestClient(app, raise_server_exceptions=False)
    guest.post("/api/auth/login", json={"username": "guest", "password": "pass1"})
    assert guest.get("/api/library").status_code == 200

    client.post("/api/auth/users",
                json={"username": "guest", "password": "newpass", "role": "user"})
    assert guest.get("/api/library").status_code == 401


def test_admin_self_password_change_keeps_session(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    r = client.post("/api/auth/users",
                    json={"username": "boss", "password": "root2", "role": "admin"})
    assert r.status_code == 200
    # cookie was re-issued, so the admin stays signed in
    assert client.get("/api/settings").status_code == 200
    client.post("/api/auth/disable", json={"password": "root2"})


def test_cannot_demote_last_admin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    r = client.post("/api/auth/users",
                    json={"username": "boss", "password": "root1", "role": "user"})
    assert r.status_code == 400
    assert auth.find_user("boss")["role"] == "admin"


def test_session_from_racing_login_is_rejected(tmp_path, monkeypatch):
    """Round 2: a login that verified the OLD record but inserts its
    session after revoke_sessions() must still be invalid."""
    _use_tmp_data(tmp_path, monkeypatch)
    auth.set_credentials("boss", "root1")
    auth.add_user("mole", "pass1", "admin")

    # Simulate the race: capture the pre-change snapshot, mutate the
    # account, then insert the session the racing login would have made.
    snapshot = auth._account_snapshot("mole", "pass1")
    assert snapshot == ("admin", 1)
    auth.add_user("mole", "pass2", "user")  # concurrent demotion

    import secrets as _secrets
    import time as _time
    token = _secrets.token_urlsafe(16)
    with auth._lock:
        auth._sessions[token] = ("mole", snapshot[0], snapshot[1],
                                 _time.time() + auth.SESSION_TTL)

    assert auth.session_info(token) is None, "stale-version session accepted"
    auth.disable()


# ------------------------------------------------- 3. upload hardening


def test_analyze_rejects_non_image_content(tmp_path, monkeypatch):
    """An .png extension on arbitrary bytes must not create a file."""
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/analyze",
                    files={"file": ("evil.png", b"\x00" * 4096, "image/png")})
    assert r.status_code == 415
    leftovers = list((tmp_path / "data" / "uploads").glob("*")) \
        if (tmp_path / "data" / "uploads").exists() else []
    assert leftovers == [], f"junk persisted: {leftovers}"


def test_analyze_rejects_empty_file(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/analyze", files={"file": ("empty.png", b"", "image/png")})
    assert r.status_code == 400


def test_analyze_accepts_real_image(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/analyze",
                    files={"file": ("ok.png", _png_bytes("upload ok"), "image/png")})
    assert r.status_code == 200
    assert r.json()["prompt"] == "upload ok"


def test_analyze_rejects_valid_header_with_junk_payload(tmp_path, monkeypatch):
    """Round 2: verify() alone passes a truncated/corrupt payload; the
    full decode must reject it."""
    client = _client(tmp_path, monkeypatch)
    good = _png_bytes("truncate me")
    truncated = good[: len(good) // 2]  # header intact, pixel data cut
    r = client.post("/api/analyze",
                    files={"file": ("t.png", truncated, "image/png")})
    assert r.status_code == 415
    updir = tmp_path / "data" / "uploads"
    assert not (list(updir.glob("*")) if updir.exists() else [])


def test_analyze_rejects_decompression_bomb(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.routers import scan as scan_router

    monkeypatch.setattr(scan_router, "MAX_UPLOAD_PIXELS", 1000)
    r = client.post("/api/analyze",
                    files={"file": ("big.png", _png_bytes(), "image/png")})
    assert r.status_code == 413


def test_analyze_enforces_storage_quota(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.routers import scan as scan_router

    payload = _png_bytes("quota")
    assert client.post("/api/analyze",
                       files={"file": ("q1.png", payload, "image/png")}
                       ).status_code == 200
    monkeypatch.setattr(scan_router, "UPLOAD_DIR_QUOTA_BYTES", 1)
    r = client.post("/api/analyze",
                    files={"file": ("q2.png", payload, "image/png")})
    assert r.status_code == 507


def test_analyze_rate_limited(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.routers import scan as scan_router

    monkeypatch.setattr(scan_router, "UPLOAD_RATE_LIMIT", 3)
    scan_router._upload_hits.clear()

    payload = _png_bytes("burst")
    codes = [
        client.post("/api/analyze",
                    files={"file": (f"b{i}.png", payload, "image/png")}).status_code
        for i in range(5)
    ]
    assert codes.count(200) == 3
    assert codes.count(429) == 2
    scan_router._upload_hits.clear()
