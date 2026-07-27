"""Directory-scoped purge, the archive and its retention.

Covers the reported bug: unregistering a directory left its records
behind with no way to remove them, and the only cleanup path
(`cleanup_missing`) hard-deleted rows carrying expensive tagger/quality
state.
"""
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, purge  # noqa: E402


def _use_tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "data" / "uploads")
    purge._plans.clear()


def _client(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), "navy").save(path)
    return path


def _row(path: Path, **params):
    return {
        "file": str(path), "filename": path.name, "tool": "a1111",
        "prompt": "p", "negative_prompt": "", "params": params, "error": None,
    }


def _seed(path: Path, *, enrich=False) -> Path:
    _png(path)
    db.upsert_image(_row(path), phash="abcdabcdabcdabcd")
    if enrich:
        db.set_meta(str(path), rating=4, favorite=True, group_name="portrait")
        db.set_tags(str(path), ["1girl", "smile"], "PG-13")
        db.set_quality(str(path), 82.0, ["low_resolution"])
    return path


# ------------------------------------------------- path scoping


def test_scope_treats_underscore_literally(tmp_path, monkeypatch):
    """A LIKE prefix would let `a_b` match `axb` — fatal for a DELETE."""
    _use_tmp_data(tmp_path, monkeypatch)
    target = _seed(tmp_path / "a_b" / "in.png")
    decoy = _seed(tmp_path / "axb" / "out.png")

    total, items = db.query_images(directory=str(tmp_path / "a_b"))
    assert total == 1
    assert items[0]["file"] == str(target)
    assert db.get_image(str(decoy)) is not None


def test_scope_treats_percent_literally(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    _seed(tmp_path / "a%b" / "in.png")
    decoy = _seed(tmp_path / "other" / "out.png")

    total, _ = db.query_images(directory=str(tmp_path / "a%b"))
    assert total == 1, "percent in a directory name matched everything"
    assert db.get_image(str(decoy)) is not None


def test_scope_excludes_sibling_prefix(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    _seed(tmp_path / "img" / "a.png")
    _seed(tmp_path / "img2" / "b.png")
    total, items = db.query_images(directory=str(tmp_path / "img"))
    assert total == 1 and items[0]["filename"] == "a.png"


def test_scope_tolerates_trailing_slashes(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    _seed(tmp_path / "img" / "a.png")
    for spelling in (str(tmp_path / "img"), str(tmp_path / "img") + "/",
                     str(tmp_path / "img") + "///"):
        assert db.query_images(directory=spelling)[0] == 1, spelling


def test_scope_non_recursive_excludes_subdirectories(tmp_path, monkeypatch):
    """The sharpest trap: a non-recursive scan only visits immediate
    children, so its scope must not include deeper rows."""
    _use_tmp_data(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "top.png")
    _seed(root / "sub" / "deep.png")

    assert len(db.known_mtimes(str(root), recursive=True)) == 2
    shallow = db.known_mtimes(str(root), recursive=False)
    assert list(shallow) == [str(root / "top.png")]


def test_scope_rejects_empty_root():
    with pytest.raises(ValueError):
        db.path_scope("")


# ------------------------------------------------- purge preview


def test_preview_counts_and_prices_the_operation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png", enrich=True)
    _seed(root / "b.png")
    gone = _seed(root / "gone.png")
    gone.unlink()

    r = client.post("/api/admin/library/purge/preview",
                    json={"root": str(root), "mode": "all"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["total"] == 3
    assert p["present"] == 2 and p["missing"] == 1 and p["inaccessible"] == 0
    assert p["targets"] == 3
    assert p["files_deleted"] == 0
    assert p["at_risk"] == {"tagged": 1, "content_rated": 1,
                            "quality_analysed": 1, "grouped": 1,
                            "rated": 1, "favorite": 1}
    assert p["token"] and p["expires_at"] > time.time()
    # Nothing removed by previewing
    assert db.query_images(directory=str(root))[0] == 3


def test_preview_missing_mode_targets_only_gone_files(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "keep.png")
    gone = _seed(root / "gone.png")
    gone.unlink()

    p = client.post("/api/admin/library/purge/preview",
                    json={"root": str(root), "mode": "missing"}).json()
    assert p["targets"] == 1 and p["total"] == 2


def test_preview_reports_overlaps(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")
    client.post("/api/settings/directories", json={"path": str(root)})
    client.post("/api/watches", json={"directory": str(root)})

    p = client.post("/api/admin/library/purge/preview",
                    json={"root": str(root)}).json()
    assert str(root) in p["overlaps"]["registered_directories"]
    assert [w["directory"] for w in p["overlaps"]["watches"]] == [str(root)]
    assert p["overlaps"]["active_scans"] == []


def test_preview_refuses_filesystem_root_and_relative(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/admin/library/purge/preview",
                       json={"root": "/"}).status_code == 400
    assert client.post("/api/admin/library/purge/preview",
                       json={"root": "relative/path"}).status_code == 400
    assert client.post("/api/admin/library/purge/preview",
                       json={"root": str(tmp_path), "mode": "bogus"}
                       ).status_code == 400


# ------------------------------------------------- purge execute


def test_purge_archives_rows_and_leaves_files(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    a = _seed(root / "a.png", enrich=True)
    outside = _seed(tmp_path / "other" / "keep.png")

    token = client.post("/api/admin/library/purge/preview",
                        json={"root": str(root)}).json()["token"]
    r = client.post("/api/admin/library/purge", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archived"] == 1 and body["files_deleted"] == 0

    assert db.get_image(str(a)) is None          # gone from the library
    assert a.exists(), "purge must not touch files on disk"
    assert db.get_image(str(outside)) is not None  # scope respected
    assert db.get_archived(str(a)) is not None    # snapshot kept


def test_purge_token_is_single_use(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")
    token = client.post("/api/admin/library/purge/preview",
                        json={"root": str(root)}).json()["token"]
    assert client.post("/api/admin/library/purge",
                       json={"token": token}).status_code == 200
    assert client.post("/api/admin/library/purge",
                       json={"token": token}).status_code == 409


def test_whitespace_in_root_never_retargets_a_neighbour(tmp_path, monkeypatch):
    """`"/a/set "` and `"/a/set"` are different directories on POSIX.

    Trimming the request would purge the neighbour's records under a path
    the operator never named, so a padded root must resolve to itself.
    """
    client = _client(tmp_path, monkeypatch)
    victim = _seed(tmp_path / "set" / "victim.png")
    intended = _seed(tmp_path / "set " / "intended.png")

    p = client.post("/api/admin/library/purge/preview",
                    json={"root": f"{tmp_path}/set "}).json()
    assert p["root"] == f"{tmp_path}/set ", "the root was silently trimmed"
    assert p["targets"] == 1
    # Flagged so the UI can ask "did you mean this?" — the trailing space is
    # indistinguishable from a paste accident, and only the operator knows.
    assert p["padded_root"] is True

    client.post("/api/admin/library/purge", json={"token": p["token"]})
    assert db.has_image(str(victim)), "purged a directory that was not named"
    assert not db.has_image(str(intended))


def test_trailing_whitespace_matches_nothing_and_is_flagged(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _seed(tmp_path / "imgs" / "a.png")

    p = client.post("/api/admin/library/purge/preview",
                    json={"root": f"{tmp_path}/imgs "}).json()
    assert p["padded_root"] is True
    assert p["targets"] == 0, "whitespace must not silently match the real path"


def test_leading_whitespace_in_root_is_rejected(tmp_path, monkeypatch):
    """Leading space makes the path non-absolute, which is refused outright
    rather than quietly reinterpreted."""
    client = _client(tmp_path, monkeypatch)
    _seed(tmp_path / "imgs" / "a.png")

    r = client.post("/api/admin/library/purge/preview",
                    json={"root": f" {tmp_path}/imgs"})
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"]


def test_unused_plans_are_capped(tmp_path, monkeypatch):
    """Each plan holds every target path, so unexecuted previews must not
    accumulate for the whole TTL window."""
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")
    monkeypatch.setattr(purge, "MAX_PLANS", 3)

    tokens = [client.post("/api/admin/library/purge/preview",
                          json={"root": str(root)}).json()["token"]
              for _ in range(5)]

    assert len(purge._plans) == 3
    # The oldest are evicted; the newest still executes.
    assert client.post("/api/admin/library/purge",
                       json={"token": tokens[0]}).status_code == 409
    assert client.post("/api/admin/library/purge",
                       json={"token": tokens[-1]}).status_code == 200


def test_purge_rejects_stale_plan_after_library_changes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")
    token = client.post("/api/admin/library/purge/preview",
                        json={"root": str(root)}).json()["token"]

    _seed(root / "added-after-preview.png")  # library moves on

    r = client.post("/api/admin/library/purge", json={"token": token})
    assert r.status_code == 409
    assert "changed" in r.json()["detail"]
    assert db.query_images(directory=str(root))[0] == 2, "nothing was purged"


def test_purge_refuses_while_a_watch_is_enabled(tmp_path, monkeypatch):
    """An enabled watch re-ingests on a timer, so purging under it would be
    undone by the next sweep."""
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")
    db.add_watch(str(root), True, 30)

    token = client.post("/api/admin/library/purge/preview",
                        json={"root": str(root)}).json()["token"]
    r = client.post("/api/admin/library/purge", json={"token": token})
    assert r.status_code == 409
    assert "watch" in r.json()["detail"]
    assert db.query_images(directory=str(root))[0] == 1, "nothing was purged"


def test_purge_root_rejects_dot_segments(tmp_path, monkeypatch):
    """Folding "link/.." lexically can scope a different directory when the
    component is a symlink, so dot segments are refused outright."""
    client = _client(tmp_path, monkeypatch)
    _seed(tmp_path / "imgs" / "a.png")

    r = client.post("/api/admin/library/purge/preview",
                    json={"root": f"{tmp_path}/imgs/../imgs"})
    assert r.status_code == 400
    assert ".." in r.json()["detail"]


def test_missing_mode_aborts_when_a_file_came_back(tmp_path, monkeypatch):
    """A remount between preview and execute changes no database row, so the
    version check cannot see it — the files must be re-checked."""
    _use_tmp_data(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    img = _seed(root / "a.png")
    img.unlink()

    plan = purge.plan(str(root), mode="missing")
    assert plan["targets"] == 1

    _png(img)  # the file is back before the operator confirms

    with pytest.raises(purge.PurgeError, match="no longer"):
        purge.execute(plan["token"])
    assert db.has_image(str(img)), "archived a file that exists again"


def test_archive_aborts_when_the_library_moves_mid_transaction(tmp_path,
                                                               monkeypatch):
    """The version is re-checked after the write lock is taken, not only by
    the caller beforehand."""
    _use_tmp_data(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    img = _seed(root / "a.png")

    with pytest.raises(db.RevisionConflict):
        db.archive_rows([str(img)], "test", "batch",
                        expected_version=db.data_version() + 1)
    assert db.has_image(str(img)), "rows were archived despite the conflict"


def test_preview_captures_the_version_before_reading_rows(tmp_path,
                                                          monkeypatch):
    """A write landing while the preview is computed must invalidate it,
    rather than being folded into the plan's own version."""
    _use_tmp_data(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")

    real_classify = purge._classify

    def mutate_then_classify(paths):
        _seed(root / "landed-during-preview.png")
        return real_classify(paths)

    monkeypatch.setattr(purge, "_classify", mutate_then_classify)
    plan = purge.plan(str(root))
    monkeypatch.setattr(purge, "_classify", real_classify)

    with pytest.raises(purge.PurgeError, match="changed"):
        purge.execute(plan["token"])


def test_scope_covers_the_filesystem_root(tmp_path, monkeypatch):
    """A watch may be configured on "/" — scoping it must not raise, which
    would abort the whole watcher tick including retention pruning."""
    _use_tmp_data(tmp_path, monkeypatch)
    _seed(tmp_path / "imgs" / "a.png")

    clause, args = db.path_scope("/")
    n = db.connect().execute(
        f"SELECT COUNT(*) FROM images WHERE {clause}", args).fetchone()[0]
    assert n == 1
    with pytest.raises(ValueError):
        db.path_scope("   ")


def test_concurrent_execute_runs_the_plan_once(tmp_path, monkeypatch):
    """Two requests arriving together must not both archive.

    The token is claimed before validation, so the loser sees an unknown
    token rather than racing through to a second archive pass.
    """
    _use_tmp_data(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")
    plan = purge.plan(str(root))

    calls, gate = [], threading.Barrier(2, timeout=5)
    real_archive = db.archive_rows

    def counted(paths, reason, batch_id, **kw):
        calls.append(batch_id)
        return real_archive(paths, reason, batch_id, **kw)

    monkeypatch.setattr(db, "archive_rows", counted)

    outcomes: list[str] = []

    def run():
        try:
            gate.wait()
            purge.execute(plan["token"])
            outcomes.append("ok")
        except purge.PurgeError:
            outcomes.append("refused")
        except threading.BrokenBarrierError:  # pragma: no cover - timing only
            outcomes.append("barrier")

    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert sorted(outcomes) == ["ok", "refused"]
    assert len(calls) == 1, "the plan was archived more than once"


def test_restoring_a_batch_never_overwrites_a_newer_live_row(tmp_path,
                                                             monkeypatch):
    """Re-scanning a purged path revives and refreshes the row. Restoring
    the old batch afterwards must not clobber it with the snapshot."""
    _use_tmp_data(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    img = _seed(root / "a.png", enrich=True)

    plan = purge.plan(str(root))
    purge.execute(plan["token"])
    batch = db.archive_summary()["batches"][0]["batch_id"]

    # The path is live again, with newer content than the snapshot.
    db.upsert_image({"file": str(img), "filename": img.name,
                     "prompt": "NEWER", "negative_prompt": "",
                     "params": {}, "error": None})

    result = db.restore_archived(batch)
    assert result == {"restored": 0, "skipped": 1}
    row = db.get_image(str(img))
    assert row["prompt"] == "NEWER", "the newer live row was overwritten"
    assert db.get_archived(str(img)) is None, "the snapshot was left stranded"


def test_dot_segments_never_satisfy_an_allow_rule():
    """"/api/library/../admin/..." passes a prefix test even though it
    matches no route — authorization must not depend on who normalises it."""
    from app.main import _user_allowed

    assert _user_allowed("/api/library") is True
    assert _user_allowed("/api/library/../admin/library/purge") is False
    assert _user_allowed("/api/library/./item") is False


def test_missing_mode_aborts_when_rows_are_unreadable(tmp_path, monkeypatch):
    """An offline mount or unreadable parent must never be mistaken for
    deleted files."""
    _use_tmp_data(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")

    real_stat = purge.os.stat

    def flaky(path, *a, **kw):
        if str(path).endswith("a.png"):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(purge.os, "stat", flaky)
    plan = purge.plan(str(root), mode="missing")
    assert plan["inaccessible"] == 1 and plan["missing"] == 0

    with pytest.raises(purge.PurgeError, match="unreadable"):
        purge.execute(plan["token"])
    # Assert while still patched: monkeypatch.undo() would also revert
    # _use_tmp_data's DATA_DIR patch and query the real database.
    assert db.query_images(directory=str(root))[0] == 1


# ------------------------------------------------- archive lifecycle


def test_archive_restore_round_trip_preserves_enrichment(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    a = _seed(root / "a.png", enrich=True)
    before = db.get_image(str(a))

    token = client.post("/api/admin/library/purge/preview",
                        json={"root": str(root)}).json()["token"]
    batch = client.post("/api/admin/library/purge",
                        json={"token": token}).json()["batch_id"]

    r = client.post("/api/admin/library/archive/restore",
                    json={"batch_id": batch})
    assert r.status_code == 200 and r.json()["restored"] == 1

    after = db.get_image(str(a))
    for field in ("rating", "favorite", "group_name", "tags",
                  "content_rating", "quality_score", "quality_issues",
                  "phash", "prompt"):
        assert after[field] == before[field], field
    assert db.get_archived(str(a)) is None, "restored row left in the archive"


def test_rescan_revives_archived_enrichment(tmp_path, monkeypatch):
    """The point of archiving: re-scanning a purged path brings tags and
    quality back without another GPU pass."""
    _use_tmp_data(tmp_path, monkeypatch)
    from app.scanner import process_and_store

    root = tmp_path / "imgs"
    a = _seed(root / "a.png", enrich=True)
    plan = purge.plan(str(root))
    purge.execute(plan["token"])
    assert db.get_image(str(a)) is None

    process_and_store(a)
    revived = db.get_image(str(a))
    assert revived is not None
    assert revived["tags"] == ["1girl", "smile"]
    assert revived["content_rating"] == "PG-13"
    assert revived["quality_score"] == 82.0
    assert revived["rating"] == 4 and revived["group_name"] == "portrait"
    assert db.get_archived(str(a)) is None


def test_rescan_does_not_give_a_different_file_the_old_tags(tmp_path,
                                                            monkeypatch):
    """Enrichment is keyed by path, and paths get reused. A new image at a
    purged path must be tagged on its own merits, not inherit the old one's."""
    _use_tmp_data(tmp_path, monkeypatch)
    from app.scanner import process_and_store

    root = tmp_path / "imgs"
    a = _seed(root / "a.png", enrich=True)
    plan = purge.plan(str(root))
    purge.execute(plan["token"])

    # A visibly different image lands at the same name.
    a.unlink()
    Image.new("RGB", (200, 120), "orange").save(a)

    process_and_store(a)
    row = db.get_image(str(a))
    assert row is not None
    assert row["tags"] in (None, []), "inherited the previous file's tags"
    assert row["quality_score"] is None, "inherited the previous quality score"
    assert db.get_archived(str(a)) is None, "the stale snapshot was left behind"


def test_retention_prunes_only_expired(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    config.update_settings({"archive": {"retention_days": 30}})
    old = _seed(tmp_path / "imgs" / "old.png")
    new = _seed(tmp_path / "imgs" / "new.png")
    db.archive_rows([str(old)], "test", "batch-old")
    db.archive_rows([str(new)], "test", "batch-new")
    # Age one entry past the window
    conn = db.connect()
    with conn:
        conn.execute("UPDATE archived_images SET archived_at=? WHERE path=?",
                     (time.time() - 40 * 86400, str(old)))

    assert purge.prune_expired() == 1
    assert db.get_archived(str(old)) is None
    assert db.get_archived(str(new)) is not None


def test_retention_zero_keeps_everything(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    config.update_settings({"archive": {"retention_days": 0}})
    p = _seed(tmp_path / "imgs" / "a.png")
    db.archive_rows([str(p)], "test", "b1")
    conn = db.connect()
    with conn:
        conn.execute("UPDATE archived_images SET archived_at=?",
                     (time.time() - 9999 * 86400,))
    assert purge.retention_cutoff() is None
    assert purge.prune_expired() == 0
    assert db.get_archived(str(p)) is not None


def test_archive_prune_all_and_status(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    p = _seed(tmp_path / "imgs" / "a.png")
    db.archive_rows([str(p)], "test", "b1")

    status = client.get("/api/admin/library/archive").json()
    assert status["total"] == 1
    assert status["batches"][0]["batch_id"] == "b1"
    assert status["retention_days"] == 30

    r = client.post("/api/admin/library/archive/prune", json={"all": True})
    assert r.json()["removed"] == 1
    assert client.get("/api/admin/library/archive").json()["total"] == 0


def test_cleanup_missing_archives_instead_of_deleting(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    keep = _seed(tmp_path / "imgs" / "keep.png")
    gone = _seed(tmp_path / "imgs" / "gone.png", enrich=True)
    gone.unlink()

    r = client.post("/api/library/cleanup")
    assert r.json()["removed"] == 1
    assert db.get_image(str(gone)) is None
    assert db.get_image(str(keep)) is not None
    snapshot = db.get_archived(str(gone))
    assert snapshot is not None and snapshot["tags"] is not None, (
        "cleanup must preserve tagger output, not destroy it")


def test_cleanup_missing_keeps_unreadable_rows(tmp_path, monkeypatch):
    """Path.exists() used to treat a permission error as 'file gone'."""
    _use_tmp_data(tmp_path, monkeypatch)
    p = _seed(tmp_path / "imgs" / "a.png")
    import os as _os

    real_stat = _os.stat

    def flaky(path, *a, **kw):
        if str(path).endswith("a.png"):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(_os, "stat", flaky)
    assert db.cleanup_missing() == 0
    # Checked under the patch on purpose — undoing it here would also
    # revert the DATA_DIR patch and hit the real database.
    assert db.get_image(str(p)) is not None
    assert db.get_archived(str(p)) is None


# ------------------------------------------------- authorization


def test_every_admin_route_is_denied_to_restricted_users(tmp_path, monkeypatch):
    """Enumerated from the app, so a future /api/admin route cannot be
    added without this covering it."""
    client = _client(tmp_path, monkeypatch)
    from app.main import app

    paths = set()

    def walk(routes):
        for r in routes:
            p = getattr(r, "path", None)
            if p:
                paths.add(p)
            for attr in ("routes", "original_router"):
                o = getattr(r, attr, None)
                if o is not None:
                    walk(o.routes if hasattr(o, "routes") else o)

    walk(app.routes)
    admin_paths = sorted(p for p in paths if p.startswith("/api/admin"))
    assert admin_paths, "no /api/admin routes discovered — test is vacuous"

    client.post("/api/auth/setup", json={"username": "boss", "password": "root1"})
    client.post("/api/auth/users",
                json={"username": "guest", "password": "pass1", "role": "user"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "guest", "password": "pass1"})
    try:
        for path in admin_paths:
            for method in ("GET", "POST"):
                r = client.request(method, path, json={})
                assert r.status_code in (403, 405), (
                    f"{method} {path} -> {r.status_code} for role=user")
    finally:
        client.cookies.clear()
        client.post("/api/auth/login",
                    json={"username": "boss", "password": "root1"})
        client.post("/api/auth/disable", json={"password": "root1"})


def test_segment_aware_prefix_matching():
    """`/api/library-admin/x` must not inherit the /api/library allowance,
    and `/api/library/cleanup-all` must not evade the deny rule."""
    from app.main import _user_allowed

    assert _user_allowed("/api/library") is True
    assert _user_allowed("/api/library/item") is True
    assert _user_allowed("/api/library/cleanup") is False
    assert _user_allowed("/api/library/cleanup/all") is False
    assert _user_allowed("/api/library-admin/purge") is False
    assert _user_allowed("/api/libraryXYZ") is False
    assert _user_allowed("/api/admin/library/purge") is False


def test_purge_is_audited(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "imgs"
    _seed(root / "a.png")
    token = client.post("/api/admin/library/purge/preview",
                        json={"root": str(root)}).json()["token"]
    client.post("/api/admin/library/purge", json={"token": token})

    actions = [i["action"] for i in client.get("/api/audit").json()["items"]]
    assert "library.purge_preview" in actions
    assert "library.purge" in actions
