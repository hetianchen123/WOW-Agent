"""macOS input backend via pynput (Quartz CGEvent).

Requires the terminal/Python to be granted 辅助功能 (Accessibility) in
System Settings -> Privacy & Security. Verify with PoC #1.
"""
from __future__ import annotations

import logging

from .input_base import InputBase

log = logging.getLogger(__name__)

_SPECIAL = {
    "space": "space",
    "esc": "esc",
    "tab": "tab",
    "enter": "return",
    "shift": "shift",
    "ctrl": "ctrl",
    "alt": "alt",
    "cmd": "cmd",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "page_up",
    "pagedown": "page_down",
}


class MacInput(InputBase):
    name = "mac"

    def __init__(self, key_jitter: bool = True) -> None:
        super().__init__(key_jitter)
        from pynput import keyboard, mouse  # noqa: F401

        self._kb = keyboard.Controller()
        self._mouse = mouse.Controller()
        self._Key = keyboard.Key
        self._Button = mouse.Button

    def _norm(self, key: str):
        k = key.strip().lower()
        if k in _SPECIAL:
            return getattr(self._Key, _SPECIAL[k])
        if k.startswith("f") and k[1:].isdigit():
            return getattr(self._Key, f"f{k[1:]}", None) or self._kb.KeyCode.from_char(key)
        return self._kb.KeyCode.from_char(key)

    def _key_down_raw(self, key: str) -> None:
        self._kb.press(self._norm(key))

    def _key_up_raw(self, key: str) -> None:
        self._kb.release(self._norm(key))

    def _move_raw(self, x: int, y: int) -> None:
        self._mouse.position = (x, y)

    def position(self) -> tuple[int, int]:
        return self._mouse.position

    def _button(self, button: str):
        return self._Button.left if button == "left" else self._Button.right

    def _mouse_down_raw(self, button: str) -> None:
        self._mouse.press(self._button(button))

    def _mouse_up_raw(self, button: str) -> None:
        self._mouse.release(self._button(button))

    def scroll(self, dy: int) -> None:
        self._mouse.scroll(0, dy)
