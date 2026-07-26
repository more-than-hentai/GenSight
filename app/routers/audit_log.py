"""Audit log and runtime status endpoints (admin-only via middleware)."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import audit, group_presets
from ..scanner import manager
from ..watcher import watch_manager

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
def get_audit(action: str = "", actor: str = "", q: str = "",
              since: float | None = None, offset: int = 0, limit: int = 100):
    total, items = audit.query(action=action, actor=actor, q=q, since=since,
                               offset=offset, limit=limit)
    return {"total": total, "offset": offset, "items": items,
            "actions": audit.actions()}


@router.get("/audit/export")
def export_audit(action: str = "", actor: str = "", q: str = ""):
    _total, items = audit.query(action=action, actor=actor, q=q, limit=1000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts", "actor", "action", "target", "ok", "detail"])
    for r in items:
        writer.writerow([r["ts"], r["actor"], r["action"], r["target"] or "",
                         r["ok"], r["detail"] or ""])
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="gensight_audit.csv"'},
    )


@router.get("/status/workers")
def worker_status():
    """Live concurrency picture: scan slots, workers and the watcher."""
    return {"scan": manager.concurrency(), "watcher": watch_manager.status()}


@router.get("/groups/presets")
def list_presets():
    return {"presets": {name: group_presets.entries(name)
                        for name in group_presets.PRESETS}}
