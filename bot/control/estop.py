"""Global emergency-stop hotkey + flag. Fires release_all on the input and
wakes every module loop through the triggered flag / event bus."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

from ..core.events import EventBus
from .input_base import InputBase

log = logging.getLogger(__name__)


class EStop:
    def __init__(self, hotkey: str, bus: Optional[EventBus] = None) -> None:
        self.hotkey = hotkey
        self.bus = bus
        self.triggered = threading.Event()
        self._triggered_at = 0.0
        self._input: Optional[InputBase] = None
        self._after_hooks: List[Callable[[], None]] = []
        self._listener = None

    def bind(self, input_backend: InputBase) -> None:
        self._input = input_backend

    def on_trigger(self, hook: Callable[[], None]) -> None:
        self._after_hooks.append(hook)

    def fire(self, reason: str = "manual") -> None:
        if self.triggered.is_set():
            return
        self._triggered_at = time.time()
        self.triggered.set()
        log.warning("ESTOP (%s)", reason)
        if self._input is not None:
            try:
                self._input.release_all()
            except Exception:  # noqa: BLE001
                log.exception("release_all failed during estop")
        for hook in self._after_hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("estop hook failed")
        if self.bus is not None:
            self.bus.publish("estop", {"reason": reason, "t": self._triggered_at})

    def check(self) -> bool:
        if not self.triggered.is_set() and self._input is not None and self._input.name == "helper":
            if self._input.interrupted():
                self.fire("键鼠执行器已停止")
        return self.triggered.is_set()

    def reset(self) -> None:
        self.triggered.clear()

    # ------------------------------------------------------------- listener
    def start_hotkey(self) -> bool:
        if self._input is not None and self._input.name == "helper":
            return True  # The native helper owns stop, focus, and disconnect handling.
        try:
            from pynput import keyboard

            self._listener = keyboard.GlobalHotKeys({self.hotkey: lambda: self.fire("hotkey")})
            self._listener.daemon = True
            self._listener.start()
            log.info("estop hotkey armed: %s", self.hotkey)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("estop hotkey unavailable: %s", exc)
            return False

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._listener = None
