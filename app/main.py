"""GenSight — AI-generated image metadata extractor web UI."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import __version__, config, db, gpu, metadata
from . import stats as stats_mod
from .scanner import manager
from .tagger import TaggerUnavailable, tagger_manager
from .watcher import watch_manager

logger = logging.getLogger("gensight")

app = FastAPI(title="GenSight", version=__version__)


@app.on_event("startup")
def _startup() -> None:
    watch_manager.start()

WEB_DIR = config.BASE_DIR / "web"

MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# 1x1 dark-gray PNG used when a thumbnail cannot be generated
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763601818000000830081dd436af40000000049"
    "454e44ae426082"
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return JSON instead of a bare 500 so the UI can show a message."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"}
    )


# ---------------------------------------------------------------- models


class SettingsPatch(BaseModel):
    language: str | None = None
    recursive: bool | None = None
    workers: dict | None = None
    max_concurrent_jobs: int | None = None
    gpu: dict | None = None
    page_size: int | None = None


class DirectoryBody(BaseModel):
    path: str


class ScanBody(BaseModel):
    directory: str
    recursive: bool | None = None
    workers: int | None = None


# ---------------------------------------------------------------- helpers


def _allowed_roots() -> list[Path]:
    """Directories whose files may be served.

    Registered settings directories, directories of past/current scan
    jobs (ad-hoc scans), and the upload folder.
    """
    roots = [Path(d).resolve() for d in config.load_settings()["directories"]]
    roots += [Path(j["directory"]).resolve() for j in manager.list()]
    roots.append(config.UPLOAD_DIR.resolve())
    return roots


def _validate_path(raw: str) -> Path:
    p = Path(raw).resolve()
    for root in _allowed_roots():
        if p == root or root in p.parents:
            if p.is_file():
                return p
            raise HTTPException(404, "file not found")
    # Library entries (from watches or past sessions) are also servable
    if db.has_image(str(p)):
        if p.is_file():
            return p
        raise HTTPException(404, "file not found")
    raise HTTPException(403, "path outside configured directories")


# ---------------------------------------------------------------- settings


@app.get("/api/settings")
def get_settings():
    return config.load_settings()


@app.put("/api/settings")
def put_settings(patch: SettingsPatch):
    return config.update_settings(patch.model_dump(exclude_none=True))


@app.post("/api/settings/directories")
def add_directory(body: DirectoryBody):
    p = Path(body.path).expanduser().resolve()
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")
    settings = config.load_settings()
    if str(p) not in settings["directories"]:
        settings["directories"].append(str(p))
        config.save_settings(settings)
    return config.load_settings()


@app.delete("/api/settings/directories")
def remove_directory(path: str = Query(...)):
    settings = config.load_settings()
    settings["directories"] = [d for d in settings["directories"] if d != path]
    return config.save_settings(settings)


@app.get("/api/gpus")
def get_gpus():
    return {"gpus": gpu.list_gpus(), "cpu_count": config._CPU}


@app.get("/api/i18n/{lang}")
def get_i18n(lang: str):
    safe = "".join(c for c in lang if c.isalnum() or c in "-_")
    for candidate in (safe, "en"):
        path = WEB_DIR / "i18n" / f"{candidate}.json"
        try:
            return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("i18n load failed for %s: %s", candidate, e)
    return JSONResponse({})  # UI falls back to markup defaults


# ---------------------------------------------------------------- scan jobs


@app.post("/api/scan")
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


@app.post("/api/analyze")
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

    result = metadata.extract(dest)
    result["uploaded"] = True
    result["original_name"] = safe_name
    return result


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": manager.list()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.summary()


@app.get("/api/jobs/{job_id}/results")
def job_results(
    job_id: str,
    offset: int = 0,
    limit: int = 60,
    q: str = "",
    tool: str = "",
):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.page(offset, min(limit, 500), q, tool)


@app.get("/api/jobs/{job_id}/result")
def job_result_detail(job_id: str, file: str = Query(...)):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    result = job.get_result(file)
    if not result:
        raise HTTPException(404, "result not found")
    return result


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.cancel()
    return job.summary()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if not manager.delete(job_id):
        raise HTTPException(404, "job not found")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/export")
def export_job(job_id: str, format: str = "json"):
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be json or csv")
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    data = job.page(0, 10**9)["items"]
    stem = f"gensight_{job_id}"
    if format == "csv":
        buf = io.StringIO()
        fields = [
            "file", "tool", "prompt", "negative_prompt",
            "Sampler", "Steps", "CFG scale", "Seed", "Size", "Model", "Model hash",
        ]
        writer = csv.writer(buf)
        writer.writerow(fields)
        for r in data:
            writer.writerow(
                [r["file"], r["tool"], r["prompt"], r["negative_prompt"]]
                + [r["params"].get(f, "") for f in fields[4:]]
            )
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
        )
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{stem}.json"'},
    )


# ---------------------------------------------------------------- library


class MetaPatch(BaseModel):
    path: str
    rating: int | None = None
    favorite: bool | None = None
    group_name: str | None = None


class WatchBody(BaseModel):
    directory: str
    recursive: bool = True
    poll_interval: float = 30


class WatchPatch(BaseModel):
    enabled: bool | None = None
    poll_interval: float | None = None


class GroupBody(BaseModel):
    name: str
    pattern: str
    is_regex: bool = False
    target: str = "prompt"


class TaggerBody(BaseModel):
    limit: int | None = None


@app.get("/api/library")
def library(
    q: str = "",
    tool: str = "",
    favorite: bool | None = None,
    min_rating: int = 0,
    group: str = "",
    sort: str = "recent",
    offset: int = 0,
    limit: int = 60,
):
    total, items = db.query_images(
        q=q, tool=tool, favorite=favorite, min_rating=min_rating,
        group=group, sort=sort, offset=offset, limit=min(limit, 500),
    )
    return {"total": total, "offset": offset, "items": items,
            "groups": db.group_names()}


@app.get("/api/library/item")
def library_item(path: str = Query(...)):
    item = db.get_image(path)
    if not item:
        raise HTTPException(404, "not in library")
    return item


@app.patch("/api/library/item")
def library_item_patch(patch: MetaPatch):
    if not db.has_image(patch.path):
        raise HTTPException(404, "not in library")
    return db.set_meta(
        patch.path, rating=patch.rating, favorite=patch.favorite,
        group_name=patch.group_name,
    )


@app.get("/api/library/similar")
def library_similar(path: str = Query(...), max_distance: int = 10, limit: int = 30):
    return {"items": db.similar_images(path, max(0, min(max_distance, 32)), limit)}


@app.get("/api/library/duplicates")
def library_duplicates(limit: int = 100):
    return {"groups": db.duplicate_groups(min(limit, 500))}


@app.get("/api/library/summary")
def library_summary():
    return db.summary()


@app.get("/api/stats/prompts")
def prompt_stats(top: int = 50):
    return stats_mod.collect(top=max(1, min(top, 200)))


# ---------------------------------------------------------------- watches


@app.get("/api/watches")
def get_watches():
    return {"watches": db.list_watches(), "watcher": watch_manager.status()}


@app.post("/api/watches")
def post_watch(body: WatchBody):
    p = Path(body.directory).expanduser()
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, f"directory not found: {body.directory}")
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")
    return db.add_watch(str(p), body.recursive, body.poll_interval)


@app.patch("/api/watches/{watch_id}")
def patch_watch(watch_id: int, patch: WatchPatch):
    db.update_watch(watch_id, enabled=patch.enabled,
                    poll_interval=patch.poll_interval)
    return {"ok": True}


@app.delete("/api/watches/{watch_id}")
def remove_watch(watch_id: int):
    db.delete_watch(watch_id)
    return {"ok": True}


# ---------------------------------------------------------------- groups


@app.get("/api/groups")
def get_groups():
    return {"groups": db.list_groups()}


@app.post("/api/groups")
def post_group(body: GroupBody):
    import re as _re

    try:
        return db.add_group(body.name.strip(), body.pattern, body.is_regex,
                            body.target)
    except _re.error as e:
        raise HTTPException(400, f"invalid regex: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/groups/{group_id}")
def remove_group(group_id: int):
    db.delete_group(group_id)
    return {"ok": True}


@app.post("/api/groups/apply")
def apply_groups(overwrite: bool = False):
    return {"updated": db.apply_groups(overwrite=overwrite)}


# ---------------------------------------------------------------- tagger


@app.get("/api/tagger/status")
def tagger_status():
    return tagger_manager.status()


@app.post("/api/tagger/run")
def tagger_run(body: TaggerBody):
    try:
        return tagger_manager.run(limit=body.limit)
    except TaggerUnavailable as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/tagger/cancel")
def tagger_cancel():
    tagger_manager.cancel()
    return {"ok": True}


# ---------------------------------------------------------------- images


@app.get("/api/image")
def get_image(path: str = Query(...), thumb: bool = False):
    p = _validate_path(path)
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


# ---------------------------------------------------------------- static UI

app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
