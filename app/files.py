"""File operations: serving-path validation, recycle bin, organize.

All destructive operations are soft by default — "delete" moves the
file into data/trash/ with a DB snapshot so it can be restored; only
an explicit purge removes bytes from disk.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import config, db

logger = logging.getLogger("gensight.files")

TRASH_DIR_NAME = "trash"


def trash_dir() -> Path:
    return Path(config.DATA_DIR) / TRASH_DIR_NAME


def allowed_roots(extra_job_dirs: list[str] | None = None) -> list[Path]:
    roots = [Path(d).resolve() for d in config.load_settings()["directories"]]
    roots += [Path(w["directory"]).resolve() for w in db.list_watches()]
    for d in extra_job_dirs or []:
        roots.append(Path(d).resolve())
    roots.append(Path(config.UPLOAD_DIR).resolve())
    # Trashed files must stay viewable in the recycle-bin UI
    roots.append(trash_dir().resolve())
    return roots


def _unique_dest(dest: Path) -> Path:
    """Never overwrite: append _1, _2 ... before the suffix."""
    if not dest.exists():
        return dest
    for i in range(1, 10_000):
        candidate = dest.with_stem(f"{dest.stem}_{i}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"cannot find unique name for {dest}")


# ---------------------------------------------------------------- trash


def trash_image(path: str) -> dict:
    p = Path(path)
    item = db.get_image(str(p))
    if item is None:
        raise FileNotFoundError(f"not in library: {p}")
    if not p.exists():
        # File vanished outside the app — drop the stale row instead of
        # creating a phantom trash entry.
        db.delete_image_row(str(p))
        raise FileNotFoundError(
            f"file missing on disk (stale library entry removed): {p}"
        )
    trash_dir().mkdir(parents=True, exist_ok=True)
    dest = trash_dir() / f"{uuid.uuid4().hex[:8]}_{p.name}"
    shutil.move(str(p), dest)
    db.delete_image_row(str(p))
    return db.trash_add(str(p), str(dest), item)


def restore_image(trash_id: int) -> dict:
    entry = db.trash_get(trash_id)
    if entry is None:
        raise FileNotFoundError(f"trash entry not found: {trash_id}")
    src = Path(entry["trash_path"])
    dest = Path(entry["original_path"])
    if not src.exists():
        db.trash_remove(trash_id)
        raise FileNotFoundError("trashed file is missing on disk")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest)
    shutil.move(str(src), dest)
    db.restore_image_row(entry["item"], str(dest))
    db.trash_remove(trash_id)
    return {"restored_to": str(dest)}


def purge_trash(trash_id: int | None = None) -> int:
    """Permanently delete one entry, or all when trash_id is None."""
    entries = [db.trash_get(trash_id)] if trash_id else [
        db.trash_get(e["id"]) for e in db.trash_list()
    ]
    purged = 0
    for entry in entries:
        if not entry:
            continue
        try:
            Path(entry["trash_path"]).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("purge failed for %s: %s", entry["trash_path"], e)
            continue
        db.trash_remove(entry["id"])
        purged += 1
    return purged


# ---------------------------------------------------------------- organize

_TEMPLATE_VARS = ("model", "tool", "date", "group", "sampler")
_SEGMENT_SANITIZE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _segment(value: str) -> str:
    cleaned = _SEGMENT_SANITIZE.sub("_", str(value)).strip(". ")
    return cleaned or "unknown"


def _render_template(template: str, item: dict) -> str:
    params = item.get("params") or {}
    mtime = item.get("mtime") or time.time()
    values = {
        "model": _segment(params.get("Model", "unknown")),
        "tool": _segment(item.get("tool", "unknown")),
        "date": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"),
        "group": _segment(item.get("group_name") or "ungrouped"),
        "sampler": _segment(params.get("Sampler", "unknown")),
    }
    out = template
    for var in _TEMPLATE_VARS:
        out = out.replace("{" + var + "}", values[var])
    return out


def organize(
    target_root: str,
    template: str = "{model}/{date}",
    dry_run: bool = True,
    **filters,
) -> dict:
    """Move library files matching `filters` into
    target_root/<rendered template>/<filename>. Returns the move plan;
    with dry_run=False the moves are performed and the DB updated."""
    if not template.strip():
        raise ValueError("template is empty")
    unknown = re.findall(r"\{(\w+)\}", template)
    bad = [v for v in unknown if v not in _TEMPLATE_VARS]
    if bad:
        raise ValueError(
            f"unknown template variable(s): {bad}; allowed: {_TEMPLATE_VARS}"
        )
    root = Path(target_root).expanduser().resolve()
    _total, items = db.query_images(**filters, limit=1_000_000)

    plan, errors = [], []
    for item in items:
        src = Path(item["file"])
        rel = _render_template(template, item)
        dest = root / rel / src.name
        if dest == src:
            continue
        plan.append({"from": str(src), "to": str(dest)})

    if dry_run:
        return {"dry_run": True, "count": len(plan), "moves": plan[:500],
                "errors": errors}

    moved = 0
    for move in plan:
        src, dest = Path(move["from"]), Path(move["to"])
        try:
            if not src.exists():
                raise FileNotFoundError("source missing")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest = _unique_dest(dest)
            shutil.move(str(src), dest)
            db.update_path(str(src), str(dest))
            moved += 1
        except OSError as e:
            errors.append({"file": str(src), "error": str(e)})
    return {"dry_run": False, "count": moved, "errors": errors}
