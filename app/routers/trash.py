"""Recycle bin and metadata-based file organization."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, files

router = APIRouter(prefix="/api", tags=["trash"])


class TrashBody(BaseModel):
    path: str


class OrganizeBody(BaseModel):
    target_root: str
    template: str = "{model}/{date}"
    dry_run: bool = True
    # Library filters selecting which images to move
    q: str = ""
    tool: str = ""
    group: str = ""
    favorite: bool | None = None
    min_rating: int = 0
    quality: str = ""
    directory: str = ""


@router.post("/trash")
def trash_image(body: TrashBody):
    try:
        return files.trash_image(body.path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"move failed: {e}")


@router.get("/trash")
def trash_list():
    return {"items": db.trash_list()}


@router.post("/trash/{trash_id}/restore")
def trash_restore(trash_id: int):
    try:
        return files.restore_image(trash_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"restore failed: {e}")


@router.delete("/trash/{trash_id}")
def trash_purge_one(trash_id: int):
    if not db.trash_get(trash_id):
        raise HTTPException(404, "trash entry not found")
    return {"purged": files.purge_trash(trash_id)}


@router.delete("/trash")
def trash_purge_all():
    return {"purged": files.purge_trash()}


@router.post("/organize")
def organize(body: OrganizeBody):
    root = Path(body.target_root).expanduser()
    if not str(root).strip():
        raise HTTPException(400, "target_root is required")
    if not body.dry_run and not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"cannot create target_root: {e}")
    try:
        return files.organize(
            str(root), template=body.template, dry_run=body.dry_run,
            q=body.q, tool=body.tool, group=body.group,
            favorite=body.favorite, min_rating=body.min_rating,
            quality=body.quality, directory=body.directory,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
