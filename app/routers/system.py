"""Settings, directories, GPUs and i18n resources."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config, gpu

logger = logging.getLogger("gensight")

router = APIRouter(prefix="/api", tags=["system"])

WEB_DIR = config.BASE_DIR / "web"


class SettingsPatch(BaseModel):
    language: str | None = None
    recursive: bool | None = None
    workers: dict | None = None
    max_concurrent_jobs: int | None = None
    gpu: dict | None = None
    page_size: int | None = None
    quality: dict | None = None


class DirectoryBody(BaseModel):
    path: str


def public_settings() -> dict:
    """Settings with auth secrets stripped — never leak salts/hashes.
    Account details live behind /api/auth/users (admin-only)."""
    s = config.load_settings()
    s["auth"] = {"enabled": bool(s.get("auth", {}).get("enabled"))}
    return s


@router.get("/settings")
def get_settings():
    return public_settings()


@router.put("/settings")
def put_settings(patch: SettingsPatch):
    config.update_settings(patch.model_dump(exclude_none=True))
    return public_settings()


@router.post("/settings/directories")
def add_directory(body: DirectoryBody):
    p = Path(body.path).expanduser().resolve()
    if not p.exists():
        try:
            p.mkdir(parents=True)
        except OSError as e:
            raise HTTPException(400, f"cannot create directory: {e}")
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")
    settings = config.load_settings()
    if str(p) not in settings["directories"]:
        settings["directories"].append(str(p))
        config.save_settings(settings)
    return public_settings()


@router.delete("/settings/directories")
def remove_directory(path: str = Query(...)):
    settings = config.load_settings()
    settings["directories"] = [d for d in settings["directories"] if d != path]
    config.save_settings(settings)
    return public_settings()


@router.get("/gpus")
def get_gpus():
    return {"gpus": gpu.list_gpus(), "cpu_count": config._CPU}


@router.get("/i18n/{lang}")
def get_i18n(lang: str):
    safe = "".join(c for c in lang if c.isalnum() or c in "-_")
    for candidate in (safe, "en"):
        path = WEB_DIR / "i18n" / f"{candidate}.json"
        try:
            return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("i18n load failed for %s: %s", candidate, e)
    return JSONResponse({})
