"""Windows input backend via pydirectinput (DirectInput scan codes —
games often ignore plain VK events)."""
from __future__ import annotations

import logging

from .input_base import InputBase

log = logging.getLogger(__name__)

_KEYMAP = {
    "space": "space", "esc": "esc", "tab": "tab", "enter": "enter",
    "shift": "shift", "ctrl": "ctrl", "alt": "alt",
    "up": "up", "down": "down", "left": "left", "right": "right",
}


class WinInput(InputBase):
    name = "win"

    def __init__(self, key_jitter: bool = True) -> None:
        super().__init__(key_jitter)
        import pydirectinput

        pydirectinput.PAUSE = 0.02
        self._pdi = pydirectinput

    def _norm(self, key: str) -> str:
        k = key.strip().lower()
        return _KEYMAP.get(k, k)

    def _key_down_raw(self, key: str) -> None:
        self._pdi.keyDown(self._norm(key))

    def _key_up_raw(self, key: str) -> None:
        self._pdi.keyUp(self._norm(key))

    def _move_raw(self, x: int, y: int) -> None:
        self._pdi.moveTo(x, y)

    def position(self) -> tuple[int, int]:
        import pyautogui

        return pyautogui.position()

    def _mouse_down_raw(self, button: str) -> None:
        self._pdi.mouseDown(button=button)

    def _mouse_up_raw(self, button: str) -> None:
        self._pdi.mouseUp(button=button)

    def scroll(self, dy: int) -> None:
        self._pdi.scroll(dy)
