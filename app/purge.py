"""Directory-scoped library record purge.

Removing a registered scan directory deliberately leaves the catalog
alone (the library is independent of the registration list), which is
why records survive an unregister. This module is the explicit,
scoped way to remove them.

Everything is preview-first: a purge is computed, classified and priced
(how many tagged / rated / quality-analysed rows would go) and handed
back with a token. Only that exact, unchanged plan can then be executed.
Rows are archived rather than deleted — see db.archive_rows.

Files on disk are never touched here. `files_deleted: 0` is reported
explicitly so the UI can say so out loud.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from . import config, db, files

logger = logging.getLogger("gensight.purge")

TOKEN_TTL = 300.0  # a plan older than this must be re-previewed
MODES = ("all", "missing")
# A plan retains the full target path list so that execute() deletes exactly
# what the preview counted. On a large root that list is tens of megabytes, and
# expiry alone does not bound it — previews inside one TTL window accumulate.
MAX_PLANS = 8
# Plan count alone does not bound memory: eight million-row roots is a lot of
# path strings. Evict oldest-first until the retained total fits too.
MAX_RETAINED_TARGETS = 500_000

_lock = threading.Lock()
# Plans live in this process only, as does db's revision counter. GenSight
# runs a single uvicorn worker (no --workers anywhere in run.sh, Dockerfile
# or compose), so threads share both. Adding workers would break this:
# a preview served by one worker could not be executed by another, and
# neither would see the other's writes when validating a plan. Persist plans
# and the revision in SQLite before scaling out.
_plans: dict[str, dict] = {}


class PurgeError(RuntimeError):
    """Refused for a reason the caller should surface verbatim."""


def _classify(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split rows into (present, missing, inaccessible).

    `Path.exists()` collapses "not there" and "cannot tell" into False,
    which is exactly the distinction that matters before deleting: an
    unmounted drive or an unreadable parent would otherwise look like
    thousands of deleted files. os.stat's errno keeps them apart.
    """
    present, missing, inaccessible = [], [], []
    for p in paths:
        try:
            os.stat(p)
            present.append(p)
        except FileNotFoundError:
            missing.append(p)
        except NotADirectoryError:
            # A path component is no longer a directory — the file is
            # genuinely unreachable at this path.
            missing.append(p)
        except OSError:
            inaccessible.append(p)
    return present, missing, inaccessible


def _overlaps(root: str) -> dict:
    """Registered roots, watches and running scans that touch `root`.

    Reported so the operator can see that a purge would fight an active
    ingester, and so nested registrations are visible before they are
    swept up.
    """
    def related(other: str) -> bool:
        a, b = root.rstrip("/") + "/", str(other).rstrip("/") + "/"
        return a.startswith(b) or b.startswith(a)

    settings = config.load_settings()
    return {
        "registered_directories": [d for d in settings["directories"]
                                   if related(d)],
        "watches": [
            {"directory": w["directory"], "enabled": bool(w["enabled"])}
            for w in db.list_watches() if related(w["directory"])
        ],
        "active_scans": [],  # filled by the router (avoids a scanner import cycle)
    }


def plan(root: str, recursive: bool = True, mode: str = "all") -> dict:
    """Compute a purge plan and register it under a one-shot token."""
    if mode not in MODES:
        raise PurgeError(f"mode must be one of {MODES}")
    given = str(root or "")
    if not given.strip():
        raise PurgeError("root is required")
    # Surrounding whitespace is NOT stripped. A directory may legally be
    # named "set " on POSIX, and trimming would silently retarget the purge
    # at the different directory "set" — deleting records the operator never
    # named. Pasted whitespace instead yields zero targets, which the
    # preview reports (and `padded` lets the UI explain why).
    padded = given != given.strip()
    raw = Path(given).expanduser()
    # Dot segments are refused rather than folded away. Lexical normpath
    # would rewrite "/safe/link/.." to "/safe", but if `link` is a symlink
    # to /other/sub the OS means "/other" — so folding could scope the purge
    # at a directory the operator never named. resolve() is not the answer
    # either: it rewrites symlinks the scanner stored unresolved, and a root
    # that no longer exists cannot be resolved at all.
    if any(seg in (".", "..") for seg in str(raw).split(os.sep)):
        raise PurgeError(
            "root must not contain '.' or '..' segments; give the real path")
    canonical = os.path.normpath(str(raw))
    if canonical in ("/", ""):
        raise PurgeError("refusing to purge the filesystem root")
    if not os.path.isabs(canonical):
        raise PurgeError(f"root must be an absolute path: {canonical}")

    # Captured BEFORE reading rows, so a write that lands while the preview
    # is being computed invalidates the plan. Capturing it afterwards would
    # fold such a write into the plan's own version and let execute() accept
    # a plan whose counts were already stale when shown.
    version = db.data_version()
    clause, args = db.path_scope(canonical, recursive)
    conn = db.connect()
    rows = [r["path"] for r in conn.execute(
        f"SELECT path FROM images WHERE {clause}", args
    ).fetchall()]
    present, missing, inaccessible = _classify(rows)
    targets = rows if mode == "all" else missing

    # Price the operation in terms of work that would need redoing.
    at_risk = {"tagged": 0, "content_rated": 0, "quality_analysed": 0,
               "grouped": 0, "rated": 0, "favorite": 0}
    if targets:
        for i in range(0, len(targets), 500):
            chunk = targets[i:i + 500]
            marks = ",".join("?" * len(chunk))
            row = conn.execute(
                f"""SELECT
                      SUM(tags IS NOT NULL) tagged,
                      SUM(content_rating IS NOT NULL) content_rated,
                      SUM(quality_score IS NOT NULL) quality_analysed,
                      SUM(group_name IS NOT NULL) grouped,
                      SUM(rating > 0) rated,
                      SUM(favorite = 1) favorite
                    FROM images WHERE path IN ({marks})""",
                chunk,
            ).fetchone()
            for k in at_risk:
                at_risk[k] += row[k] or 0

    token = secrets.token_urlsafe(16)
    plan_data = {
        "token": token,
        "root": canonical,
        "recursive": recursive,
        "mode": mode,
        "total": len(rows),
        "present": len(present),
        "missing": len(missing),
        "inaccessible": len(inaccessible),
        "targets": len(targets),
        "padded_root": padded,
        "at_risk": at_risk,
        "overlaps": _overlaps(canonical),
        "files_deleted": 0,
        "data_version": version,
        "expires_at": time.time() + TOKEN_TTL,
    }
    with _lock:
        # Drop expired plans opportunistically so the dict cannot grow
        # without bound on an instance that previews a lot.
        now = time.time()
        for stale in [t for t, p in _plans.items() if p["expires_at"] < now]:
            _plans.pop(stale, None)
        _plans[token] = {**plan_data, "_targets": targets}
        # dict preserves insertion order, so the front is the oldest plan.
        def _retained() -> int:
            return sum(len(p["_targets"]) for p in _plans.values())

        while len(_plans) > 1 and (
            len(_plans) > MAX_PLANS or _retained() > MAX_RETAINED_TARGETS
        ):
            evicted = next(iter(_plans))
            _plans.pop(evicted, None)
            logger.info("evicted an unused purge plan (plans=%d, targets=%d)",
                        len(_plans), _retained())
    return plan_data


def get_plan(token: str) -> dict | None:
    """The stored plan for a token, without consuming it (the router
    needs the root to check for competing scans before executing)."""
    with _lock:
        stored = _plans.get(token)
    if stored is None or stored["expires_at"] < time.time():
        return None
    return {k: v for k, v in stored.items() if not k.startswith("_")}


def execute(token: str) -> dict:
    """Run a previously computed plan, if it is still exactly valid."""
    # Claim the token before validating anything. Looking it up and popping
    # it after the archive let two concurrent requests both pass validation
    # and both run the archive; popping first means the loser sees an
    # unknown token, which is what "single use" has to mean.
    with _lock:
        stored = _plans.pop(token, None)
    if stored is None:
        raise PurgeError("unknown or already-used purge token; preview again")
    if stored["expires_at"] < time.time():
        raise PurgeError("purge plan expired; preview again")
    if stored["data_version"] != db.data_version():
        raise PurgeError(
            "the library changed since the preview; preview again")
    if stored["mode"] == "missing" and stored["inaccessible"]:
        raise PurgeError(
            f"{stored['inaccessible']} row(s) under this path are currently "
            "unreadable, so missing files cannot be told apart from an "
            "offline mount or a permission problem — refusing to purge")

    targets = stored["_targets"]
    if stored["mode"] == "missing":
        # Re-stat rather than trust the preview: a remount or a restored
        # backup between preview and execute changes nothing in the
        # database, so the version check cannot see it, and archiving a
        # file that is present again would be plain wrong.
        back, still_gone, unreadable = _classify(targets)
        if back or unreadable:
            raise PurgeError(
                f"{len(back) + len(unreadable)} of these files are no longer "
                "missing (or became unreadable) since the preview; "
                "preview again")
        targets = still_gone

    batch_id = uuid.uuid4().hex[:12]
    reason = f"{stored['mode']} purge of {stored['root']}"
    try:
        archived = db.archive_rows(targets, reason, batch_id,
                                   expected_version=stored["data_version"])
    except db.RevisionConflict as e:
        raise PurgeError(str(e)) from e

    thumbs = 0
    try:
        thumbs = files.cleanup_thumbs()
    except Exception:  # noqa: BLE001 - cache tidy-up is not worth failing on
        logger.exception("thumbnail cleanup after purge failed")

    logger.info("purge %s: archived %d row(s) from %s (mode=%s, batch=%s)",
                batch_id, archived, stored["root"], stored["mode"], batch_id)
    return {
        "root": stored["root"], "mode": stored["mode"],
        "recursive": stored["recursive"],
        "archived": archived, "batch_id": batch_id,
        "thumbs_removed": thumbs, "files_deleted": 0,
    }


def retention_cutoff() -> float | None:
    """Timestamp before which archived rows may be pruned, or None when
    retention is disabled (keep until pruned by hand)."""
    days = config.load_settings().get("archive", {}).get("retention_days", 30)
    try:
        days = float(days)
    except (TypeError, ValueError):
        days = 30.0
    if days <= 0:
        return None
    return time.time() - days * 86400.0


def prune_expired() -> int:
    cutoff = retention_cutoff()
    if cutoff is None:
        return 0
    removed = db.prune_archive(cutoff)
    if removed:
        logger.info("archive retention: pruned %d expired row(s)", removed)
    return removed
