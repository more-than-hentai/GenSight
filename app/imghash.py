"""Perceptual hashing (dHash) without heavy dependencies.

A 64-bit difference hash is enough for exact/near-duplicate detection
of AI-generated images (same seed re-renders, format conversions,
minor re-encodes). Hamming distance 0 = practically identical,
<= 10 = visually similar.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image


def dhash(path: str | Path, hash_size: int = 8) -> str | None:
    """64-bit dHash as a 16-char hex string, or None if undecodable."""
    try:
        with Image.open(path) as img:
            img = img.convert("L").resize(
                (hash_size + 1, hash_size), Image.LANCZOS
            )
            px = list(img.getdata())
    except Exception:  # noqa: BLE001 - corrupt/unsupported image
        return None
    bits = 0
    w = hash_size + 1
    for row in range(hash_size):
        for col in range(hash_size):
            bits = (bits << 1) | (px[row * w + col] > px[row * w + col + 1])
    return f"{bits:0{hash_size * hash_size // 4}x}"


def hamming(a: str, b: str) -> int:
    """Hamming distance between two hex hash strings."""
    return (int(a, 16) ^ int(b, 16)).bit_count()
