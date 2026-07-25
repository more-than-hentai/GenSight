"""GenSight — AI-generated image metadata extractor web UI."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import __version__, config, gpu
from .scanner import manager

app = FastAPI(title="GenSight", version=__version__)

WEB_DIR = config.BASE_DIR / "web"


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
    return [Path(d).resolve() for d in config.load_settings()["directories"]]


def _validate_path(raw: str) -> Path:
    """Only serve files under directories registered in settings."""
    p = Path(raw).resolve()
    for root in _allowed_roots():
        if p == root or root in p.parents:
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
    path = WEB_DIR / "i18n" / f"{safe}.json"
    if not path.exists():
        path = WEB_DIR / "i18n" / "en.json"
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- scan jobs


@app.post("/api/scan")
def start_scan(body: ScanBody):
    settings = config.load_settings()
    directory = str(Path(body.directory).expanduser().resolve())
    if directory not in settings["directories"]:
        raise HTTPException(403, "directory not registered in settings")
    recursive = body.recursive if body.recursive is not None else settings["recursive"]
    workers = body.workers or settings["workers"]["extract"]
    job = manager.submit(directory, recursive, workers)
    return job.summary()


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


# ---------------------------------------------------------------- images


@app.get("/api/image")
def get_image(path: str = Query(...), thumb: bool = False):
    p = _validate_path(path)
    if not thumb:
        return FileResponse(p)
    config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{p}:{p.stat().st_mtime_ns}".encode()).hexdigest()
    cached = config.THUMB_DIR / f"{key}.webp"
    if not cached.exists():
        with Image.open(p) as img:
            img.thumbnail((360, 360))
            img.convert("RGB").save(cached, "WEBP", quality=80)
    return FileResponse(cached, media_type="image/webp")


# ---------------------------------------------------------------- static UI

app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
