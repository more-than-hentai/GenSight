"""Audit log and runtime status endpoints (admin-only via middleware)."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime

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


_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Defuse spreadsheet formula injection.

    Audit rows carry attacker-influenced text (a failed login can supply
    any username), and Excel/Sheets evaluate cells starting with = + - @.
    """
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in _CSV_TRIGGERS else text


@router.get("/audit/export")
def export_audit(action: str = "", actor: str = "", q: str = "",
                 since: float | None = None):
    """Stream every matching entry — an export that stops at a page
    boundary would be a misleading audit trail."""

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts", "iso", "actor", "action", "target", "ok",
                         "detail"])
        yield buf.getvalue()
        for r in audit.iter_all(action=action, actor=actor, q=q, since=since):
            buf.seek(0)
            buf.truncate(0)
            writer.writerow([
                r["ts"],
                datetime.fromtimestamp(r["ts"]).isoformat(timespec="seconds"),
                _csv_safe(r["actor"]), _csv_safe(r["action"]),
                _csv_safe(r["target"]), r["ok"],
                _csv_safe(json.dumps(r["detail"], ensure_ascii=False)
                          if isinstance(r["detail"], (dict, list))
                          else r["detail"]),
            ])
            yield buf.getvalue()

    return StreamingResponse(
        rows(), media_type="text/csv",
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
