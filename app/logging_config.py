"""Application logging setup.

Console output stays as-is (run.sh already captures it), and everything
also goes to a rotating file under the data directory so a long tagging
or scan run can be reviewed after the fact.

Level is controlled by GENSIGHT_LOG_LEVEL (default INFO).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from . import config

LOG_FILE = "gensight-app.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"

_configured = False


def setup() -> None:
    """Idempotent: safe to call from startup and from tests."""
    global _configured
    if _configured:
        return

    level_name = os.environ.get("GENSIGHT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("gensight")
    root.setLevel(level)
    root.propagate = True

    formatter = logging.Formatter(FORMAT)
    if not any(isinstance(h, logging.StreamHandler) and
               not isinstance(h, logging.handlers.RotatingFileHandler)
               for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    try:
        data_dir = Path(config.DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            data_dir / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:  # read-only data dir — console logging still works
        root.warning("file logging disabled: %s", e)

    _configured = True
    root.info("logging initialised (level=%s, file=%s)", level_name, LOG_FILE)
