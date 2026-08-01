"""WD Tagger auto-tagging (optional ML feature, multi-GPU capable).

Heavy dependencies (onnxruntime, numpy, huggingface_hub) are NOT part
of the base install — see requirements-ml.txt. Everything here degrades
gracefully: the API reports "unavailable" with an install hint instead
of failing at import time.

GPU distribution: one ONNX session per enabled GPU (settings.gpu
.enabled_devices), each driving `jobs_per_gpu` worker threads that pull
from a shared queue. Without GPUs (or with onnxruntime CPU build) a
single CPU session is used.
"""
from __future__ import annotations

import csv
import logging
import queue
import threading
from pathlib import Path

from PIL import Image

from . import audit, config, db

logger = logging.getLogger("gensight.tagger")

MODEL_REPO = "SmilingWolf/wd-swinv2-tagger-v3"
GENERAL_THRESHOLD = 0.35
CHARACTER_THRESHOLD = 0.85
INSTALL_HINT = "pip install -r requirements-ml.txt"

# WD tagger rating head (category 9) -> civitai-style content rating.
# "explicit" is mapped to X; the tagger cannot separate X from XXX.
RATING_MAP = {
    "general": "PG",
    "sensitive": "PG-13",
    "questionable": "R",
    "explicit": "X",
}


def extract_predictions(
    probs, names: list[str], categories: list[int]
) -> tuple[list[str], str | None]:
    """Turn raw model probabilities into (tags, content_rating).

    Pure function so the mapping is unit-testable without ML deps.
    Categories: 0 = general tags, 4 = character tags, 9 = rating head.
    """
    tags: list[str] = []
    best_rating, best_prob = None, -1.0
    for name, cat, p in zip(names, categories, probs):
        if cat == 0 and p >= GENERAL_THRESHOLD:
            tags.append(name)
        elif cat == 4 and p >= CHARACTER_THRESHOLD:
            tags.append(f"character:{name}")
        elif cat == 9 and p > best_prob:
            best_rating, best_prob = RATING_MAP.get(name), float(p)
    return tags, best_rating


class TaggerUnavailable(RuntimeError):
    pass


def _load_deps():
    try:
        import numpy
        import onnxruntime
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        # A missing package and a broken CUDA runtime both surface as
        # ImportError, but they need different fixes — onnxruntime
        # raises from its own extension module when libcudart/libcudnn
        # cannot be loaded. Report the real cause instead of telling
        # the user to install something they already have.
        message = str(e)
        if "libcud" in message or "onnxruntime" in (e.name or ""):
            raise TaggerUnavailable(
                f"onnxruntime could not load its CUDA runtime ({message}). "
                "Reinstall the ML extras (pip install -r requirements-ml.txt) "
                "so the pip CUDA libraries are present, and start the server "
                "via ./run.sh so they are on LD_LIBRARY_PATH. For a CPU-only "
                "host, swap onnxruntime-gpu for onnxruntime."
            ) from e
        raise TaggerUnavailable(
            f"ML dependency missing: {e.name}. Install with: {INSTALL_HINT}"
        ) from e
    return numpy, onnxruntime, hf_hub_download


def deps_available() -> tuple[bool, str | None]:
    try:
        _load_deps()
        return True, None
    except TaggerUnavailable as e:
        return False, str(e)


def _load_labels(csv_path: str) -> tuple[list[str], list[int]]:
    names, categories = [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            names.append(row["name"].replace("_", " "))
            categories.append(int(row["category"]))
    return names, categories


def _preprocess(np, path: str, size: int):
    with Image.open(path) as img:
        img = img.convert("RGBA")
        canvas = Image.new("RGBA", img.size, (255, 255, 255, 255))
        canvas.alpha_composite(img)
        img = canvas.convert("RGB")
        w, h = img.size
        side = max(w, h)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        square.paste(img, ((side - w) // 2, (side - h) // 2))
        square = square.resize((size, size), Image.BICUBIC)
    arr = np.asarray(square, dtype=np.float32)[:, :, ::-1]  # RGB -> BGR
    return np.expand_dims(arr, 0)


class TagJob:
    def __init__(self, total: int):
        self.total = total
        self.processed = 0
        self.errors = 0
        self.status = "running"  # running | done | error | cancelled
        self.error: str | None = None
        self.lock = threading.Lock()

    def summary(self) -> dict:
        return {
            "status": self.status, "total": self.total,
            "processed": self.processed, "errors": self.errors,
            "error": self.error,
        }


class TaggerManager:
    def __init__(self) -> None:
        self.job: TagJob | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def status(self) -> dict:
        ok, reason = deps_available()
        gpu_ready = False
        if ok:
            try:
                import onnxruntime as ort

                from . import gpu as gpu_mod

                # The CUDA provider being compiled in says nothing about
                # a device being reachable — a container without
                # nvidia-container-toolkit lists the provider but has no
                # GPU. Require both.
                gpu_ready = (
                    "CUDAExecutionProvider" in ort.get_available_providers()
                    and bool(gpu_mod.list_gpus())
                )
            except Exception:  # noqa: BLE001
                gpu_ready = False
        return {
            "available": ok,
            "reason": reason,
            "gpu": gpu_ready,
            "model": MODEL_REPO,
            "untagged": len(db.untagged_paths()),
            "job": self.job.summary() if self.job else None,
        }

    def cancel(self) -> None:
        self._cancel.set()

    def run(self, limit: int | None = None) -> dict:
        with self._lock:
            if self.job and self.job.status == "running":
                raise RuntimeError("tagging already running")
            _load_deps()  # raise early with the install hint
            paths = db.untagged_paths(limit)
            if not paths:
                raise RuntimeError("no untagged images")
            self.job = TagJob(len(paths))
            self._cancel.clear()
            threading.Thread(
                target=self._run, args=(self.job, paths), daemon=True
            ).start()
            return self.job.summary()

    def autorun(self) -> None:
        """Tag whatever is untagged, if the setting is on. Never raises.

        Triggered after a scan job or a watch sweep rather than per file:
        loading an ONNX session costs seconds, so one batch over the backlog is
        far cheaper than one session per image. Every refusal `run()` can raise
        is benign here — already running, nothing to do, or the optional ML
        dependencies are absent — so they are logged, not propagated into the
        ingest path.
        """
        if not config.load_settings().get("tagger", {}).get("auto"):
            return
        try:
            job = self.run()
            logger.info("auto tagging started: %d image(s)", job.get("total", 0))
        except Exception as e:  # noqa: BLE001 - ingest must not fail on this
            logger.info("auto tagging skipped: %s", e)

    # -- worker side -------------------------------------------------

    def _run(self, job: TagJob, paths: list[str]) -> None:
        import time

        started = time.time()
        try:
            np, ort, hf_hub_download = _load_deps()
            logger.info("tagging %d image(s); fetching model %s",
                        len(paths), MODEL_REPO)
            model_path = hf_hub_download(MODEL_REPO, "model.onnx")
            labels_path = hf_hub_download(MODEL_REPO, "selected_tags.csv")
            names, categories = _load_labels(labels_path)

            settings = config.load_settings()
            devices = settings["gpu"]["enabled_devices"]
            jobs_per_gpu = max(1, int(settings["gpu"]["jobs_per_gpu"]))
            available = ort.get_available_providers()

            sessions = []
            if devices and "CUDAExecutionProvider" in available:
                for d in devices:
                    sessions.append(
                        ort.InferenceSession(
                            model_path,
                            providers=[
                                ("CUDAExecutionProvider", {"device_id": int(d)}),
                                "CPUExecutionProvider",
                            ],
                        )
                    )
                logger.info(
                    "tagger using %d GPU session(s) on device(s) %s, "
                    "%d worker(s) each (%d total)",
                    len(sessions), devices, jobs_per_gpu,
                    len(sessions) * jobs_per_gpu,
                )
            else:
                sessions.append(
                    ort.InferenceSession(
                        model_path, providers=["CPUExecutionProvider"]
                    )
                )
                jobs_per_gpu = 1
                reason = ("no GPU enabled in settings" if not devices
                          else "CUDAExecutionProvider unavailable")
                logger.info("tagger running on CPU (%s), 1 worker", reason)

            work_q: queue.Queue[str] = queue.Queue()
            for p in paths:
                work_q.put(p)

            def worker(session) -> None:
                input_meta = session.get_inputs()[0]
                size = input_meta.shape[1] if isinstance(input_meta.shape[1], int) else 448
                while not self._cancel.is_set():
                    try:
                        path = work_q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        batch = _preprocess(np, path, size)
                        probs = session.run(None, {input_meta.name: batch})[0][0]
                        tags, rating = extract_predictions(probs, names, categories)
                        db.set_tags(path, tags, rating)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("tagging failed for %s: %s", path, e)
                        with job.lock:
                            job.errors += 1
                    finally:
                        with job.lock:
                            job.processed += 1
                            done, total = job.processed, job.total
                        # Progress every 5% (min 25 images) so a long run
                        # leaves a trail without flooding the log.
                        step = max(25, total // 20)
                        if done % step == 0 or done == total:
                            elapsed = time.time() - started
                            rate = done / elapsed if elapsed else 0
                            eta = (total - done) / rate if rate else 0
                            logger.info(
                                "tagging %d/%d (%.0f%%) %.1f img/s, eta %.0fs, "
                                "errors=%d",
                                done, total, done / total * 100, rate, eta,
                                job.errors,
                            )

            threads = []
            for session in sessions:
                for _ in range(jobs_per_gpu):
                    t = threading.Thread(target=worker, args=(session,), daemon=True)
                    t.start()
                    threads.append(t)
            for t in threads:
                t.join()
            job.status = "cancelled" if self._cancel.is_set() else "done"
            elapsed = time.time() - started
            logger.info(
                "tagging %s: %d/%d in %.1fs (%.1f img/s), errors=%d",
                job.status, job.processed, job.total, elapsed,
                job.processed / elapsed if elapsed else 0, job.errors,
            )
            audit.record("tagger.finish", detail={
                "status": job.status, "processed": job.processed,
                "total": job.total, "errors": job.errors,
                "seconds": round(elapsed, 1),
            }, ok=job.errors == 0)
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            logger.exception("tag job failed")
            audit.record("tagger.finish", detail={"status": "error",
                                                  "error": job.error}, ok=False)


tagger_manager = TaggerManager()
