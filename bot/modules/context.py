"""ModuleContext: everything a module needs, injected for testability."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

from ..control.estop import EStop
from ..control.input_base import InputBase
from ..core.config import BotConfig
from ..core.events import EventBus
from ..core.paths import SCREENSHOT_DIR, DATA_DIR
from ..data.evidence import RunEvidence, build_manifest
from ..data.db import Database
from ..ops.notify import Notifier
from ..perception.audio import AudioMonitor
from ..perception.capture import ScreenCapture
from ..perception.channel import ChannelReader
from ..perception.channel_spec import MSG_BAGSUM, MSG_FASTEV, MSG_HDG, MSG_POS, MSG_STATUS
from ..perception.cv import save_snapshot
from ..perception import cv as cvmod

Rect = Tuple[int, int, int, int]


class ModuleContext:
    def __init__(
        self,
        cfg: BotConfig,
        capture: ScreenCapture,
        channel: ChannelReader,
        input_backend: InputBase,
        audio: AudioMonitor,
        db: Database,
        events: EventBus,
        estop: EStop,
        notifier: Notifier,
    ) -> None:
        self.cfg = cfg
        self.capture = capture
        self.channel = channel
        self.input = input_backend
        self.audio = audio
        self.db = db
        self.events = events
        self.estop = estop
        self.notifier = notifier
        self.log = logging.getLogger("module")
        self._frame_cache: Dict[Tuple[int, int, int, int], Tuple[float, np.ndarray]] = {}
        self.stats: Dict[str, float] = {}
        self.gold_last_logged = 0.0
        self._fastev_cursor = 0
        self.evidence = None
        self.evidence_state = None
        self.evidence_failed = False

    def start_evidence(self, module: str) -> None:
        if self.evidence is not None or not module.startswith("quest"):
            return
        edition = "_forever_" if self.cfg.game_version.startswith("1.60.") else "_retail_"
        installed = Path("/Volumes/Mac SSD1/Applications/World of Warcraft") / edition / "Interface/AddOns/WowAgent"
        try:
            manifest = build_manifest(installed=installed)
            manifest.update(module=module, input_backend=self.input.name,
                            configuration={name: asdict(getattr(self.cfg, name))
                                           for name in ("navigation", "capture", "channel", "route_planner")})
            self.evidence = RunEvidence(DATA_DIR / "runs", manifest)
            self.log_event("run_evidence_started", {"directory": str(self.evidence.path)})
        except (OSError, ValueError, TypeError) as exc:
            self.evidence_failed = True
            self.log.warning("run evidence unavailable: %s", exc)

    # ------------------------------------------------------------- per tick
    def begin_tick(self) -> None:
        # drop frames older than 0.2 s so each ROI is grabbed at most ~5 Hz
        now = time.time()
        for k in list(self._frame_cache):
            if now - self._frame_cache[k][0] > 0.2:
                del self._frame_cache[k]

    def invalidate_game_observation(self) -> None:
        self._frame_cache.clear()
        self.channel.invalidate()
        self._fastev_cursor = len(self.channel.events)

    def frame(self, rect: Rect) -> np.ndarray:
        key = tuple(rect)
        got = self._frame_cache.get(key)
        if got is None:
            img = self.capture.grab(*rect)
            self._frame_cache[key] = (time.time(), img)
            return img
        return got[1]

    def pump_channel(self) -> bool:
        """Grab the channel region and decode. Returns channel-alive flag.

        The 142x16 channel strip is tiny — always a direct grab so the
        decoder runs at full control-loop rate (frame cache would throttle
        the synthetic/real message cadence).
        """
        try:
            img = self.capture.grab(self.cfg.channel.x, self.cfg.channel.y, 142, 16)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("channel grab failed: %s", exc)
            return False
        self.channel.update(img)
        return self.channel.is_alive()

    # ------------------------------------------------------------ shortcuts
    def pos(self):
        return self.channel.pos()

    def heading(self):
        return self.channel.heading()

    def heading_age(self):
        return self.channel.heading_age()

    def status(self):
        return self.channel.status()

    def bagsum(self):
        return self.channel.bagsum()

    def new_fastev(self):
        evs = self.channel.pop_events(self._fastev_cursor)
        if evs:
            self._fastev_cursor = len(self.channel.events)
        return evs

    # ---------------------------------------------------------------- utils
    def log_event(self, kind: str, data: Optional[dict] = None) -> None:
        if self.evidence is not None and not self.evidence_failed:
            try:
                data = self.evidence.record(kind, data, self.evidence_state)
            except (OSError, ValueError, TypeError) as exc:
                self.evidence_failed = True
                self.log.warning("run evidence write failed: %s", exc)
        self.log.info("%s %s", kind, data or "")
        self.db.log_event(kind, data)

    def notify(self, title: str, body: str, level: str = "warn") -> None:
        self.db.log_event("notify", {"title": title, "body": body, "level": level})
        self.notifier.send(title, body, level=level)

    def snapshot(self, tag: str) -> Optional[str]:
        try:
            img = self.capture.grab(0, 0, 1920, 1080)
        except Exception:  # noqa: BLE001
            return None
        p = save_snapshot(img, tag, directory=SCREENSHOT_DIR)
        return str(p) if p else None

    def sample_gold(self) -> None:
        g = self.channel.gold(max_age_s=90.0)
        if g is not None and time.time() - self.gold_last_logged > 60:
            self.gold_last_logged = time.time()
            self.db.add_gold(g)
