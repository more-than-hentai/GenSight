"""Image and thumbnail serving, restricted to known-safe paths."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import stat as stat_mod
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image

from .. import config, db, files, metadata
from ..scanner import manager

logger = logging.getLogger("gensight")

router = APIRouter(prefix="/api", tags=["media"])

# 1x1 dark-gray PNG used when a thumbnail cannot be generated
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763601818000000830081dd436af40000000049"
    "454e44ae426082"
)

STREAM_CHUNK = 256 * 1024


def validate_path(raw: str, *, indexed_only: bool = False) -> Path:
    """Resolve a request path to a servable file.

    Only files with a supported image extension are ever served — the
    configured roots hold user data (.env, keys, exports) that this
    endpoint must never hand out.

    indexed_only (non-admin callers) tightens this further: the file
    must exist in the library AND have decoded as a real image during
    extraction, so neither an unscanned file nor a secret wearing a
    .png suffix can be fetched.
    """
    p = Path(raw).resolve()
    if p.suffix.lower() not in metadata.SUPPORTED_EXTENSIONS:
        raise HTTPException(403, "not an image file")

    if indexed_only:
        if not db.is_decoded_image(str(p)):
            raise HTTPException(403, "not a library image")
        if not p.is_file():
            raise HTTPException(404, "file not found")
        return p

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


def _open_checked(p: Path) -> int:
    """Open a validated path as a regular file, refusing symlinks.

    Validation and serving are separate syscalls, so the pathname could
    be swapped for a link in between. Opening with O_NOFOLLOW and
    confirming the descriptor is a regular file closes that window: the
    bytes we stream come from the fd, never from a re-resolved name.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(p, flags)
    except OSError as e:
        raise HTTPException(404, f"cannot open file: {e.strerror}")
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise HTTPException(403, "not a regular file")
    except HTTPException:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        raise HTTPException(404, "file not found")
    return fd


def _stream_fd(fd: int, media_type: str) -> StreamingResponse:
    def chunks():
        with os.fdopen(fd, "rb") as fh:
            while chunk := fh.read(STREAM_CHUNK):
                yield chunk

    return StreamingResponse(chunks(), media_type=media_type)


@router.get("/image")
def get_image(request: Request, path: str = Query(...), thumb: bool = False):
    role = getattr(request.state, "auth_role", "admin")
    p = validate_path(path, indexed_only=(role != "admin"))

    if not thumb:
        fd = _open_checked(p)
        media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return _stream_fd(fd, media_type)

    try:
        config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{p}:{p.stat().st_mtime_ns}".encode()).hexdigest()
        cached = config.THUMB_DIR / f"{key}.webp"
        if not cached.exists():
            fd = _open_checked(p)
            with os.fdopen(fd, "rb") as fh, Image.open(fh) as img:
                img.thumbnail((360, 360))
                img.convert("RGB").save(cached, "WEBP", quality=80)
        return FileResponse(cached, media_type="image/webp")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - corrupt image → placeholder, not a 500
        logger.warning("thumbnail failed for %s: %s", p, e)
        return Response(content=_PLACEHOLDER_PNG, media_type="image/png")
