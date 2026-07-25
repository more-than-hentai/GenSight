"""Image and thumbnail serving, restricted to known-safe paths."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from PIL import Image

from .. import config, db, files
from ..scanner import manager

logger = logging.getLogger("gensight")

router = APIRouter(prefix="/api", tags=["media"])

# 1x1 dark-gray PNG used when a thumbnail cannot be generated
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763601818000000830081dd436af40000000049"
    "454e44ae426082"
)


def validate_path(raw: str) -> Path:
    """Serve only files under registered/scanned/watched roots, or
    files already present in the library DB."""
    p = Path(raw).resolve()
    job_dirs = [j["directory"] for j in manager.list()]
    for root in files.allowed_roots(extra_job_dirs=job_dirs):
        if p == root or root in p.parents:
            if p.is_file():
                return p
            raise HTTPException(404, "file not found")
    if db.has_image(str(p)):
        if p.is_file():
            return p
        raise HTTPException(404, "file not found")
    raise HTTPException(403, "path outside configured directories")


@router.get("/image")
def get_image(path: str = Query(...), thumb: bool = False):
    p = validate_path(path)
    if not thumb:
        return FileResponse(p)
    try:
        config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{p}:{p.stat().st_mtime_ns}".encode()).hexdigest()
        cached = config.THUMB_DIR / f"{key}.webp"
        if not cached.exists():
            with Image.open(p) as img:
                img.thumbnail((360, 360))
                img.convert("RGB").save(cached, "WEBP", quality=80)
        return FileResponse(cached, media_type="image/webp")
    except Exception as e:  # noqa: BLE001 - corrupt image → placeholder, not a 500
        logger.warning("thumbnail failed for %s: %s", p, e)
        return Response(content=_PLACEHOLDER_PNG, media_type="image/png")
