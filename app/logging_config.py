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
import threading
from pathlib import Path

from . import config

LOG_FILE = "gensight-app.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"

_setup_lock = threading.Lock()


class _OneLineFilter(logging.Filter):
    """Collapse CR/LF in the rendered message.

    Log lines carry filesystem paths and prompts, and a filename may
    legally contain a newline on Linux — enough to forge extra log
    entries in the persistent file.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - bad format args; let logging report it
            return True
        if "\r" in rendered or "\n" in rendered:
            record.msg = rendered.replace("\r", "\\r").replace("\n", "\\n")
            record.args = ()
        return True


def _has_file_handler(logger: logging.Logger, path: Path) -> bool:
    target = str(path)
    return any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        and getattr(h, "baseFilename", None) == target
        for h in logger.handlers
    )


def setup() -> None:
    """Idempotent: safe to call from startup, reloads and tests."""
    level_name = os.environ.get("GENSIGHT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    with _setup_lock:
        root = logging.getLogger("gensight")
        root.setLevel(level)
        root.propagate = True

        formatter = logging.Formatter(FORMAT)
        # The filter goes on the handlers, not the logger: a logger's
        # filters only see records logged directly to it, so records
        # propagated up from gensight.scanner et al. would bypass it.
        one_line = _OneLineFilter()
        if not any(type(h) is logging.StreamHandler for h in root.handlers):
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            console.addFilter(one_line)
            root.addHandler(console)

        # Identify the handler by its resolved path rather than a module
        # flag: after a reload the flag resets while handlers survive.
        try:
            data_dir = Path(config.DATA_DIR)
            data_dir.mkdir(parents=True, exist_ok=True)
            log_path = (data_dir / LOG_FILE).resolve()
            if not _has_file_handler(root, log_path):
                file_handler = logging.handlers.RotatingFileHandler(
                    log_path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                    encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                file_handler.addFilter(one_line)
                root.addHandler(file_handler)
                root.info("logging initialised (level=%s, file=%s)",
                          level_name, log_path)
        except OSError as e:  # read-only data dir — console still works
            root.warning("file logging disabled: %s", e)
