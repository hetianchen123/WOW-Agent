"""Push notifications via Bark (iOS) or ServerChan. Never raises."""
from __future__ import annotations

import logging
from typing import Optional

import requests

from ..core.config import NotifyConfig

log = logging.getLogger(__name__)

_LEVELS = {"info": 0, "warn": 1, "error": 2}


class Notifier:
    def __init__(self, cfg: NotifyConfig) -> None:
        self.cfg = cfg

    def send(self, title: str, body: str, level: str = "warn") -> bool:
        if self.cfg.provider in ("", "none"):
            return False
        if _LEVELS.get(level, 1) < _LEVELS.get("warn", 1) and self.cfg.provider == "none":
            return False
        try:
            if self.cfg.provider == "bark":
                r = requests.post(
                    f"https://api.day.app/{self.cfg.key}",
                    json={"title": title, "body": body, "group": "wowagnet"},
                    timeout=self.cfg.timeout_s,
                )
                return r.status_code == 200
            if self.cfg.provider == "serverchan":
                r = requests.post(
                    f"https://sctapi.ftqq.com/{self.cfg.key}.send",
                    data={"title": title, "desp": body},
                    timeout=self.cfg.timeout_s,
                )
                return r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            log.warning("notify failed: %s", exc)
        return False
