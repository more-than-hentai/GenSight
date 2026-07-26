"""Audit log — who did what, kept in SQLite.

Separate from the application log on purpose: the app log is for
diagnosing behaviour and rotates away, while this is a durable record
of actions that changed state (scans, deletions, settings, accounts)
and must survive log rotation and restarts.

Writes never raise: an audit failure must not break the operation it
was recording.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import db

logger = logging.getLogger("gensight.audit")

# Keep the table bounded; pruning runs opportunistically on write.
MAX_ROWS = 100_000
_PRUNE_EVERY = 500
_write_count = 0
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL NOT NULL,
  actor  TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  target TEXT,
  detail TEXT,
  ok     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action);
"""


def _ensure(conn) -> None:
    conn.executescript(SCHEMA)


def record(
    action: str,
    *,
    actor: str = "",
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ok: bool = True,
) -> None:
    """Append one audit entry. Best effort — never raises."""
    global _write_count
    try:
        conn = db.connect()
        _ensure(conn)
        with conn:
            conn.execute(
                "INSERT INTO audit(ts, actor, action, target, detail, ok)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), actor or "system", action, target,
                 json.dumps(detail, ensure_ascii=False, default=str)
                 if detail else None,
                 1 if ok else 0),
            )
        with _lock:
            _write_count += 1
            due = _write_count % _PRUNE_EVERY == 0
        if due:
            prune()
    except Exception:  # noqa: BLE001 - auditing must not break the action
        logger.exception("audit write failed for %s", action)


def prune(max_rows: int = MAX_ROWS) -> int:
    """Drop the oldest rows beyond max_rows. Returns rows removed."""
    try:
        conn = db.connect()
        _ensure(conn)
        total = conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
        if total <= max_rows:
            return 0
        with conn:
            cur = conn.execute(
                "DELETE FROM audit WHERE id IN ("
                "  SELECT id FROM audit ORDER BY id ASC LIMIT ?)",
                (total - max_rows,),
            )
        return cur.rowcount or 0
    except Exception:  # noqa: BLE001
        logger.exception("audit prune failed")
        return 0


def query(
    action: str = "",
    actor: str = "",
    q: str = "",
    since: float | None = None,
    offset: int = 0,
    limit: int = 100,
) -> tuple[int, list[dict]]:
    conn = db.connect()
    _ensure(conn)
    where, args = [], []
    if action:
        where.append("action LIKE ?")
        args.append(action.rstrip("*") + "%")
    if actor:
        where.append("actor = ?")
        args.append(actor)
    if q:
        where.append("(target LIKE ? OR detail LIKE ?)")
        args += [f"%{q}%"] * 2
    if since:
        where.append("ts >= ?")
        args.append(float(since))
    w = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    total = conn.execute(f"SELECT COUNT(*) c FROM audit {w}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM audit {w} ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except json.JSONDecodeError:
                pass
        d["ok"] = bool(d["ok"])
        items.append(d)
    return total, items


def actions() -> list[str]:
    conn = db.connect()
    _ensure(conn)
    return [r["action"] for r in conn.execute(
        "SELECT DISTINCT action FROM audit ORDER BY action").fetchall()]
