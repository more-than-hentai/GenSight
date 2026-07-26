"""Scan jobs and single-image analyze.

Per-job result browsing/export was consolidated into the persistent
library (/api/library with a `directory` filter, /api/library/export);
jobs remain for progress tracking and cancellation."""
from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from PIL import Image
from pydantic import BaseModel

from .. import config, metadata
from ..scanner import manager, process_and_store

router = APIRouter(prefix="/api", tags=["scan"])

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
UPLOAD_CHUNK = 1024 * 1024
# Uploads are writable by restricted accounts, so cap how fast one
# identity can turn requests into permanent files on disk.
UPLOAD_RATE_LIMIT = 60
UPLOAD_RATE_WINDOW = 600  # seconds

_rate_lock = threading.Lock()
_upload_hits: dict[str, deque] = defaultdict(deque)


def _rate_limit(identity: str) -> None:
    now = time.time()
    with _rate_lock:
        hits = _upload_hits[identity]
        while hits and hits[0] < now - UPLOAD_RATE_WINDOW:
            hits.popleft()
        if len(hits) >= UPLOAD_RATE_LIMIT:
            retry = int(hits[0] + UPLOAD_RATE_WINDOW - now) + 1
            raise HTTPException(
                429, f"too many uploads, retry in {retry}s",
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)


class ScanBody(BaseModel):
    directory: str
    recursive: bool | None = None
    workers: int | None = None


@router.post("/scan")
def start_scan(body: ScanBody):
    """Start a scan. The directory does not need to be registered in
    settings — any readable local directory can be scanned ad hoc."""
    settings = config.load_settings()
    if not body.directory or not body.directory.strip():
        raise HTTPException(400, "directory is required")
    path = Path(body.directory.strip()).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, f"directory not found: {body.directory}")
    if not path.is_dir():
        raise HTTPException(400, f"not a directory: {path}")
    recursive = body.recursive if body.recursive is not None else settings["recursive"]
    workers = body.workers or settings["workers"]["extract"]
    job = manager.submit(str(path), recursive, workers)
    return job.summary()


@router.post("/analyze")
async def analyze_upload(request: Request, file: UploadFile):
    """Analyze a single uploaded image (drag & drop / click upload).

    The upload is streamed to a temp file, decoded to prove it really is
    an image, and only then kept — an extension alone must not be enough
    to park arbitrary bytes in the data directory.
    """
    _rate_limit(
        getattr(request.state, "auth_user", "")
        or (request.client.host if request.client else "anonymous")
    )

    suffix = Path(file.filename or "upload").suffix.lower()
    if suffix not in metadata.SUPPORTED_EXTENSIONS:
        raise HTTPException(415, f"unsupported file type: {suffix or '(none)'}")

    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload").name
    dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    tmp = dest.with_suffix(dest.suffix + ".part")

    written = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(UPLOAD_CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "file too large (max 100 MB)")
                out.write(chunk)
        if not written:
            raise HTTPException(400, "empty file")
        try:
            with Image.open(tmp) as probe:
                probe.verify()
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - not a decodable image
            raise HTTPException(415, "file is not a readable image")
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    # Persist into the library like scanned files (searchable, has
    # phash for similarity, shows up with rating/favorite controls)
    result = process_and_store(dest)
    result["uploaded"] = True
    result["original_name"] = safe_name
    return result


@router.get("/jobs")
def list_jobs():
    return {"jobs": manager.list()}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.summary()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.cancel()
    return job.summary()


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    if not manager.delete(job_id):
        raise HTTPException(404, "job not found")
    return {"ok": True}
