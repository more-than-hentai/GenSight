"""GPU detection via nvidia-smi (no heavy dependencies).

Used by the settings UI to list devices, and by the (future) ML
analysis scheduler to distribute jobs across enabled GPUs.
"""
from __future__ import annotations

import shutil
import subprocess

QUERY_FIELDS = "index,name,memory.total,memory.used,utilization.gpu"


def list_gpus() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={QUERY_FIELDS}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []

    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_total_mb": int(parts[2]),
                    "memory_used_mb": int(parts[3]),
                    "utilization": int(parts[4]),
                }
            )
        except ValueError:
            continue
    return gpus
