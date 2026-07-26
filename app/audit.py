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
# Entries carry attacker-influenced text (usernames from failed logins,
# filesystem paths, prompts). Row count alone does not bound disk use,
# so cap the fields too.
MAX_FIELD_CHARS = 512
MAX_DETAIL_CHARS = 4000
_write_count = 0
_lock = threading.Lock()


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"

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
        detail_json = (
            _clip(json.dumps(detail, ensure_ascii=False, default=str),
                  MAX_DETAIL_CHARS)
            if detail else None
        )
        conn = db.connect()
        _ensure(conn)
        with conn:
            conn.execute(
                "INSERT INTO audit(ts, actor, action, target, detail, ok)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(),
                 _clip(actor, MAX_FIELD_CHARS) or "system",
                 _clip(action, MAX_FIELD_CHARS),
                 _clip(target, MAX_FIELD_CHARS),
                 detail_json,
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


def _filters(action: str, actor: str, q: str,
             since: float | None) -> tuple[str, list]:
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
    return ("WHERE " + " AND ".join(where)) if where else "", args


def _row(r) -> dict:
    d = dict(r)
    if d.get("detail"):
        try:
            d["detail"] = json.loads(d["detail"])
        except json.JSONDecodeError:
            pass
    d["ok"] = bool(d["ok"])
    return d


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
    w, args = _filters(action, actor, q, since)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    total = conn.execute(f"SELECT COUNT(*) c FROM audit {w}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM audit {w} ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return total, [_row(r) for r in rows]


def iter_all(action: str = "", actor: str = "", q: str = "",
             since: float | None = None, chunk: int = 1000):
    """Yield every matching entry, newest first.

    Export must not silently stop at a page boundary — an audit trail
    that quietly omits older rows is worse than no export.
    """
    conn = db.connect()
    _ensure(conn)
    w, args = _filters(action, actor, q, since)
    last_id = None
    while True:
        clause = w
        page_args = list(args)
        if last_id is not None:
            clause = (f"{w} AND id < ?" if w else "WHERE id < ?")
            page_args.append(last_id)
        rows = conn.execute(
            f"SELECT * FROM audit {clause} ORDER BY id DESC LIMIT ?",
            page_args + [chunk],
        ).fetchall()
        if not rows:
            return
        for r in rows:
            yield _row(r)
        last_id = rows[-1]["id"]


def actions() -> list[str]:
    conn = db.connect()
    _ensure(conn)
    return [r["action"] for r in conn.execute(
        "SELECT DISTINCT action FROM audit ORDER BY action").fetchall()]
