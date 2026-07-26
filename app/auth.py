"""Session-based authentication with roles (optional, off by default).

Two roles:
- admin: full access (settings, scans, file operations, user management)
- user:  restricted viewer — library browsing/rating, single-image
  analyze, stats. Every endpoint that accepts filesystem paths or
  changes system state is admin-only (enforced by the middleware in
  main.py), so a "user" account is safe to hand out when the instance
  is exposed beyond localhost.

Passwords are hashed with stdlib hashlib.scrypt. Accounts live in
settings.json under auth.users; the legacy single-admin fields
(username/salt/password_hash) are migrated transparently. Sessions are
opaque tokens in an in-memory store (a restart just requires logging
in again).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from . import config

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


def get_users() -> list[dict]:
    cfg = auth_config()
    users = [dict(u) for u in cfg.get("users") or []]
    if not users and cfg.get("username") and cfg.get("password_hash"):
        # Legacy single-admin layout -> migrate in place
        users = [{
            "username": cfg["username"], "salt": cfg.get("salt", ""),
            "password_hash": cfg["password_hash"], "role": "admin",
        }]
    return users


def _save_users(users: list[dict], enabled_flag: bool | None = None) -> None:
    patch = {"users": users, "username": "", "salt": "", "password_hash": ""}
    if enabled_flag is not None:
        patch["enabled"] = enabled_flag
    config.update_settings({"auth": patch})


def find_user(username: str) -> dict | None:
    for u in get_users():
        if u["username"] == username:
            return u
    return None


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
    salt, digest = hash_password(password)
    previous = find_user(username)
    version = int((previous or {}).get("version", 0)) + 1
    users = [u for u in get_users() if u["username"] != username]
    users.append({"username": username, "salt": salt,
                  "password_hash": digest, "role": role, "version": version})
    _save_users(users)
    # Replacing an account (password reset / role change) must not leave
    # the previous role alive in an existing cookie. The version bump
    # also invalidates a session minted by a login that raced this
    # update: session_info rejects any token whose version is stale.
    revoke_sessions(username)


def delete_user(username: str) -> None:
    users = get_users()
    remaining = [u for u in users if u["username"] != username]
    if len(remaining) == len(users):
        raise KeyError(f"no such user: {username}")
    if not any(u.get("role") == "admin" for u in remaining):
        raise ValueError("cannot delete the last admin account")
    _save_users(remaining)
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
    _save_users(get_users(), enabled_flag=True)


def disable() -> None:
    """Turn auth off. Accounts are kept for a later re-enable."""
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
