"""Session-based authentication with roles (optional, off by default).

Two roles:
- admin: full access (settings, scans, file operations, user management)
- user:  restricted viewer — library browsing/rating, single-image
  analyze, stats. Every endpoint that accepts filesystem paths or
  changes system state is admin-only (enforced by the middleware in
  main.py), so a "user" account is safe to hand out when the instance
  is exposed beyond localhost.

Passwords are hashed with stdlib hashlib.scrypt. Accounts live in the
SQLite `users` table (db.py) — the same WAL-protected storage as the
rest of the app — rather than settings.json, so a credential write
cannot race a concurrent read-modify-write the way a JSON file can, and
salts/hashes never sit in a config file someone might share for
support. Older installs that still have accounts in settings.json (the
"auth.users" list, or the original single-admin fields) are migrated
into the table transparently on first access. Sessions are opaque
tokens in an in-memory store (a restart just requires logging in
again).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from . import config, db

COOKIE_NAME = "gensight_session"
SESSION_TTL = 7 * 24 * 3600

ROLES = ("admin", "user")

# token -> (username, role, credential version, expiry)
_sessions: dict[str, tuple[str, str, int, float]] = {}
_lock = threading.Lock()

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt_hex = salt_hex or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT
    )
    return salt_hex, digest.hex()


def verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    if not (salt_hex and expected_hex):
        return False
    try:
        _, actual = hash_password(password, salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected_hex)


def auth_config() -> dict:
    return config.load_settings().get("auth", {})


def enabled() -> bool:
    return bool(auth_config().get("enabled"))


# ---------------------------------------------------------------- users


def _migrate_legacy_users() -> None:
    """One-time move of accounts out of settings.json into the users
    table (see module docstring for why). Cheap and idempotent — a
    no-op the instant the table has at least one row — so every entry
    point can just call it rather than tracking whether it ran.

    The import itself (db.import_legacy_users) is one atomic
    transaction, so an interruption partway can never leave the table
    non-empty with some legacy accounts still stranded in settings.json
    — it is either fully imported or not imported at all, and "table
    non-empty" stays a reliable "already migrated" signal either way.
    (The narrower case of the DB commit succeeding but the process
    dying before the settings.json clear below persists is harmless: on
    the next call the table is already non-empty so migration is
    correctly skipped, just leaving inert, already-superseded secrets
    sitting in the JSON file rather than truly two-phase-committing
    across two separate storage systems for a one-time historical
    bridge.)
    """
    if db.list_users():
        return
    cfg = auth_config()
    legacy = [dict(u) for u in cfg.get("users") or []]
    if not legacy and cfg.get("username") and cfg.get("password_hash"):
        legacy = [{"username": cfg["username"], "salt": cfg.get("salt", ""),
                  "password_hash": cfg["password_hash"], "role": "admin"}]
    if not legacy:
        return
    db.import_legacy_users(legacy)
    config.update_settings({"auth": {"users": [], "username": "",
                                     "salt": "", "password_hash": ""}})


def get_users() -> list[dict]:
    _migrate_legacy_users()
    return db.list_users()


def find_user(username: str) -> dict | None:
    _migrate_legacy_users()
    return db.get_user(username)


def revoke_sessions(username: str) -> int:
    """Drop every live session for a user. Called whenever an account
    changes, so a password reset or a demotion takes effect at once
    instead of leaving the old role usable for the session TTL."""
    with _lock:
        stale = [tok for tok, (name, _r, _v, _e) in _sessions.items()
                 if name == username]
        for tok in stale:
            _sessions.pop(tok, None)
    return len(stale)


def cred_version(username: str) -> int:
    user = find_user(username)
    return int(user.get("version", 0)) if user else -1


def add_user(username: str, password: str, role: str = "user") -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    _migrate_legacy_users()
    salt, digest = hash_password(password)
    version = db.upsert_user(username, salt, digest, role)
    if version is None:
        # db.upsert_user refuses atomically — this can trigger even
        # after a caller's own pre-check passed, if a concurrent
        # request already demoted/removed the other admin(s) first.
        raise ValueError("cannot demote the last admin account")
    # Replacing an account (password reset / role change) must not leave
    # the previous role alive in an existing cookie. upsert_user's
    # version bump also invalidates a session minted by a login that
    # raced this update: session_info rejects any token whose version
    # is stale.
    revoke_sessions(username)


def delete_user(username: str) -> None:
    result = db.delete_user_row(username)
    if result == "not_found":
        raise KeyError(f"no such user: {username}")
    if result == "last_admin":
        raise ValueError("cannot delete the last admin account")
    revoke_sessions(username)


def authenticate(username: str, password: str) -> str | None:
    """Return the user's role on success, None otherwise."""
    user = find_user(username)
    if user is None:
        # Hash anyway so the timing does not reveal valid usernames
        hash_password(password, secrets.token_hex(16))
        return None
    if not verify_password(password, user.get("salt", ""),
                           user.get("password_hash", "")):
        return None
    return user.get("role", "admin")


def _account_snapshot(username: str, password: str) -> tuple[str, int] | None:
    """(role, version) captured from the record the password matched."""
    user = find_user(username)
    if user is None:
        hash_password(password, secrets.token_hex(16))
        return None
    if not verify_password(password, user.get("salt", ""),
                           user.get("password_hash", "")):
        return None
    return user.get("role", "admin"), int(user.get("version", 0))


def set_credentials(username: str, password: str) -> None:
    """Enable auth with an admin account (initial setup)."""
    add_user(username, password, role="admin")
    config.update_settings({"auth": {"enabled": True}})


def disable() -> None:
    """Turn auth off. Accounts stay in the database for a later
    re-enable — disabling is a toggle, not a wipe."""
    config.update_settings({"auth": {"enabled": False}})
    with _lock:
        _sessions.clear()


# ---------------------------------------------------------------- sessions


def login(username: str, password: str) -> str | None:
    if not enabled():
        return None
    snapshot = _account_snapshot(username, password)
    if snapshot is None:
        return None
    role, version = snapshot
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = (username, role, version, time.time() + SESSION_TTL)
    return token


def logout(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


def session_info(token: str | None) -> tuple[str, str] | None:
    """(username, role) for a valid session, else None.

    A session is only valid while it matches the account's current
    credential version, so any account change (including one that raced
    the login that minted this token) invalidates it.
    """
    if not token:
        return None
    with _lock:
        entry = _sessions.get(token)
        if not entry:
            return None
        username, role, version, expiry = entry
        if expiry < time.time():
            _sessions.pop(token, None)
            return None
    if version != cred_version(username):
        with _lock:
            _sessions.pop(token, None)
        return None
    return username, role


def check(token: str | None) -> bool:
    return session_info(token) is not None
