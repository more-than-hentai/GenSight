"""Heuristic image quality assessment (dependency-free).

Scores 0-100 with a list of detected issues:

Image-based (PIL only):
- blurry          low edge-map variance (out-of-focus / oversmoothed)
- low_resolution  pixel count under ~0.13 MP
- too_dark / too_bright   extreme mean luminance
- low_contrast    tiny luminance variance (washed-out output)

Generation-settings based (the "bad settings" class of failures):
- low_steps       < 10 steps on a non-turbo/lcm/lightning model
- cfg_too_high    CFG > 15 (fried colors / artifacts)
- cfg_too_low     CFG < 2 on a non-turbo model (mushy output)

Anatomy damage (broken hands/limbs) requires an ML detector; this
module exposes the same job/queue shape so an ONNX-based detector can
be plugged in later (see docs/architecture.md roadmap).
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageFilter, ImageStat

from . import config, db

logger = logging.getLogger("gensight.quality")

_TURBO_MARKERS = ("turbo", "lcm", "lightning", "hyper", "schnell", "flux")

PENALTIES = {
    "blurry": 30,
    "low_resolution": 20,
    "too_dark": 15,
    "too_bright": 15,
    "low_contrast": 15,
    "low_steps": 10,
    "cfg_too_high": 10,
    "cfg_too_low": 10,
}


def _to_float(value, default=0.0) -> float:
    try:
        return float(str(value).split(",")[0])
    except (TypeError, ValueError):
        return default


def analyze(path: str, params: dict | None = None) -> tuple[float, list[str]]:
    """Return (score 0-100, issues). Raises on unreadable files."""
    issues: list[str] = []
    with Image.open(path) as img:
        w, h = img.size
        if w * h < 360_000:  # ~600x600
            issues.append("low_resolution")
        gray = img.convert("L")
        gray.thumbnail((512, 512))
        stat = ImageStat.Stat(gray)
        mean, var = stat.mean[0], stat.var[0]
        if mean < 35:
            issues.append("too_dark")
        elif mean > 220:
            issues.append("too_bright")
        if var < 200:
            issues.append("low_contrast")
        edge_var = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
        # Solid/flat images have ~0 edge variance; sharp photos are >>300
        if edge_var < 60:
            issues.append("blurry")

    params = params or {}
    model = str(params.get("Model", "")).lower()
    sampler = str(params.get("Sampler", "")).lower()
    turbo = any(m in model or m in sampler for m in _TURBO_MARKERS)
    steps = _to_float(params.get("Steps"))
    cfg = _to_float(params.get("CFG scale"))
    if steps and steps < 10 and not turbo:
        issues.append("low_steps")
    if cfg and cfg > 15:
        issues.append("cfg_too_high")
    if cfg and 0 < cfg < 2 and not turbo:
        issues.append("cfg_too_low")

    score = max(0.0, 100.0 - sum(PENALTIES.get(i, 5) for i in issues))
    return score, issues


class QualityJob:
    def __init__(self, total: int):
        self.total = total
        self.processed = 0
        self.errors = 0
        self.status = "running"
        self.error: str | None = None
        self.lock = threading.Lock()

    def summary(self) -> dict:
        return {"status": self.status, "total": self.total,
                "processed": self.processed, "errors": self.errors,
                "error": self.error}


class QualityManager:
    def __init__(self) -> None:
        self.job: QualityJob | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def status(self) -> dict:
        return {
            "pending": len(db.quality_pending_paths()),
            "job": self.job.summary() if self.job else None,
        }

    def cancel(self) -> None:
        self._cancel.set()

    def run(self, limit: int | None = None) -> dict:
        with self._lock:
            if self.job and self.job.status == "running":
                raise RuntimeError("quality analysis already running")
            paths = db.quality_pending_paths(limit)
            if not paths:
                raise RuntimeError("no images pending quality analysis")
            self.job = QualityJob(len(paths))
            self._cancel.clear()
            threading.Thread(
                target=self._run, args=(self.job, paths), daemon=True
            ).start()
            return self.job.summary()

    def _run(self, job: QualityJob, paths: list[str]) -> None:
        workers = max(1, int(config.load_settings()["workers"]["extract"]))

        def work(path: str) -> None:
            if self._cancel.is_set():
                return
            try:
                item = db.get_image(path)
                score, issues = analyze(path, (item or {}).get("params"))
                db.set_quality(path, score, issues)
            except Exception as e:  # noqa: BLE001
                logger.warning("quality analysis failed for %s: %s", path, e)
                with job.lock:
                    job.errors += 1
            finally:
                with job.lock:
                    job.processed += 1

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(work, paths))
            job.status = "cancelled" if self._cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"


quality_manager = QualityManager()
