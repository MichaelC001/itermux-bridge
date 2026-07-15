"""Logging to ~/.itermux/logs/ (design §9, §11.5).

Note on secrets: keystrokes and send-keys arguments can contain passwords, so
callers log lengths, not contents. Raising level to debug will surface more.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LEVELS = {
    "debug": logging.DEBUG, "info": logging.INFO,
    "warning": logging.WARNING, "error": logging.ERROR,
}


def setup_logging(log_path: Path, level: str = "info") -> None:
    log_path = Path(log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(LEVELS.get(level.lower(), logging.INFO))
    root.addHandler(handler)
