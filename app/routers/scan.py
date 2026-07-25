"""Scan jobs and single-image analyze.

Per-job result browsing/export was consolidated into the persistent
library (/api/library with a `directory` filter, /api/library/export);
jobs remain for progress tracking and cancellation."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from .. import config, metadata
from ..scanner import manager, process_and_store

router = APIRouter(prefix="/api", tags=["scan"])

MAX_UPLOAD_BYTES = 100 * 1024 * 1024


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
async def analyze_upload(file: UploadFile):
    """Analyze a single uploaded image (drag & drop / click upload)."""
    suffix = Path(file.filename or "upload").suffix.lower()
    if suffix not in metadata.SUPPORTED_EXTENSIONS:
        raise HTTPException(415, f"unsupported file type: {suffix or '(none)'}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (max 100 MB)")

    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload").name
    dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    dest.write_bytes(data)

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
