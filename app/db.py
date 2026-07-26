"""SQLite persistence layer.

Stores every scanned image (metadata survives restarts), watch folders,
and auto-classification group rules. Connections are per-thread with
WAL journaling so scan workers, the watcher thread and API requests
can read/write concurrently.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config, imghash

_local = threading.local()
# Guards first-time schema creation: concurrent CREATE TABLE IF NOT
# EXISTS from multiple threads can still race inside SQLite.
_schema_lock = threading.Lock()

# Monotonic library change counter. Bumped on every mutation so
# consumers (stats cache, future views) can cheaply detect staleness
# without re-reading the tables.
_version_lock = threading.Lock()
_data_version = 0


def bump_version() -> None:
    global _data_version
    with _version_lock:
        _data_version += 1


def data_version() -> int:
    with _version_lock:
        return _data_version

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
  path            TEXT PRIMARY KEY,
  filename        TEXT NOT NULL,
  mtime           REAL,
  size            INTEGER,
  tool            TEXT DEFAULT 'unknown',
  prompt          TEXT NOT NULL DEFAULT '',
  negative_prompt TEXT NOT NULL DEFAULT '',
  params          TEXT NOT NULL DEFAULT '{}',
  phash           TEXT,
  error           TEXT,
  rating          INTEGER NOT NULL DEFAULT 0,
  favorite        INTEGER NOT NULL DEFAULT 0,
  group_name      TEXT,
  tags            TEXT,
  scanned_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_images_tool  ON images(tool);
CREATE INDEX IF NOT EXISTS idx_images_group ON images(group_name);
CREATE INDEX IF NOT EXISTS idx_images_phash ON images(phash);

CREATE TABLE IF NOT EXISTS watches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  directory     TEXT UNIQUE NOT NULL,
  recursive     INTEGER NOT NULL DEFAULT 1,
  enabled       INTEGER NOT NULL DEFAULT 1,
  poll_interval REAL NOT NULL DEFAULT 30,
  last_scan     REAL
);

CREATE TABLE IF NOT EXISTS groups (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  name     TEXT UNIQUE NOT NULL,
  pattern  TEXT NOT NULL,
  is_regex INTEGER NOT NULL DEFAULT 0,
  target   TEXT NOT NULL DEFAULT 'prompt'
);

CREATE TABLE IF NOT EXISTS trash (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  original_path TEXT NOT NULL,
  trash_path    TEXT NOT NULL,
  item          TEXT NOT NULL,
  trashed_at    REAL NOT NULL
);

-- Login accounts (optional auth). Salts/hashes live here rather than in
-- settings.json so credential writes get the same WAL-protected
-- transactions as everything else in the app, instead of a JSON
-- read-modify-write that can lose a concurrent update; it also keeps
-- secrets out of a config file an admin might paste into a support
-- request. `version` bumps on every write and backs session
-- invalidation (auth.revoke_sessions / session_info).
CREATE TABLE IF NOT EXISTS users (
  username      TEXT PRIMARY KEY,
  salt          TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',
  version       INTEGER NOT NULL DEFAULT 1
);
"""

# Columns added after the initial release; applied via ALTER TABLE so
# existing databases upgrade in place.
_MIGRATIONS = [
    ("images", "quality_score", "ALTER TABLE images ADD COLUMN quality_score REAL"),
    ("images", "quality_issues", "ALTER TABLE images ADD COLUMN quality_issues TEXT"),
    ("images", "content_rating", "ALTER TABLE images ADD COLUMN content_rating TEXT"),
]

# Whitelisted sort keys; the API accepts a comma-separated chain
# ("mtime_desc,rating,name") applied as 1st/2nd/3rd ORDER BY level.
_SORT_KEYS = {
    "recent": "scanned_at DESC",
    "oldest": "scanned_at ASC",
    "mtime_desc": "mtime DESC",
    "mtime_asc": "mtime ASC",
    "rating": "rating DESC",
    "rating_asc": "rating ASC",
    "quality": "quality_score ASC NULLS LAST",
    "quality_desc": "quality_score DESC NULLS LAST",
    "name": "filename COLLATE NOCASE ASC",
    "name_desc": "filename COLLATE NOCASE DESC",
    "size_desc": "size DESC",
    "size_asc": "size ASC",
}


# Hard ceiling for any single query. Exports intentionally ask for a
# very large page; anything above this is a bug or an abusive caller.
MAX_QUERY_LIMIT = 1_000_000


def _clamp(value, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, n))


def _order_clause(sort: str) -> str:
    parts = [_SORT_KEYS[s.strip()] for s in (sort or "").split(",")
             if s.strip() in _SORT_KEYS]
    return ", ".join(parts) if parts else "scanned_at DESC"


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    conn.commit()


def _db_path() -> Path:
    return Path(config.DATA_DIR) / "gensight.db"


def connect() -> sqlite3.Connection:
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    key = str(_db_path())
    conn = cache.get(key)
    if conn is None:
        Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
        # Serialize connection setup: switching a fresh DB to WAL and
        # creating the schema can deadlock (immediate SQLITE_BUSY, the
        # busy timeout does not apply) against a concurrent writer.
        with _schema_lock:
            conn = sqlite3.connect(key, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            for attempt in range(5):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.executescript(SCHEMA)
                    _migrate(conn)
                    break
                except sqlite3.OperationalError:
                    if attempt == 4:
                        raise
                    time.sleep(0.2)
        cache[key] = conn
    return conn


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["params"] = json.loads(d.get("params") or "{}")
    except json.JSONDecodeError:
        d["params"] = {}
    if d.get("tags"):
        try:
            d["tags"] = json.loads(d["tags"])
        except json.JSONDecodeError:
            d["tags"] = []
    if d.get("quality_issues"):
        try:
            d["quality_issues"] = json.loads(d["quality_issues"])
        except json.JSONDecodeError:
            d["quality_issues"] = []
    # Keep the shape the frontend already understands
    d["file"] = d["path"]
    d["favorite"] = bool(d["favorite"])
    return d


# ---------------------------------------------------------------- images


def upsert_image(r: dict, phash: str | None = None) -> None:
    p = Path(r["file"])
    try:
        st = p.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        mtime, size = None, None
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO images
               (path, filename, mtime, size, tool, prompt, negative_prompt,
                params, phash, error, scanned_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 filename=excluded.filename, mtime=excluded.mtime,
                 size=excluded.size, tool=excluded.tool,
                 prompt=excluded.prompt,
                 negative_prompt=excluded.negative_prompt,
                 params=excluded.params,
                 phash=COALESCE(excluded.phash, images.phash),
                 error=excluded.error, scanned_at=excluded.scanned_at""",
            (
                str(p), r["filename"], mtime, size, r.get("tool", "unknown"),
                r.get("prompt", ""), r.get("negative_prompt", ""),
                json.dumps(r.get("params", {}), ensure_ascii=False),
                phash, r.get("error"), time.time(),
            ),
        )
    bump_version()


def has_image(path: str) -> bool:
    row = connect().execute(
        "SELECT 1 FROM images WHERE path=?", (path,)
    ).fetchone()
    return row is not None


def is_decoded_image(path: str) -> bool:
    """True only if the file was indexed AND actually decoded as an
    image during extraction (error IS NULL).

    A file merely *named* like an image (secrets.env.png) still gets a
    library row so scan counts stay honest, but it must never be served
    to a restricted account.
    """
    row = connect().execute(
        "SELECT 1 FROM images WHERE path=? AND error IS NULL", (path,)
    ).fetchone()
    return row is not None


def get_image(path: str) -> dict | None:
    row = connect().execute(
        "SELECT * FROM images WHERE path=?", (path,)
    ).fetchone()
    return _row_to_item(row) if row else None


def known_mtimes(prefix: str) -> dict[str, float]:
    """path -> mtime for all images under a directory (for incremental scans)."""
    rows = connect().execute(
        "SELECT path, mtime FROM images WHERE path LIKE ?",
        (prefix.rstrip("/") + "/%",),
    ).fetchall()
    return {r["path"]: r["mtime"] or 0 for r in rows}


def query_images(
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
) -> tuple[int, list[dict]]:
    where, args = [], []
    if directory:
        where.append("path LIKE ?")
        args.append(str(directory).rstrip("/") + "/%")
    if content_rating == "unrated":
        where.append("content_rating IS NULL")
    elif content_rating:
        where.append("content_rating=?")
        args.append(content_rating)
    if quality == "issues":
        where.append("quality_issues IS NOT NULL AND quality_issues != '[]'")
    elif quality == "low":
        where.append("quality_score IS NOT NULL AND quality_score < 50")
    elif quality == "unrated":
        where.append("quality_score IS NULL")
    if q:
        like = f"%{q}%"
        where.append(
            "(prompt LIKE ? OR negative_prompt LIKE ? OR filename LIKE ?"
            " OR params LIKE ? OR IFNULL(tags,'') LIKE ?)"
        )
        args += [like] * 5
    if tool:
        where.append("tool=?")
        args.append(tool)
    if favorite is not None:
        where.append("favorite=?")
        args.append(1 if favorite else 0)
    if min_rating:
        where.append("rating>=?")
        args.append(min_rating)
    if group:
        where.append("group_name=?")
        args.append(group)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    order = _order_clause(sort)
    # SQLite treats a negative LIMIT as "no limit", so a caller passing
    # -1 would stream the whole table. Clamp here rather than trusting
    # each caller (web API and MCP both reach this).
    limit = _clamp(limit, 1, MAX_QUERY_LIMIT)
    offset = max(0, int(offset or 0))
    conn = connect()
    total = conn.execute(f"SELECT COUNT(*) c FROM images {w}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM images {w} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return total, [_row_to_item(r) for r in rows]


def set_meta(
    path: str,
    rating: int | None = None,
    favorite: bool | None = None,
    group_name: str | None = None,
) -> dict | None:
    sets, args = [], []
    if rating is not None:
        sets.append("rating=?")
        args.append(max(0, min(5, int(rating))))
    if favorite is not None:
        sets.append("favorite=?")
        args.append(1 if favorite else 0)
    if group_name is not None:
        sets.append("group_name=?")
        args.append(group_name or None)
    if sets:
        conn = connect()
        with conn:
            conn.execute(
                f"UPDATE images SET {', '.join(sets)} WHERE path=?", args + [path]
            )
        bump_version()
    return get_image(path)


def set_tags(path: str, tags: list[str],
             content_rating: str | None = None) -> None:
    conn = connect()
    with conn:
        if content_rating is not None:
            conn.execute(
                "UPDATE images SET tags=?, content_rating=? WHERE path=?",
                (json.dumps(tags, ensure_ascii=False), content_rating, path),
            )
        else:
            conn.execute(
                "UPDATE images SET tags=? WHERE path=?",
                (json.dumps(tags, ensure_ascii=False), path),
            )
    bump_version()


def untagged_paths(limit: int | None = None) -> list[str]:
    sql = "SELECT path FROM images WHERE tags IS NULL AND error IS NULL"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r["path"] for r in connect().execute(sql).fetchall()]


def similar_images(path: str, max_distance: int = 10, limit: int = 50) -> list[dict]:
    max_distance = _clamp(max_distance, 0, 64)
    limit = _clamp(limit, 1, 1000)
    target = get_image(path)
    if not target or not target.get("phash"):
        return []
    rows = connect().execute(
        "SELECT * FROM images WHERE phash IS NOT NULL AND path != ?", (path,)
    ).fetchall()
    scored = []
    for r in rows:
        d = imghash.hamming(target["phash"], r["phash"])
        if d <= max_distance:
            item = _row_to_item(r)
            item["distance"] = d
            scored.append(item)
    scored.sort(key=lambda x: x["distance"])
    return scored[:limit]


def duplicate_groups(limit: int = 100) -> list[dict]:
    """Groups of images sharing an identical perceptual hash.

    The all-zero hash is excluded: every flat/gradient image collapses
    to it, which would lump unrelated solid-color images together."""
    limit = _clamp(limit, 1, 1000)
    conn = connect()
    hashes = conn.execute(
        """SELECT phash, COUNT(*) c FROM images
           WHERE phash IS NOT NULL AND phash != '0000000000000000'
           GROUP BY phash
           HAVING c > 1 ORDER BY c DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for h in hashes:
        rows = conn.execute(
            "SELECT * FROM images WHERE phash=? ORDER BY path", (h["phash"],)
        ).fetchall()
        out.append({"phash": h["phash"], "count": h["c"],
                    "items": [_row_to_item(r) for r in rows]})
    return out


def summary() -> dict:
    conn = connect()
    total = conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"]
    by_tool = {
        r["tool"]: r["c"]
        for r in conn.execute(
            "SELECT tool, COUNT(*) c FROM images GROUP BY tool"
        ).fetchall()
    }
    favorites = conn.execute(
        "SELECT COUNT(*) c FROM images WHERE favorite=1"
    ).fetchone()["c"]
    tagged = conn.execute(
        "SELECT COUNT(*) c FROM images WHERE tags IS NOT NULL"
    ).fetchone()["c"]
    return {"total": total, "by_tool": by_tool, "favorites": favorites,
            "tagged": tagged}


def cleanup_missing() -> int:
    """Drop library rows whose files no longer exist on disk."""
    conn = connect()
    rows = conn.execute("SELECT path FROM images").fetchall()
    gone = [(r["path"],) for r in rows if not Path(r["path"]).exists()]
    with conn:
        conn.executemany("DELETE FROM images WHERE path=?", gone)
    if gone:
        bump_version()
    return len(gone)


def group_names() -> list[str]:
    rows = connect().execute(
        "SELECT DISTINCT group_name FROM images WHERE group_name IS NOT NULL"
        " ORDER BY group_name"
    ).fetchall()
    return [r["group_name"] for r in rows]


def set_quality(path: str, score: float, issues: list[str]) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE images SET quality_score=?, quality_issues=? WHERE path=?",
            (score, json.dumps(issues), path),
        )
    bump_version()


def quality_pending_paths(limit: int | None = None) -> list[str]:
    sql = ("SELECT path FROM images WHERE quality_score IS NULL"
           " AND error IS NULL")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r["path"] for r in connect().execute(sql).fetchall()]


def update_path(old: str, new: str) -> None:
    """Reflect a file move in the library (organize feature)."""
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE images SET path=?, filename=? WHERE path=?",
            (new, Path(new).name, old),
        )
    bump_version()


def delete_image_row(path: str) -> dict | None:
    item = get_image(path)
    if item:
        conn = connect()
        with conn:
            conn.execute("DELETE FROM images WHERE path=?", (path,))
        bump_version()
    return item


# ---------------------------------------------------------------- trash


def trash_add(original: str, trash_path: str, item: dict) -> dict:
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO trash(original_path, trash_path, item, trashed_at)"
            " VALUES (?,?,?,?)",
            (original, trash_path,
             json.dumps(item, ensure_ascii=False, default=str), time.time()),
        )
    return {"id": cur.lastrowid, "original_path": original,
            "trash_path": trash_path}


def trash_list() -> list[dict]:
    rows = connect().execute(
        "SELECT id, original_path, trash_path, trashed_at FROM trash"
        " ORDER BY trashed_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def trash_get(trash_id: int) -> dict | None:
    row = connect().execute(
        "SELECT * FROM trash WHERE id=?", (trash_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["item"] = json.loads(d["item"])
    except json.JSONDecodeError:
        d["item"] = {}
    return d


def trash_remove(trash_id: int) -> None:
    conn = connect()
    with conn:
        conn.execute("DELETE FROM trash WHERE id=?", (trash_id,))


def restore_image_row(item: dict, new_path: str) -> None:
    """Re-insert a trashed row (path may differ if the original was taken)."""
    item = dict(item)
    item["file"] = new_path
    item["filename"] = Path(new_path).name
    upsert_image(item, item.get("phash"))
    set_meta(new_path, rating=item.get("rating"),
             favorite=item.get("favorite"), group_name=item.get("group_name"))
    if item.get("tags"):
        set_tags(new_path, item["tags"], item.get("content_rating"))
    if item.get("quality_score") is not None:
        set_quality(new_path, item["quality_score"],
                    item.get("quality_issues") or [])


# ---------------------------------------------------------------- watches


def list_watches() -> list[dict]:
    return [dict(r) for r in connect().execute(
        "SELECT * FROM watches ORDER BY id"
    ).fetchall()]


def add_watch(directory: str, recursive: bool = True,
              poll_interval: float = 30) -> dict:
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO watches(directory, recursive, poll_interval)
               VALUES (?,?,?)
               ON CONFLICT(directory) DO UPDATE SET
                 recursive=excluded.recursive,
                 poll_interval=excluded.poll_interval, enabled=1""",
            (directory, 1 if recursive else 0, max(5, float(poll_interval))),
        )
    row = conn.execute(
        "SELECT * FROM watches WHERE directory=?", (directory,)
    ).fetchone()
    return dict(row)


def update_watch(watch_id: int, enabled: bool | None = None,
                 poll_interval: float | None = None) -> None:
    sets, args = [], []
    if enabled is not None:
        sets.append("enabled=?")
        args.append(1 if enabled else 0)
    if poll_interval is not None:
        sets.append("poll_interval=?")
        args.append(max(5, float(poll_interval)))
    if sets:
        conn = connect()
        with conn:
            conn.execute(
                f"UPDATE watches SET {', '.join(sets)} WHERE id=?",
                args + [watch_id],
            )


def touch_watch(watch_id: int) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE watches SET last_scan=? WHERE id=?", (time.time(), watch_id)
        )


def delete_watch(watch_id: int) -> None:
    conn = connect()
    with conn:
        conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))


# ---------------------------------------------------------------- groups


def list_groups() -> list[dict]:
    return [dict(r) for r in connect().execute(
        "SELECT * FROM groups ORDER BY id"
    ).fetchall()]


def add_group(name: str, pattern: str, is_regex: bool = False,
              target: str = "prompt") -> dict:
    if is_regex:
        re.compile(pattern)  # raises re.error -> 400 upstream
    if target not in ("prompt", "filename", "model"):
        raise ValueError("target must be prompt, filename or model")
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO groups(name, pattern, is_regex, target)
               VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET pattern=excluded.pattern,
                 is_regex=excluded.is_regex, target=excluded.target""",
            (name, pattern, 1 if is_regex else 0, target),
        )
    row = conn.execute("SELECT * FROM groups WHERE name=?", (name,)).fetchone()
    return dict(row)


def delete_group(group_id: int) -> None:
    conn = connect()
    with conn:
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))


def apply_groups(overwrite: bool = False) -> int:
    """Assign group_name by matching each rule against prompt/filename/model.
    First matching rule (by id order) wins. Returns updated row count."""
    rules = list_groups()
    if not rules:
        return 0
    compiled = []
    for g in rules:
        if g["is_regex"]:
            try:
                rx = re.compile(g["pattern"], re.IGNORECASE)
            except re.error:
                continue
            compiled.append((g["name"], g["target"], rx, None))
        else:
            compiled.append((g["name"], g["target"], None, g["pattern"].lower()))

    conn = connect()
    where = "" if overwrite else "WHERE group_name IS NULL"
    rows = conn.execute(
        f"SELECT path, prompt, filename, params, group_name FROM images {where}"
    ).fetchall()
    updates = []
    for r in rows:
        try:
            model = json.loads(r["params"] or "{}").get("Model", "")
        except json.JSONDecodeError:
            model = ""
        haystacks = {"prompt": r["prompt"] or "", "filename": r["filename"] or "",
                     "model": str(model)}
        for name, target, rx, sub in compiled:
            hay = haystacks.get(target, "")
            if (rx.search(hay) if rx else sub in hay.lower()):
                if r["group_name"] != name:
                    updates.append((name, r["path"]))
                break
    with conn:
        conn.executemany("UPDATE images SET group_name=? WHERE path=?", updates)
    if updates:
        bump_version()
    return len(updates)


# ---------------------------------------------------------------- users


def list_users() -> list[dict]:
    rows = connect().execute(
        "SELECT username, salt, password_hash, role, version FROM users"
        " ORDER BY username"
    ).fetchall()
    return [dict(r) for r in rows]


def get_user(username: str) -> dict | None:
    row = connect().execute(
        "SELECT username, salt, password_hash, role, version FROM users"
        " WHERE username=?",
        (username,),
    ).fetchone()
    return dict(row) if row else None


def upsert_user(username: str, salt: str, password_hash: str,
                role: str) -> int | None:
    """Insert or replace an account in one atomic statement.

    The version bump (`users.version + 1`) is computed by SQLite in the
    UPSERT itself rather than pre-read in Python: a pre-read-then-write
    lets two concurrent writes to the same username both compute the
    same "next" version, so the loser's credentials win but the
    version looks unchanged — a session minted against the loser's
    write would then wrongly keep validating. Doing the increment as
    part of the single write closes that: SQLite serializes writers, so
    the second writer's `users.version + 1` is evaluated against the
    first writer's already-committed row.

    The same statement also refuses, atomically, to demote an account
    from admin if that would leave zero admins — closing a race where
    two concurrent demotions (or an admin deleting the last other
    admin while a third request demotes them) could otherwise both
    pass a Python-level "count the admins" check before either commits.

    Returns the new version, or None if the write was refused because
    it would have removed the last admin.
    """
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO users(username, salt, password_hash, role, version)
               VALUES (?,?,?,?,1)
               ON CONFLICT(username) DO UPDATE SET
                 salt=excluded.salt, password_hash=excluded.password_hash,
                 role=excluded.role, version=users.version + 1
               WHERE NOT (
                 users.role = 'admin' AND excluded.role != 'admin' AND
                 (SELECT COUNT(*) FROM users u2
                  WHERE u2.role = 'admin' AND u2.username != users.username) = 0
               )""",
            (username, salt, password_hash, role),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT version FROM users WHERE username=?", (username,)
        ).fetchone()
    return row["version"] if row else None


def import_legacy_users(users: list[dict]) -> None:
    """Bulk-insert accounts migrated from settings.json as ONE atomic
    transaction — all land or none do.

    Without this, a caller inserting them one upsert_user() call at a
    time (each its own commit) could be interrupted partway: the users
    table would end up non-empty (so the "already migrated, skip"
    check in auth.py treats the migration as done) while some legacy
    accounts were never imported and their settings.json entry was
    never cleared either — silently and permanently unreachable.
    """
    if not users:
        return
    conn = connect()
    with conn:
        for u in users:
            conn.execute(
                """INSERT INTO users(username, salt, password_hash, role, version)
                   VALUES (?,?,?,?,1)
                   ON CONFLICT(username) DO NOTHING""",
                (u["username"], u.get("salt", ""), u.get("password_hash", ""),
                 u.get("role", "admin")),
            )


def delete_user_row(username: str) -> str:
    """Delete an account. Returns 'deleted', 'not_found', or
    'last_admin'.

    The invariant ("at least one admin remains") is enforced inside the
    DELETE's own WHERE clause rather than a separate count-then-delete
    in Python, so two admins concurrently deleting each other cannot
    both pass the check before either commits: SQLite serializes the
    two DELETE statements, and the second one's subquery re-counts
    admins against the first's already-committed result.
    """
    conn = connect()
    with conn:
        cur = conn.execute(
            """DELETE FROM users WHERE username = ? AND (
                 role != 'admin' OR
                 (SELECT COUNT(*) FROM users
                  WHERE role='admin' AND username != ?) > 0
               )""",
            (username, username),
        )
        if cur.rowcount > 0:
            return "deleted"
        # rowcount 0 means either the username never existed, or it
        # exists but the guard above blocked the delete (last admin).
        # This lookup runs before the transaction started above
        # commits, so nothing else can have changed the row in
        # between — telling the two cases apart needs no pre-delete
        # existence check (and the race window that would create).
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ).fetchone()
    return "last_admin" if exists else "not_found"
