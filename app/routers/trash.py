"""Recycle bin and metadata-based file organization."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import audit, db, files

router = APIRouter(prefix="/api", tags=["trash"])


def _actor(request: Request) -> str:
    return getattr(request.state, "auth_user", "") or ""


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
def trash_image(body: TrashBody, request: Request):
    try:
        entry = files.trash_image(body.path)
        audit.record("trash.move", actor=_actor(request), target=body.path,
                     detail={"trash_path": entry["trash_path"]})
        return entry
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"move failed: {e}")


@router.get("/trash")
def trash_list():
    return {"items": db.trash_list()}


@router.post("/trash/{trash_id}/restore")
def trash_restore(trash_id: int, request: Request):
    try:
        result = files.restore_image(trash_id)
        audit.record("trash.restore", actor=_actor(request),
                     target=result["restored_to"], detail={"id": trash_id})
        return result
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"restore failed: {e}")


@router.delete("/trash/{trash_id}")
def trash_purge_one(trash_id: int, request: Request):
    entry = db.trash_get(trash_id)
    if not entry:
        raise HTTPException(404, "trash entry not found")
    purged = files.purge_trash(trash_id)
    audit.record("trash.purge", actor=_actor(request),
                 target=entry["original_path"], detail={"id": trash_id})
    return {"purged": purged}


@router.delete("/trash")
def trash_purge_all(request: Request):
    purged = files.purge_trash()
    audit.record("trash.empty", actor=_actor(request),
                 detail={"purged": purged})
    return {"purged": purged}


@router.post("/organize")
def organize(body: OrganizeBody, request: Request):
    root = Path(body.target_root).expanduser()
    if not str(root).strip():
        raise HTTPException(400, "target_root is required")
    if not body.dry_run and not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"cannot create target_root: {e}")
    try:
        result = files.organize(
            str(root), template=body.template, dry_run=body.dry_run,
            q=body.q, tool=body.tool, group=body.group,
            favorite=body.favorite, min_rating=body.min_rating,
            quality=body.quality, directory=body.directory,
        )
        if not body.dry_run:
            audit.record("organize.apply", actor=_actor(request),
                         target=str(root),
                         detail={"template": body.template,
                                 "moved": result.get("count"),
                                 "errors": len(result.get("errors") or [])})
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
