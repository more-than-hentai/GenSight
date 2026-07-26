"""Audit log, group presets and worker-status tests."""
import io
import sys
import time
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit, config, db, group_presets  # noqa: E402


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


def _png_bytes(prompt="audit cat"):
    img = Image.new("RGB", (48, 48), "teal")
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", f"{prompt}\nSteps: 20, Sampler: Euler a")
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


# ------------------------------------------------- audit core


def test_record_and_query(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    audit.record("scan.start", actor="boss", target="/imgs",
                 detail={"workers": 4})
    audit.record("auth.login", actor="guest", ok=False,
                 detail={"result": "invalid credentials"})

    total, items = audit.query()
    assert total == 2
    assert items[0]["action"] == "auth.login"      # newest first
    assert items[0]["ok"] is False
    assert items[1]["detail"] == {"workers": 4}

    assert audit.query(action="scan")[0] == 1      # prefix match
    assert audit.query(actor="guest")[0] == 1
    assert audit.query(q="imgs")[0] == 1
    assert set(audit.actions()) == {"scan.start", "auth.login"}


def test_record_never_raises(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(audit.db, "connect", boom)
    audit.record("scan.start")  # must not propagate


def test_prune_keeps_newest(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    for i in range(20):
        audit.record("bulk.event", target=str(i))
    removed = audit.prune(max_rows=5)
    assert removed == 15
    total, items = audit.query()
    assert total == 5
    assert items[0]["target"] == "19"  # newest survived


# ------------------------------------------------- audit via API


def test_scan_and_settings_are_audited(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "imgs"
    imgdir.mkdir()
    (imgdir / "a.png").write_bytes(_png_bytes())

    client.post("/api/settings/directories", json={"path": str(imgdir)})
    client.post("/api/scan", json={"directory": str(imgdir)})
    for _ in range(50):
        if client.get("/api/library").json().get("total"):
            break
        time.sleep(0.1)
    time.sleep(0.3)  # let scan.finish land

    data = client.get("/api/audit").json()
    actions = [i["action"] for i in data["items"]]
    assert "settings.dir_add" in actions
    assert "scan.start" in actions
    assert "scan.finish" in actions
    finish = next(i for i in data["items"] if i["action"] == "scan.finish")
    assert finish["detail"]["processed"] == 1
    assert finish["detail"]["workers"] >= 1


def test_audit_records_actor_and_failed_login(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/login", json={"username": "boss", "password": "WRONG"})
    client.post("/api/settings/directories", json={"path": str(tmp_path / "d")})

    items = client.get("/api/audit").json()["items"]
    failed = next(i for i in items if i["action"] == "auth.login")
    assert failed["ok"] is False and failed["actor"] == "boss"
    dir_add = next(i for i in items if i["action"] == "settings.dir_add")
    assert dir_add["actor"] == "boss", "actor not attributed to the session"
    client.post("/api/auth/disable", json={"password": "root1"})


def test_audit_csv_export(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    audit.record("scan.start", actor="boss", target="/x")
    r = client.get("/api/audit/export")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("ts,actor,action")
    assert any("scan.start" in ln for ln in lines[1:])


def test_audit_is_admin_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/users",
                json={"username": "guest", "password": "pass1", "role": "user"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "guest", "password": "pass1"})
    assert client.get("/api/audit").status_code == 403
    assert client.get("/api/status/workers").status_code == 403
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/disable", json={"password": "root1"})


# ------------------------------------------------- worker status


def test_worker_status_shape(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    s = client.get("/api/status/workers").json()
    assert set(s) == {"scan", "watcher"}
    for key in ("running_jobs", "queued_jobs", "max_concurrent_jobs",
                "active_extract_workers", "active"):
        assert key in s["scan"], key
    assert "running" in s["watcher"] and "realtime" in s["watcher"]


# ------------------------------------------------- group presets


def test_presets_are_valid_rules():
    for name in group_presets.PRESETS:
        for rule in group_presets.entries(name):
            assert set(rule) == {"name", "pattern", "is_regex", "target"}
            assert rule["target"] in ("prompt", "filename", "model")
            if rule["is_regex"]:
                import re
                re.compile(rule["pattern"])  # must not raise


def test_install_standard_preset_and_apply(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    p = tmp_path / "imgs" / "x.png"
    p.parent.mkdir(parents=True)
    p.write_bytes(_png_bytes())
    db.upsert_image({
        "file": str(p), "filename": p.name, "tool": "a1111",
        "prompt": "a photorealistic portrait of a woman at night",
        "negative_prompt": "", "params": {}, "error": None,
    })

    r = client.post("/api/groups/install-preset?preset=standard")
    assert r.status_code == 200
    assert "portrait" in r.json()["installed"]
    assert len(db.list_groups()) == len(group_presets.STANDARD)

    applied = client.post("/api/groups/apply").json()["updated"]
    assert applied == 1
    assert db.get_image(str(p))["group_name"] == "portrait"  # first match wins

    actions = [i["action"] for i in client.get("/api/audit").json()["items"]]
    assert "group.install_preset" in actions


def test_install_example_preset(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/groups/install-preset?preset=example")
    assert r.status_code == 200
    names = {g["name"] for g in db.list_groups()}
    assert {"example-cat", "example-by-model", "example-dated-files"} <= names
    targets = {g["target"] for g in db.list_groups()}
    assert targets == {"prompt", "model", "filename"}


def test_unknown_preset_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/groups/install-preset?preset=nope").status_code == 400


def test_presets_are_idempotent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/groups/install-preset?preset=standard")
    first = len(db.list_groups())
    client.post("/api/groups/install-preset?preset=standard")
    assert len(db.list_groups()) == first, "re-install duplicated rules"
