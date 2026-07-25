"""Tests for the SQLite library, similarity, groups, stats and new APIs."""
import sys
import time
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, imghash, stats  # noqa: E402


def _use_tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "data" / "uploads")


def _fake_item(path, prompt="a cat, masterpiece", tool="a1111", **kw):
    return {
        "file": str(path), "filename": Path(path).name, "tool": tool,
        "prompt": prompt, "negative_prompt": kw.get("negative", "lowres"),
        "params": kw.get("params", {"Model": "modelA", "Sampler": "Euler a"}),
        "error": None,
    }


def _png(tmp_path, name, color):
    p = tmp_path / name
    Image.new("RGB", (64, 64), color).save(p)
    return p


# ---------------------------------------------------------------- db core


def test_upsert_preserves_user_meta(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    p = _png(tmp_path, "a.png", "red")
    db.upsert_image(_fake_item(p), phash="ff00ff00ff00ff00")
    db.set_meta(str(p), rating=4, favorite=True, group_name="test")

    # Re-scan (upsert again) must not clobber rating/favorite/group
    db.upsert_image(_fake_item(p, prompt="updated prompt"), phash=None)
    item = db.get_image(str(p))
    assert item["prompt"] == "updated prompt"
    assert item["rating"] == 4
    assert item["favorite"] is True
    assert item["group_name"] == "test"
    assert item["phash"] == "ff00ff00ff00ff00"  # COALESCE keeps old hash


def test_query_filters(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    for i, (prompt, tool) in enumerate(
        [("a cat", "a1111"), ("a dog", "comfyui"), ("a bird", "a1111")]
    ):
        db.upsert_image(_fake_item(_png(tmp_path, f"q{i}.png", "blue"), prompt, tool))
    db.set_meta(str(tmp_path / "q0.png"), favorite=True, rating=5)

    total, items = db.query_images(q="cat")
    assert total == 1 and items[0]["prompt"] == "a cat"
    total, _ = db.query_images(tool="a1111")
    assert total == 2
    total, _ = db.query_images(favorite=True)
    assert total == 1
    total, _ = db.query_images(min_rating=5)
    assert total == 1


def test_similar_and_duplicates(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    # Two visually identical images, one different
    p1 = _png(tmp_path, "s1.png", "white")
    p2 = _png(tmp_path, "s2.png", "white")
    h = imghash.dhash(p1)
    db.upsert_image(_fake_item(p1), phash=h)
    db.upsert_image(_fake_item(p2), phash=imghash.dhash(p2))
    # Solid-color images all dhash to 0, so give s3 a distinct fake hash
    db.upsert_image(_fake_item(_png(tmp_path, "s3.png", "black")),
                    phash="ffffffffffffffff")

    sim = db.similar_images(str(p1), max_distance=4)
    assert [s["filename"] for s in sim] == ["s2.png"]
    assert sim[0]["distance"] == 0

    dupes = db.duplicate_groups()
    assert len(dupes) == 1 and dupes[0]["count"] == 2


def test_groups_apply(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_image(_fake_item(_png(tmp_path, "g1.png", "red"), "1girl, blue dress"))
    db.upsert_image(_fake_item(_png(tmp_path, "g2.png", "red"), "landscape, mountain"))
    db.add_group("portrait", "1girl", target="prompt")
    db.add_group("scenery", r"landscape|mountain", is_regex=True, target="prompt")

    assert db.apply_groups() == 2
    assert db.get_image(str(tmp_path / "g1.png"))["group_name"] == "portrait"
    assert db.get_image(str(tmp_path / "g2.png"))["group_name"] == "scenery"
    # Without overwrite a second run changes nothing
    assert db.apply_groups() == 0


def test_hamming():
    assert imghash.hamming("00", "00") == 0
    assert imghash.hamming("00", "ff") == 8
    assert imghash.hamming("f0f0", "0f0f") == 16


# ---------------------------------------------------------------- stats


def test_tokenize_weights_and_split():
    tokens = list(stats.tokenize("(masterpiece:1.2), best quality\n[lowres], BREAK, "))
    assert tokens == ["masterpiece", "best quality", "lowres"]


def test_collect(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    for i in range(3):
        db.upsert_image(
            _fake_item(_png(tmp_path, f"c{i}.png", "green"), "a cat, cute")
        )
    s = stats.collect(top=5)
    assert s["images"] == 3
    assert s["positive"][0] == {"token": "a cat", "count": 3}
    assert s["models"][0]["token"] == "modelA"


# ---------------------------------------------------------------- API


def _client(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_library_api_flow(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    imgdir = tmp_path / "libimgs"
    imgdir.mkdir()
    img = imgdir / "x.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "wonderful cat\nSteps: 20, Sampler: Euler a, Seed: 1")
    Image.new("RGB", (48, 48), "purple").save(img, pnginfo=info)

    client.post("/api/scan", json={"directory": str(imgdir)})
    for _ in range(50):
        if client.get("/api/library").json()["total"]:
            break
        time.sleep(0.1)

    data = client.get("/api/library", params={"q": "wonderful"}).json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["tool"] == "a1111" and item["phash"]

    r = client.patch(
        "/api/library/item",
        json={"path": item["file"], "rating": 3, "favorite": True},
    )
    assert r.json()["rating"] == 3 and r.json()["favorite"] is True

    assert client.get("/api/stats/prompts").json()["images"] == 1
    assert client.get("/api/library/summary").json()["total"] == 1


def test_watches_api(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    d = tmp_path / "watchme"
    d.mkdir()
    w = client.post(
        "/api/watches",
        json={"directory": str(d), "poll_interval": 10},
    ).json()
    assert w["directory"] == str(d)
    assert client.get("/api/watches").json()["watches"][0]["poll_interval"] == 10
    client.patch(f"/api/watches/{w['id']}", json={"enabled": False})
    assert client.get("/api/watches").json()["watches"][0]["enabled"] == 0
    client.delete(f"/api/watches/{w['id']}")
    assert client.get("/api/watches").json()["watches"] == []
    # Nonexistent directory rejected
    assert client.post("/api/watches", json={"directory": "/no/such/dir"}).status_code == 400


def test_groups_api_rejects_bad_regex(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/groups",
        json={"name": "bad", "pattern": "([", "is_regex": True},
    )
    assert r.status_code == 400


def test_tagger_unavailable_or_conflict(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    status = client.get("/api/tagger/status").json()
    assert "available" in status
    r = client.post("/api/tagger/run", json={})
    # Without ML deps -> 409 unavailable; with deps but empty library -> 409 too
    assert r.status_code == 409
