"""Input injection abstraction. Backends: pynput (macOS), pydirectinput
(Windows), Mock (dry-run/tests)."""
from __future__ import annotations

import logging
import random
import time
from typing import Optional, Set

log = logging.getLogger(__name__)


def jitter(base: float, frac: float = 0.2) -> float:
    return max(0.0, base * (1.0 + random.uniform(-frac, frac)))


class InputBase:
    name = "base"
    supports_hold: bool = True

    def __init__(self, key_jitter: bool = True) -> None:
        self.key_jitter = key_jitter
        self._held_keys: Set[str] = set()
        self._held_buttons: Set[str] = set()

    # ------------------------------------------------------------- keyboard
    def _key_down_raw(self, key: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _key_up_raw(self, key: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def down(self, key: str) -> None:
        if key in self._held_keys:
            return
        self._key_down_raw(key)
        self._held_keys.add(key)

    def up(self, key: str) -> None:
        self._held_keys.discard(key)
        self._key_up_raw(key)

    def tap(self, key: str, hold_s: float = 0.03) -> None:
        self.down(key)
        time.sleep(jitter(hold_s) if self.key_jitter else hold_s)
        self.up(key)

    def press_for(self, key: str, seconds: float) -> None:
        self.down(key)
        time.sleep(jitter(seconds, 0.1) if self.key_jitter else seconds)
        self.up(key)

    # ---------------------------------------------------------------- mouse
    def move_to(self, x: int, y: int, jitter_px: int = 2) -> None:
        if jitter_px:
            x += random.randint(-jitter_px, jitter_px)
            y += random.randint(-jitter_px, jitter_px)
        self._move_raw(x, y)

    def _move_raw(self, x: int, y: int) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _mouse_down_raw(self, button: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _mouse_up_raw(self, button: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def click(self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left") -> None:
        if x is not None and y is not None:
            self.move_to(x, y)
        self._mouse_down_raw(button)
        time.sleep(jitter(0.04) if self.key_jitter else 0.04)
        self._mouse_up_raw(button)

    def right_click(self, x: Optional[int] = None, y: Optional[int] = None) -> None:
        self.click(x, y, button="right")

    def click_world_object(self) -> None:
        """Interact at the current, already verified hover position."""
        self.right_click()

    def click_ui(self) -> None:
        """Click the current, already verified UI button without moving it."""
        self.click()

    def rmb_turn(self, dx_px: int) -> None:
        """Hold RMB and move mouse horizontally by dx_px (character yaw)."""
        if dx_px == 0:
            return
        cur = self.position()
        self._mouse_down_raw("right")
        self._move_raw(int(cur[0] + dx_px), int(cur[1]))
        time.sleep(jitter(0.03) if self.key_jitter else 0.03)
        self._mouse_up_raw("right")

    def position(self) -> tuple[int, int]:  # pragma: no cover - abstract
        raise NotImplementedError

    def scroll(self, dy: int) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # ---------------------------------------------------------------- safety
    def release_all(self) -> None:
        for k in list(self._held_keys):
            try:
                self._key_up_raw(k)
            except Exception:  # noqa: BLE001
                pass
        self._held_keys.clear()
        for b in list(self._held_buttons):
            try:
                self._mouse_up_raw(b)
            except Exception:  # noqa: BLE001
                pass
        self._held_buttons.clear()
