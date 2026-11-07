"""Logging setup: console + rotating file under data/logs."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", logfile: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=_FORMAT, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
