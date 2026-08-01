"""Tests for multi-level sort, content rating, and quality fixes."""
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, quality, scanner, tagger  # noqa: E402


def _use_tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "data" / "uploads")


def _item(path, prompt="p", **params):
    return {
        "file": str(path), "filename": Path(path).name, "tool": "a1111",
        "prompt": prompt, "negative_prompt": "", "params": params,
        "error": None,
    }


def _png(path, color="navy"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path)
    return path


# ------------------------------------------------------------- sort


def test_multi_level_sort(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    a = _png(tmp_path / "a.png")
    b = _png(tmp_path / "b.png")
    c = _png(tmp_path / "c.png")
    for p in (a, b, c):
        db.upsert_image(_item(p))
    db.set_meta(str(a), rating=3)
    db.set_meta(str(b), rating=5)
    db.set_meta(str(c), rating=3)

    # 1st: rating desc, 2nd: name asc -> b, a, c
    _, items = db.query_images(sort="rating,name")
    assert [i["filename"] for i in items] == ["b.png", "a.png", "c.png"]
    # 2nd level flipped -> b, c, a
    _, items = db.query_images(sort="rating,name_desc")
    assert [i["filename"] for i in items] == ["b.png", "c.png", "a.png"]


def test_sort_by_file_mtime(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    import os

    old = _png(tmp_path / "old.png")
    new = _png(tmp_path / "new.png")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    # Insert oldest-file LAST so scanned_at order differs from mtime order
    db.upsert_image(_item(new))
    db.upsert_image(_item(old))

    _, items = db.query_images(sort="mtime_desc")
    assert items[0]["filename"] == "new.png"
    _, items = db.query_images(sort="mtime_asc")
    assert items[0]["filename"] == "old.png"


def test_sort_rejects_unknown_keys(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_image(_item(_png(tmp_path / "x.png")))
    # Injection / unknown keys fall back to the default order safely
    total, items = db.query_images(sort="drop table;,bogus")
    assert total == 1 and items


# ------------------------------------------------------------- rating


def test_content_rating_filter(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    safe = _png(tmp_path / "safe.png")
    nsfw = _png(tmp_path / "nsfw.png")
    plain = _png(tmp_path / "plain.png")
    for p in (safe, nsfw, plain):
        db.upsert_image(_item(p))
    db.set_tags(str(safe), ["1girl", "smile"], "PG")
    db.set_tags(str(nsfw), ["1girl"], "X")

    total, items = db.query_images(content_rating="X")
    assert total == 1 and items[0]["filename"] == "nsfw.png"
    total, _ = db.query_images(content_rating="PG")
    assert total == 1
    total, _ = db.query_images(content_rating="unrated")
    assert total == 1  # plain.png


def test_tagger_extract_predictions():
    names = ["general", "sensitive", "questionable", "explicit",
             "1girl", "smile", "hatsune miku"]
    categories = [9, 9, 9, 9, 0, 0, 4]
    probs = [0.05, 0.15, 0.7, 0.1, 0.9, 0.4, 0.95]
    tags, rating = tagger.extract_predictions(probs, names, categories)
    assert rating == "R"  # questionable wins
    assert "1girl" in tags and "smile" in tags
    assert "character:hatsune miku" in tags

    # Below thresholds -> excluded
    tags, rating = tagger.extract_predictions(
        [0.9, 0.0, 0.0, 0.0, 0.1, 0.1, 0.5], names, categories)
    assert rating == "PG" and tags == []


# ------------------------------------------------------------- quality


def test_quality_failure_leaves_pending_queue(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG not a real image")
    db.upsert_image(_item(bad))
    assert len(db.quality_pending_paths()) == 1

    mgr = quality.QualityManager()
    mgr.run()
    for _ in range(50):
        if mgr.job.status != "running":
            break
        time.sleep(0.05)
    assert mgr.job.status == "done" and mgr.job.errors == 1
    # Failed file is marked, not retried forever
    assert db.quality_pending_paths() == []
    item = db.get_image(str(bad))
    assert item["quality_issues"] == ["analysis_failed"]


def test_auto_quality_on_ingest(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    config.update_settings({"quality": {"auto": True}})
    p = _png(tmp_path / "auto.png", "black")
    scanner.process_and_store(p)
    item = db.get_image(str(p))
    assert item["quality_score"] is not None
    assert "too_dark" in item["quality_issues"]


# ------------------------------------------------- auto tagging after ingest
#
# autorun() sits in the ingest path — a scan job's finally block and every
# watch sweep — so what matters is that it obeys the setting and that it can
# never turn a tagging problem into a failed scan.

def _tmp_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")


def test_autorun_is_off_by_default(tmp_path, monkeypatch):
    _tmp_settings(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(tagger.tagger_manager, "run",
                        lambda *a, **k: calls.append(1) or {"total": 0})
    tagger.tagger_manager.autorun()
    assert calls == [], "tagging must not start unless the operator enabled it"


def test_autorun_starts_a_batch_when_enabled(tmp_path, monkeypatch):
    _tmp_settings(tmp_path, monkeypatch)
    config.update_settings({"tagger": {"auto": True}})
    calls = []

    def fake_run(*a, **k):
        calls.append(1)
        return {"total": 3}

    monkeypatch.setattr(tagger.tagger_manager, "run", fake_run)
    tagger.tagger_manager.autorun()
    assert calls == [1]


def test_autorun_swallows_every_refusal(tmp_path, monkeypatch):
    """"already running", "no untagged images" and a missing onnxruntime are
    all normal here — none of them may propagate into a scan or a watch tick."""
    _tmp_settings(tmp_path, monkeypatch)
    config.update_settings({"tagger": {"auto": True}})
    for boom in (RuntimeError("tagging already running"),
                 RuntimeError("no untagged images"),
                 tagger.TaggerUnavailable(tagger.INSTALL_HINT),
                 OSError("disk went away")):
        def fake_run(*a, _e=boom, **k):
            raise _e

        monkeypatch.setattr(tagger.tagger_manager, "run", fake_run)
        tagger.tagger_manager.autorun()   # must not raise
