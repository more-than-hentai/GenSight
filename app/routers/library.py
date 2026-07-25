"""Persistent library: search, ratings, similarity, stats, watches,
groups, WD Tagger and quality analysis."""
from __future__ import annotations

import csv
import io
import json
import re as _re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db, files
from .. import stats as stats_mod
from ..quality import quality_manager
from ..tagger import TaggerUnavailable, tagger_manager
from ..watcher import watch_manager

router = APIRouter(prefix="/api", tags=["library"])


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


class RunBody(BaseModel):
    limit: int | None = None


@router.get("/library")
def library(
    q: str = "",
    tool: str = "",
    favorite: bool | None = None,
    min_rating: int = 0,
    group: str = "",
    quality: str = "",
    directory: str = "",
    content_rating: str = "",
    sort: str = "recent",
    offset: int = 0,
    limit: int = 60,
):
    total, items = db.query_images(
        q=q, tool=tool, favorite=favorite, min_rating=min_rating,
        group=group, quality=quality, directory=directory,
        content_rating=content_rating, sort=sort,
        offset=offset, limit=min(limit, 500),
    )
    return {"total": total, "offset": offset, "items": items,
            "groups": db.group_names()}


_EXPORT_PARAM_FIELDS = ["Sampler", "Steps", "CFG scale", "Seed", "Size",
                        "Model", "Model hash"]


@router.get("/library/export")
def library_export(
    format: str = "json",
    q: str = "",
    tool: str = "",
    favorite: bool | None = None,
    min_rating: int = 0,
    group: str = "",
    quality: str = "",
    directory: str = "",
    content_rating: str = "",
):
    """Export the library (respecting the active filters) as JSON/CSV."""
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be json or csv")
    _total, items = db.query_images(
        q=q, tool=tool, favorite=favorite, min_rating=min_rating,
        group=group, quality=quality, directory=directory,
        content_rating=content_rating, limit=1_000_000,
    )
    if format == "csv":
        buf = io.StringIO()
        head = ["file", "tool", "prompt", "negative_prompt", "rating",
                "favorite", "group_name", "quality_score"]
        writer = csv.writer(buf)
        writer.writerow(head + _EXPORT_PARAM_FIELDS)
        for r in items:
            writer.writerow(
                [r[k] for k in head]
                + [r["params"].get(f, "") for f in _EXPORT_PARAM_FIELDS]
            )
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition":
                     'attachment; filename="gensight_library.csv"'},
        )
    body = json.dumps(items, ensure_ascii=False, indent=2, default=str)
    return StreamingResponse(
        iter([body]), media_type="application/json",
        headers={"Content-Disposition":
                 'attachment; filename="gensight_library.json"'},
    )


@router.get("/library/item")
def library_item(path: str = Query(...)):
    item = db.get_image(path)
    if not item:
        raise HTTPException(404, "not in library")
    return item


@router.patch("/library/item")
def library_item_patch(patch: MetaPatch):
    if not db.has_image(patch.path):
        raise HTTPException(404, "not in library")
    return db.set_meta(
        patch.path, rating=patch.rating, favorite=patch.favorite,
        group_name=patch.group_name,
    )


@router.get("/library/similar")
def library_similar(path: str = Query(...), max_distance: int = 10,
                    limit: int = 30):
    return {"items": db.similar_images(path, max(0, min(max_distance, 32)),
                                       limit)}


@router.get("/library/duplicates")
def library_duplicates(limit: int = 100):
    return {"groups": db.duplicate_groups(min(limit, 500))}


@router.get("/library/summary")
def library_summary():
    return db.summary()


@router.post("/library/cleanup")
def library_cleanup():
    """Remove rows for files deleted/moved outside the app, and
    orphaned thumbnail cache entries."""
    return {"removed": db.cleanup_missing(),
            "thumbs_removed": files.cleanup_thumbs()}


@router.get("/stats/prompts")
def prompt_stats(top: int = 50):
    return stats_mod.collect(top=max(1, min(top, 200)))


# ---------------------------------------------------------------- watches


@router.get("/watches")
def get_watches():
    return {"watches": db.list_watches(), "watcher": watch_manager.status()}


@router.post("/watches")
def post_watch(body: WatchBody):
    p = Path(body.directory).expanduser().resolve()
    if not p.exists():
        try:
            p.mkdir(parents=True)
        except OSError as e:
            raise HTTPException(400, f"cannot create directory: {e}")
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")
    return db.add_watch(str(p), body.recursive, body.poll_interval)


@router.patch("/watches/{watch_id}")
def patch_watch(watch_id: int, patch: WatchPatch):
    db.update_watch(watch_id, enabled=patch.enabled,
                    poll_interval=patch.poll_interval)
    return {"ok": True}


@router.delete("/watches/{watch_id}")
def remove_watch(watch_id: int):
    db.delete_watch(watch_id)
    return {"ok": True}


# ---------------------------------------------------------------- groups


@router.get("/groups")
def get_groups():
    return {"groups": db.list_groups()}


@router.post("/groups")
def post_group(body: GroupBody):
    try:
        return db.add_group(body.name.strip(), body.pattern, body.is_regex,
                            body.target)
    except _re.error as e:
        raise HTTPException(400, f"invalid regex: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/groups/{group_id}")
def remove_group(group_id: int):
    db.delete_group(group_id)
    return {"ok": True}


@router.post("/groups/apply")
def apply_groups(overwrite: bool = False):
    return {"updated": db.apply_groups(overwrite=overwrite)}


# ---------------------------------------------------------------- tagger


@router.get("/tagger/status")
def tagger_status():
    return tagger_manager.status()


@router.post("/tagger/run")
def tagger_run(body: RunBody):
    try:
        return tagger_manager.run(limit=body.limit)
    except (TaggerUnavailable, RuntimeError) as e:
        raise HTTPException(409, str(e))


@router.post("/tagger/cancel")
def tagger_cancel():
    tagger_manager.cancel()
    return {"ok": True}


# ---------------------------------------------------------------- quality


@router.get("/quality/status")
def quality_status():
    return quality_manager.status()


@router.post("/quality/run")
def quality_run(body: RunBody):
    try:
        return quality_manager.run(limit=body.limit)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.post("/quality/cancel")
def quality_cancel():
    quality_manager.cancel()
    return {"ok": True}
