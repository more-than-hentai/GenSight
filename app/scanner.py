"""Scan job management.

Each scan job enumerates image files under a directory and extracts
metadata with a per-job worker pool. Multiple jobs can run
concurrently (bounded by settings.max_concurrent_jobs); worker counts
are adjustable per job from the UI so very large directories can be
tuned independently.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config, db, imghash, metadata

logger = logging.getLogger("gensight.scanner")

MAX_WORKERS = 32


def process_and_store(path: Path | str) -> dict[str, Any]:
    """Extract metadata + perceptual hash and persist to the library DB.

    Shared by scan jobs and the folder watcher. Never raises.
    """
    path = Path(path)
    try:
        item = metadata.extract(path)
    except Exception as e:  # noqa: BLE001
        item = {
            "file": str(path), "filename": path.name, "tool": "unknown",
            "prompt": "", "negative_prompt": "", "params": {}, "raw": {},
            "error": f"{type(e).__name__}: {e}",
        }
    phash = None if item["error"] else imghash.dhash(path)
    item["phash"] = phash
    try:
        db.upsert_image(item, phash)
    except Exception:  # noqa: BLE001 - DB hiccup must not kill the scan
        logger.exception("db upsert failed for %s", path)
    return item


class ScanJob:
    def __init__(self, directory: str, recursive: bool, workers: int):
        self.id = uuid.uuid4().hex[:12]
        self.directory = directory
        self.recursive = recursive
        self.workers = max(1, min(MAX_WORKERS, workers))
        self.status = "queued"  # queued | scanning | extracting | done | cancelled | error
        self.total = 0
        self.processed = 0
        self.with_metadata = 0
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------

    def run(self) -> None:
        self.started_at = time.time()
        try:
            self.status = "scanning"
            files = self._enumerate()
            self.total = len(files)
            if self._cancel.is_set():
                self.status = "cancelled"
                return
            self.status = "extracting"
            self._extract_all(files)
            self.status = "cancelled" if self._cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.finished_at = time.time()

    def cancel(self) -> None:
        self._cancel.set()

    # -- internals ---------------------------------------------------

    def _enumerate(self) -> list[Path]:
        """Walk the directory, skipping unreadable entries instead of failing."""
        root = Path(self.directory)
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")
        files: list[Path] = []

        def add_if_image(p: Path) -> None:
            try:
                if p.is_file() and p.suffix.lower() in metadata.SUPPORTED_EXTENSIONS:
                    files.append(p)
            except OSError:
                pass  # broken symlink, permission issue, etc.

        if self.recursive:
            for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: None):
                if self._cancel.is_set():
                    break
                for name in filenames:
                    add_if_image(Path(dirpath) / name)
        else:
            try:
                for p in root.iterdir():
                    if self._cancel.is_set():
                        break
                    add_if_image(p)
            except OSError as e:
                raise PermissionError(f"Cannot read directory: {root} ({e})") from e
        files.sort()
        return files

    def _extract_all(self, files: list[Path]) -> None:
        def work(path: Path) -> None:
            if self._cancel.is_set():
                return
            item = process_and_store(path)
            with self._lock:
                self.processed += 1
                if item["prompt"] or len(item["params"]) > 1:
                    self.with_metadata += 1

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(work, files))

    # -- serialization -----------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "directory": self.directory,
            "recursive": self.recursive,
            "workers": self.workers,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "with_metadata": self.with_metadata,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

# NOTE: per-job result browsing lives in the library now (db-backed);
# jobs only keep counters for progress display.


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, ScanJob] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._queue_cv = threading.Condition()
        self._pending: list[ScanJob] = []
        self._running = 0
        threading.Thread(target=self._dispatcher, daemon=True).start()

    def submit(self, directory: str, recursive: bool, workers: int) -> ScanJob:
        job = ScanJob(directory, recursive, workers)
        with self._lock:
            self.jobs[job.id] = job
            self._order.insert(0, job.id)
        with self._queue_cv:
            self._pending.append(job)
            self._queue_cv.notify()
        return job

    def _dispatcher(self) -> None:
        while True:
            with self._queue_cv:
                while not self._pending or self._running >= self._max_jobs():
                    self._queue_cv.wait(timeout=1.0)
                job = self._pending.pop(0)
                self._running += 1
            threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    def _run_job(self, job: ScanJob) -> None:
        try:
            job.run()
        finally:
            with self._queue_cv:
                self._running -= 1
                self._queue_cv.notify()

    @staticmethod
    def _max_jobs() -> int:
        return max(1, int(config.load_settings().get("max_concurrent_jobs", 2)))

    def get(self, job_id: str) -> ScanJob | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict]:
        return [self.jobs[j].summary() for j in self._order if j in self.jobs]

    def delete(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel()
        with self._lock:
            self.jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        return True


manager = JobManager()
