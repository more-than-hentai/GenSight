"""Folder auto-watch: real-time events via watchdog when available,
with periodic polling sweeps as the consistency fallback.

Watches live in the DB (table `watches`) and are managed from the
settings UI. Each sweep is incremental — only files that are new or
whose mtime changed since the last known state are re-extracted.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from . import audit, db, metadata
from .scanner import process_and_store

logger = logging.getLogger("gensight.watcher")

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    HAVE_WATCHDOG = True
except ImportError:  # pragma: no cover - watchdog is in requirements.txt
    HAVE_WATCHDOG = False

TICK_SECONDS = 5.0
# Skip files modified less than this many seconds ago — they may still
# be mid-write by the generator.
SETTLE_SECONDS = 2.0


if HAVE_WATCHDOG:

    class _Handler(FileSystemEventHandler):
        def __init__(self, manager: "WatchManager"):
            self._manager = manager

        def _enqueue(self, raw_path: str) -> None:
            p = Path(raw_path)
            if p.suffix.lower() in metadata.SUPPORTED_EXTENSIONS:
                self._manager.enqueue(p)

        def on_created(self, event):
            if not event.is_directory:
                self._enqueue(event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                self._enqueue(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                self._enqueue(event.dest_path)


class WatchManager:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: set[Path] = set()
        self._pending_lock = threading.Lock()
        # watch_id -> (observer, signature) so config changes recreate it
        self._observers: dict[int, tuple[object, tuple]] = {}
        self.last_error: str | None = None
        # Start at 0 so the first tick applies retention immediately —
        # a restart should not postpone expiry by another hour.
        self._last_archive_prune = 0.0

    # -- public API --------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gensight-watcher"
        )
        self._thread.start()
        logger.info(
            "watcher started: %s, tick=%.0fs, %d watch(es) configured",
            "watchdog + polling" if HAVE_WATCHDOG else "polling only",
            TICK_SECONDS, len(db.list_watches()),
        )

    def stop(self) -> None:
        self._stop.set()
        for observer, _sig in self._observers.values():
            try:
                observer.stop()
            except Exception:  # noqa: BLE001
                pass
        self._observers.clear()

    def enqueue(self, path: Path) -> None:
        with self._pending_lock:
            self._pending.add(path)

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "realtime": HAVE_WATCHDOG,
            "pending": len(self._pending),
            "last_error": self.last_error,
        }

    # -- internals ---------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                watches = db.list_watches()
                self._sync_observers(watches)
                self._drain_pending()
                now = time.time()
                for w in watches:
                    if not w["enabled"]:
                        continue
                    if (w["last_scan"] or 0) + w["poll_interval"] <= now:
                        # Isolated per watch: one bad directory (gone,
                        # unreadable, or a root this build cannot scope)
                        # must not stop the remaining watches or the
                        # retention prune below.
                        try:
                            self._sweep(w)
                        except Exception as e:  # noqa: BLE001
                            self.last_error = f"{type(e).__name__}: {e}"
                            logger.exception("watch sweep failed for %s",
                                             w.get("directory"))
                self._prune_archive(now)
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                self.last_error = f"{type(e).__name__}: {e}"
                logger.exception("watcher tick failed")

    def _prune_archive(self, now: float) -> None:
        """Apply the archive retention window, at most hourly.

        Rides this existing tick rather than adding a scheduler thread —
        purged records must not accumulate forever, but the exact moment
        they expire does not matter.
        """
        if now - self._last_archive_prune < 3600.0:
            return
        try:
            from . import purge

            purge.prune_expired()
            # Stamped only on success: advancing first would turn a
            # transient SQLite error into an hour-long gap.
            self._last_archive_prune = now
        except Exception:  # noqa: BLE001 - retention is best-effort
            logger.exception("archive retention prune failed")

    def _sync_observers(self, watches: list[dict]) -> None:
        if not HAVE_WATCHDOG:
            return
        wanted: dict[int, tuple] = {
            w["id"]: (w["directory"], bool(w["recursive"]))
            for w in watches
            if w["enabled"] and Path(w["directory"]).is_dir()
        }
        for wid in list(self._observers):
            if wanted.get(wid) != self._observers[wid][1]:
                self._observers.pop(wid)[0].stop()
        for wid, sig in wanted.items():
            if wid in self._observers:
                continue
            directory, recursive = sig
            try:
                observer = Observer()
                observer.schedule(_Handler(self), directory, recursive=recursive)
                observer.daemon = True
                observer.start()
                self._observers[wid] = (observer, sig)
            except Exception as e:  # noqa: BLE001 - fall back to polling only
                logger.warning("watchdog observer failed for %s: %s", directory, e)

    def _drain_pending(self) -> None:
        with self._pending_lock:
            batch, self._pending = self._pending, set()
        if not batch:
            return
        now = time.time()
        ingested = deferred = 0
        for path in batch:
            try:
                if not path.is_file():
                    continue
                if now - path.stat().st_mtime < SETTLE_SECONDS:
                    self.enqueue(path)  # still being written; retry next tick
                    deferred += 1
                    continue
                process_and_store(path)
                ingested += 1
            except OSError:
                continue
        if ingested or deferred:
            logger.info(
                "watcher realtime batch: %d ingested, %d deferred (still writing)",
                ingested, deferred,
            )
        if ingested:
            self._autotag()

    def _autotag(self) -> None:
        """Hand newly ingested files to the batch tagger, if enabled."""
        try:
            from . import tagger

            tagger.tagger_manager.autorun()
        except Exception:  # noqa: BLE001 - a watch tick must survive this
            logger.exception("auto tagging hook failed")

    def _sweep(self, watch: dict) -> None:
        """Incremental polling scan of one watch directory."""
        root = Path(watch["directory"])
        if not root.is_dir():
            db.touch_watch(watch["id"])
            return
        recursive = bool(watch["recursive"])
        # The scope must match what the walker below actually visits. Asking
        # for every descendant row while only walking the immediate children
        # makes subdirectory rows look absent on every sweep.
        known = db.known_mtimes(str(root), recursive=recursive)
        processed = 0
        walker = (
            os.walk(root, onerror=lambda e: None)
            if recursive
            else [(str(root), [], [p.name for p in root.iterdir() if p.is_file()])]
        )
        now = time.time()
        for dirpath, _dirs, filenames in walker:
            if self._stop.is_set():
                return
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in metadata.SUPPORTED_EXTENSIONS:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if now - mtime < SETTLE_SECONDS:
                    continue
                if abs(known.get(str(p), -1) - mtime) < 0.5:
                    continue  # unchanged
                process_and_store(p)
                processed += 1
        db.touch_watch(watch["id"])
        if processed:
            logger.info(
                "watch sweep %s: %d new/changed file(s) ingested "
                "(%d known, recursive=%s, interval=%.0fs) in %.1fs",
                root, processed, len(known), bool(watch["recursive"]),
                watch["poll_interval"], time.time() - now,
            )
            audit.record("watch.ingest", target=str(root),
                         detail={"ingested": processed})
            self._autotag()
        else:
            logger.debug("watch sweep %s: no changes (%d known)", root, len(known))


watch_manager = WatchManager()
