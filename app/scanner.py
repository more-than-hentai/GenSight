"""Scan job management.

Each scan job enumerates image files under a directory and extracts
metadata with a per-job worker pool. Multiple jobs can run
concurrently (bounded by settings.max_concurrent_jobs); worker counts
are adjustable per job from the UI so very large directories can be
tuned independently.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config, metadata

MAX_WORKERS = 32


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
        self.results: list[dict[str, Any]] = []
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
        root = Path(self.directory)
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")
        pattern = "**/*" if self.recursive else "*"
        files = []
        for p in root.glob(pattern):
            if self._cancel.is_set():
                break
            if p.is_file() and p.suffix.lower() in metadata.SUPPORTED_EXTENSIONS:
                files.append(p)
        files.sort()
        return files

    def _extract_all(self, files: list[Path]) -> None:
        def work(path: Path) -> None:
            if self._cancel.is_set():
                return
            item = metadata.extract(path)
            with self._lock:
                self.results.append(item)
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

    def page(self, offset: int, limit: int, query: str = "", tool: str = "") -> dict:
        with self._lock:
            items = list(self.results)
        if query:
            q = query.lower()
            items = [
                r
                for r in items
                if q in r["prompt"].lower()
                or q in r["negative_prompt"].lower()
                or q in r["filename"].lower()
                or q in str(r["params"].get("Model", "")).lower()
            ]
        if tool:
            items = [r for r in items if r["tool"] == tool]
        total = len(items)
        page_items = [
            {k: v for k, v in r.items() if k != "raw"}
            for r in items[offset : offset + limit]
        ]
        return {"total": total, "offset": offset, "items": page_items}

    def get_result(self, file: str) -> dict | None:
        with self._lock:
            for r in self.results:
                if r["file"] == file:
                    return r
        return None


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
