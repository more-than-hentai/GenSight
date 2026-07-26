"""Settings persistence for GenSight.

Settings are stored as JSON under the data directory so they can be
edited from the web UI and survive restarts / container rebuilds
(mount the data directory as a volume in Docker).
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GENSIGHT_DATA_DIR", BASE_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
THUMB_DIR = DATA_DIR / "thumbs"
UPLOAD_DIR = DATA_DIR / "uploads"

_CPU = os.cpu_count() or 4

DEFAULT_SETTINGS: dict[str, Any] = {
    "language": "ko",
    "directories": [],
    "recursive": True,
    "workers": {
        # Directory walk / file enumeration workers
        "scan": 2,
        # Metadata extraction workers (CPU bound-ish, mostly I/O)
        "extract": max(2, _CPU // 2),
        # Thumbnail generation workers
        "thumbnail": 2,
    },
    "max_concurrent_jobs": 2,
    "gpu": {
        # GPU device indices enabled for (future) ML analysis jobs
        "enabled_devices": [],
        "jobs_per_gpu": 1,
    },
    "page_size": 60,
    "quality": {
        # Analyze quality inline while scanning/ingesting new images
        # (roughly doubles per-file decode cost; off by default)
        "auto": False,
    },
    "auth": {
        # Optional session auth; managed via /api/auth/*. Accounts
        # (salts/hashes) live in the SQLite `users` table (see
        # app/auth.py), not here — only the on/off toggle is a setting.
        # users/username/salt/password_hash are legacy fields kept so an
        # older settings.json still merges cleanly; auth.py migrates any
        # accounts found there into the database on first access and
        # then leaves them empty.
        "enabled": False,
        "users": [],
        "username": "",
        "salt": "",
        "password_hash": "",
    },
}

_lock = threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_settings() -> dict[str, Any]:
    with _lock:
        if SETTINGS_FILE.exists():
            try:
                stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                stored = {}
        else:
            stored = {}
        return _deep_merge(DEFAULT_SETTINGS, stored)


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_merge(DEFAULT_SETTINGS, settings)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(SETTINGS_FILE)
    return merged


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    return save_settings(_deep_merge(current, patch))
