"""Admin-only library maintenance: scoped record purge and the archive.

Mounted under /api/admin deliberately. The auth middleware allows a
restricted `user` role only on a whitelist of prefixes, so anything
under /api/library would be reachable by such an account unless
explicitly denied — a footgun for destructive routes. /api/admin matches
no allow rule, so these endpoints are admin-only by default rather than
by remembering to blacklist each one, and require_admin below enforces
it again at the route.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import audit, auth, db, purge
from ..scanner import manager

logger = logging.getLogger("gensight")


def require_admin(request: Request) -> str:
    """Defence in depth behind the middleware.

    With auth disabled every caller is the operator (single-user
    localhost), which is checked first so the owner is never locked out
    of their own maintenance tools. With auth on, the role must be
    present and equal to "admin" — a missing role denies rather than
    defaulting to admin, so a middleware that failed to run cannot open
    these routes.
    """
    if not auth.enabled():
        return ""
    if getattr(request.state, "auth_role", None) != "admin":
        raise HTTPException(403, "admin privileges required")
    return getattr(request.state, "auth_user", "") or ""


router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])


class PurgePreviewBody(BaseModel):
    root: str
    recursive: bool = True
    mode: str = "all"


class PurgeBody(BaseModel):
    token: str


class RestoreBody(BaseModel):
    batch_id: str


class ArchivePruneBody(BaseModel):
    # False prunes only entries past the retention window; True wipes the
    # whole archive (irreversible — the UI must confirm).
    all: bool = False


def _actor(request: Request) -> str:
    return getattr(request.state, "auth_user", "") or ""


def _active_scans(root: str) -> list[dict]:
    """Running scans whose directory overlaps `root`."""
    def related(other: str) -> bool:
        a, b = root.rstrip("/") + "/", str(other).rstrip("/") + "/"
        return a.startswith(b) or b.startswith(a)

    return [
        {"job": j["id"], "directory": j["directory"], "status": j["status"]}
        for j in manager.concurrency()["active"] if related(j["directory"])
    ]


@router.post("/library/purge/preview")
def purge_preview(body: PurgePreviewBody, request: Request):
    """Compute what a purge would remove, without removing anything."""
    try:
        plan = purge.plan(body.root, body.recursive, body.mode)
    except purge.PurgeError as e:
        raise HTTPException(400, str(e))
    plan["overlaps"]["active_scans"] = _active_scans(plan["root"])
    audit.record("library.purge_preview", actor=_actor(request),
                 target=plan["root"],
                 detail={"mode": plan["mode"], "recursive": plan["recursive"],
                         "targets": plan["targets"], "total": plan["total"]})
    return plan


@router.post("/library/purge")
def purge_execute(body: PurgeBody, request: Request):
    """Execute a previewed plan. Archives rows; never deletes files."""
    plan = purge.get_plan(body.token)
    if plan is None:
        raise HTTPException(409, "unknown or expired purge token; preview again")
    busy = _active_scans(plan["root"])
    if busy:
        # A worker mid-flight would re-insert rows straight after the
        # commit, so the purge would silently under-deliver.
        raise HTTPException(
            409, f"a scan is running on an overlapping path "
                 f"({busy[0]['directory']}); cancel it or wait, then retry")
    # An enabled watch is the same hazard on a timer: its next sweep re-ingests
    # the files and undoes the purge, and a sweep already in flight can strand
    # the enrichment in the archive while inserting a blank live row.
    watching = [w["directory"] for w in plan["overlaps"]["watches"]
                if w["enabled"]]
    if watching:
        raise HTTPException(
            409, f"an enabled watch covers this path ({watching[0]}); "
                 "disable it first, otherwise the next sweep re-adds "
                 "everything you just cleaned")
    try:
        result = purge.execute(body.token)
    except purge.PurgeError as e:
        # 409: the plan is no longer valid (library moved on, unreadable
        # rows). The client should re-preview rather than retry blindly.
        raise HTTPException(409, str(e))
    audit.record("library.purge", actor=_actor(request), target=result["root"],
                 detail=result)
    return result


@router.get("/library/archive")
def archive_status():
    summary = db.archive_summary()
    cutoff = purge.retention_cutoff()
    settings_days = purge.config.load_settings().get("archive", {}).get(
        "retention_days", 30)
    return {**summary, "retention_days": settings_days,
            "retention_cutoff": cutoff,
            "expired": db.count_archived_before(cutoff) if cutoff else 0}


@router.post("/library/archive/restore")
def archive_restore(body: RestoreBody, request: Request):
    result = db.restore_archived(body.batch_id)
    if not result["restored"] and not result["skipped"]:
        raise HTTPException(404, "no archived rows for that batch")
    audit.record("library.archive_restore", actor=_actor(request),
                 target=body.batch_id, detail=result)
    return {**result, "batch_id": body.batch_id}


@router.post("/library/archive/prune")
def archive_prune(body: ArchivePruneBody, request: Request):
    """Permanently delete archived rows — expired ones, or all of them."""
    if body.all:
        removed = db.prune_archive(None)
    else:
        cutoff = purge.retention_cutoff()
        removed = db.prune_archive(cutoff) if cutoff is not None else 0
    audit.record("library.archive_prune", actor=_actor(request),
                 detail={"removed": removed, "all": body.all})
    return {"removed": removed, "all": body.all}
