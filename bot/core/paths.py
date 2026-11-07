"""Project-wide constants and helpers shared across layers."""
from __future__ import annotations

import pathlib
import time

# Project root = parent of the bot package
ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
ROUTES_DIR = CONFIG_DIR / "routes"
DATA_DIR = ROOT / "data"
TEMPLATE_DIR = ROOT / "templates"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
LOG_DIR = DATA_DIR / "logs"
REPORT_DIR = DATA_DIR / "reports"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, ROUTES_DIR, DATA_DIR, SCREENSHOT_DIR, LOG_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now() -> float:
    return time.time()
