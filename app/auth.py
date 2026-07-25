"""Session-based authentication (optional, off by default).

Passwords are hashed with stdlib hashlib.scrypt — no external
dependency. Sessions are opaque tokens in an in-memory store (a
restart just requires logging in again). Auth state lives in
settings.json under the "auth" key and is managed via /api/auth/*.

When disabled (the default for a personal localhost tool) nothing
changes. When enabled, the middleware in main.py requires a valid
session cookie for every /api route except /api/auth/*.
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

_sessions: dict[str, tuple[str, float]] = {}
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


def set_credentials(username: str, password: str) -> None:
    salt, digest = hash_password(password)
    config.update_settings({
        "auth": {"enabled": True, "username": username,
                 "salt": salt, "password_hash": digest}
    })


def disable() -> None:
    config.update_settings({
        "auth": {"enabled": False, "username": "", "salt": "",
                 "password_hash": ""}
    })
    with _lock:
        _sessions.clear()


def login(username: str, password: str) -> str | None:
    cfg = auth_config()
    if not cfg.get("enabled"):
        return None
    if username != cfg.get("username"):
        # Hash anyway so the timing does not reveal valid usernames
        hash_password(password, cfg.get("salt") or secrets.token_hex(16))
        return None
    if not verify_password(password, cfg.get("salt", ""),
                           cfg.get("password_hash", "")):
        return None
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = (username, time.time() + SESSION_TTL)
    return token


def logout(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


def check(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        entry = _sessions.get(token)
        if not entry:
            return False
        if entry[1] < time.time():
            _sessions.pop(token, None)
            return False
    return True
