"""Accounts moved from settings.json into the SQLite users table, plus
the atomic-write guarantees Codex's review called for: version bumps
computed in SQL (not pre-read then written) and a last-admin guard
enforced inside the write itself so two concurrent admin removals
cannot both slip past a Python-level check."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402


def _use_tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")


def test_upsert_and_get(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    v1 = db.upsert_user("boss", "salt1", "hash1", "admin")
    assert v1 == 1
    u = db.get_user("boss")
    assert u == {"username": "boss", "salt": "salt1", "password_hash": "hash1",
                "role": "admin", "version": 1}


def test_upsert_increments_version_on_replace(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("boss", "salt1", "hash1", "admin")
    # Password reset, same role — must not trip the last-admin guard,
    # which only fires on an admin -> non-admin role change.
    v2 = db.upsert_user("boss", "salt2", "hash2", "admin")
    assert v2 == 2
    u = db.get_user("boss")
    assert u["salt"] == "salt2" and u["role"] == "admin" and u["version"] == 2


def test_list_users_sorted(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("zed", "s", "h", "user")
    db.upsert_user("amy", "s", "h", "admin")
    assert [u["username"] for u in db.list_users()] == ["amy", "zed"]


def test_delete_user_row(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("guest", "s", "h", "user")  # non-admin: no last-admin guard
    assert db.delete_user_row("guest") == "deleted"
    assert db.get_user("guest") is None
    assert db.delete_user_row("guest") == "not_found"  # already gone


def test_users_survive_process_restart_simulation(tmp_path, monkeypatch):
    """Accounts are in the DB file, not memory — a fresh connect (as a
    restarted process would do) must still see them."""
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("boss", "s", "h", "admin")
    # Drop this thread's cached connection to force a fresh open, the
    # same file db.connect() would reopen after a real restart.
    db._local.conns = {}
    assert db.get_user("boss") is not None


# ------------------------------------------------- last-admin guard


def test_delete_refuses_the_last_admin(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("boss", "s", "h", "admin")
    assert db.delete_user_row("boss") == "last_admin"
    assert db.get_user("boss") is not None


def test_delete_allows_removing_one_of_two_admins(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("boss", "s", "h", "admin")
    db.upsert_user("boss2", "s", "h", "admin")
    assert db.delete_user_row("boss") == "deleted"
    # now boss2 is the last admin
    assert db.delete_user_row("boss2") == "last_admin"


def test_upsert_refuses_demoting_the_last_admin(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("boss", "s", "h", "admin")
    result = db.upsert_user("boss", "s2", "h2", "user")
    assert result is None
    u = db.get_user("boss")
    assert u["role"] == "admin", "refused write must not have applied"
    assert u["salt"] == "s", "refused write must not have changed the row at all"


def test_upsert_allows_demoting_when_another_admin_exists(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("boss", "s", "h", "admin")
    db.upsert_user("boss2", "s", "h", "admin")
    result = db.upsert_user("boss", "s2", "h2", "user")
    assert result is not None
    assert db.get_user("boss")["role"] == "user"


def test_concurrent_admin_deletions_leave_exactly_one_admin(tmp_path, monkeypatch):
    """The race Codex flagged: two admins deleting each other at once
    must not both succeed and leave zero admins."""
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("a1", "s", "h", "admin")
    db.upsert_user("a2", "s", "h", "admin")

    results = []
    start = threading.Barrier(2)

    def delete(name, other):
        start.wait()
        results.append(db.delete_user_row(name))

    t1 = threading.Thread(target=delete, args=("a1", "a2"))
    t2 = threading.Thread(target=delete, args=("a2", "a1"))
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)

    remaining_admins = [u for u in db.list_users() if u["role"] == "admin"]
    assert len(remaining_admins) == 1, (
        f"expected exactly one admin left, found {remaining_admins}; "
        f"delete results were {results}"
    )
    assert sorted(results) == ["deleted", "last_admin"]


def test_concurrent_writes_to_same_username_do_not_lose_the_version_bump(
    tmp_path, monkeypatch
):
    """The other race Codex flagged: two concurrent password changes on
    the SAME account must not both compute "current + 1" from a stale
    pre-read and collide on the same version number."""
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("boss", "s0", "h0", "admin")

    versions = []
    start = threading.Barrier(2)

    def bump(salt):
        start.wait()
        v = db.upsert_user("boss", salt, "h", "admin")
        versions.append(v)

    threads = [threading.Thread(target=bump, args=(f"s{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert sorted(versions) == [2, 3], (
        "two concurrent writes to the same account must yield two "
        f"distinct, sequential versions, got {versions}"
    )
    assert db.get_user("boss")["version"] == 3


# ------------------------------------------------- legacy import atomicity


def test_import_legacy_users_is_all_or_nothing(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    db.import_legacy_users([
        {"username": "u1", "salt": "s", "password_hash": "h", "role": "admin"},
        {"username": "u2", "salt": "s", "password_hash": "h", "role": "user"},
    ])
    assert {u["username"] for u in db.list_users()} == {"u1", "u2"}


def test_import_legacy_users_rolls_back_fully_on_error(tmp_path, monkeypatch):
    """A batch that fails partway must leave NO accounts imported —
    the "table non-empty means already migrated" check in auth.py
    depends on this being strictly all-or-nothing."""
    _use_tmp_data(tmp_path, monkeypatch)
    bad_batch = [
        {"username": "good1", "salt": "s", "password_hash": "h", "role": "admin"},
        # password_hash is NOT NULL in the schema — this row must fail
        # and, being in the same transaction, take "good1" down with it.
        {"username": "bad1", "salt": "s", "password_hash": None, "role": "user"},
    ]
    with pytest.raises(Exception):
        db.import_legacy_users(bad_batch)
    assert db.list_users() == [], "partial import was not rolled back"


def test_import_legacy_users_skips_existing(tmp_path, monkeypatch):
    """ON CONFLICT DO NOTHING: re-running an import (e.g. a retried
    migration) must not clobber accounts that already exist."""
    _use_tmp_data(tmp_path, monkeypatch)
    db.upsert_user("u1", "real-salt", "real-hash", "admin")
    db.import_legacy_users([
        {"username": "u1", "salt": "stale-salt", "password_hash": "stale-hash",
         "role": "user"},
    ])
    u = db.get_user("u1")
    assert u["salt"] == "real-salt" and u["role"] == "admin"
