"""Prompt / parameter usage statistics over the image library."""
from __future__ import annotations

import json
import re
import threading
from collections import Counter
from typing import Iterator

from . import db

# In-memory materialized result, keyed by the library's change counter.
# Recomputing token statistics over the whole table on every request
# gets expensive at tens of thousands of images; the version check
# makes repeat views O(1) while staying exact (any DB mutation bumps
# the version and invalidates the cache).
_cache_lock = threading.Lock()
_cache: dict = {"version": None, "top": None, "result": None}

_STRIP_CHARS = re.compile(r"[()\[\]{}<>]")
_TRAILING_WEIGHT = re.compile(r":\s*[0-9.]+\s*$")
_SPLIT = re.compile(r"[,\n]")
_IGNORED = {"break", ""}


def tokenize(text: str) -> Iterator[str]:
    """Split an SD-style prompt into normalized tokens.

    "(masterpiece:1.2), best quality" -> "masterpiece", "best quality"
    """
    for part in _SPLIT.split(text or ""):
        token = _STRIP_CHARS.sub("", part)
        token = _TRAILING_WEIGHT.sub("", token)
        token = re.sub(r"\s+", " ", token).strip().lower()
        if token not in _IGNORED and len(token) <= 80:
            yield token


def collect(top: int = 50) -> dict:
    version = db.data_version()
    with _cache_lock:
        if _cache["version"] == version and _cache["top"] == top:
            return _cache["result"]

    result = _collect(top)

    with _cache_lock:
        _cache.update(version=version, top=top, result=result)
    return result


def _collect(top: int) -> dict:
    conn = db.connect()
    rows = conn.execute(
        "SELECT prompt, negative_prompt, params FROM images WHERE error IS NULL"
    ).fetchall()

    positive: Counter[str] = Counter()
    negative: Counter[str] = Counter()
    models: Counter[str] = Counter()
    samplers: Counter[str] = Counter()

    for r in rows:
        positive.update(tokenize(r["prompt"]))
        negative.update(tokenize(r["negative_prompt"]))
        try:
            params = json.loads(r["params"] or "{}")
        except json.JSONDecodeError:
            params = {}
        if params.get("Model"):
            models[str(params["Model"])] += 1
        if params.get("Sampler"):
            samplers[str(params["Sampler"])] += 1

    def fmt(counter: Counter, n: int) -> list[dict]:
        return [{"token": k, "count": v} for k, v in counter.most_common(n)]

    return {
        "images": len(rows),
        "positive": fmt(positive, top),
        "negative": fmt(negative, top),
        "models": fmt(models, 30),
        "samplers": fmt(samplers, 30),
    }
