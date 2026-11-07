"""Background watchdog: game process liveness; optional foreground check
stub. Stops the session through the estop when the game disappears."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, process_names: List[str], on_lost: Callable[[str], None], interval_s: float = 10.0) -> None:
        self.process_names = process_names
        self.on_lost = on_lost
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.enabled = True

    def game_running(self) -> bool:
        try:
            import psutil

            for p in psutil.process_iter(["name"]):
                name = (p.info.get("name") or "").lower()
                if any(n.lower() in name for n in self.process_names):
                    return True
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("psutil unavailable: %s", exc)
            return True  # fail-open: don't kill sessions on tooling errors

    def _loop(self) -> None:
        misses = 0
        while not self._stop.is_set():
            if self.enabled and not self.game_running():
                misses += 1
                if misses >= 2:  # two consecutive misses -> real
                    log.error("game process lost")
                    try:
                        self.on_lost("game_process_lost")
                    except Exception:  # noqa: BLE001
                        log.exception("on_lost callback failed")
                    return
            else:
                misses = 0
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
