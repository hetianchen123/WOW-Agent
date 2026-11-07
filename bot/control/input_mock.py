"""Mock input backend: records every action — used by dry-run, tests and
the synthetic selftest world."""
from __future__ import annotations

import time
from typing import List, Tuple

from .input_base import InputBase


class MockInput(InputBase):
    name = "mock"

    def __init__(self, key_jitter: bool = False) -> None:
        super().__init__(key_jitter)
        self.actions: List[Tuple[float, str, tuple]] = []
        self._pos: Tuple[int, int] = (960, 540)

    def _record(self, kind: str, *args) -> None:
        self.actions.append((time.time(), kind, args))

    def _key_down_raw(self, key: str) -> None:
        self._record("key_down", key)

    def _key_up_raw(self, key: str) -> None:
        self._record("key_up", key)

    def _move_raw(self, x: int, y: int) -> None:
        self._pos = (x, y)
        self._record("move", x, y)

    def position(self) -> Tuple[int, int]:
        return self._pos

    def _mouse_down_raw(self, button: str) -> None:
        self._record("mouse_down", button)

    def _mouse_up_raw(self, button: str) -> None:
        self._record("mouse_up", button)

    def scroll(self, dy: int) -> None:
        self._record("scroll", dy)

    # ---------------------------------------------------------------- helpers
    def count(self, kind: str, *args) -> int:
        return sum(1 for _, k, a in self.actions if k == kind and (not args or a[: len(args)] == args))

    def last(self, kind: str):
        for t, k, a in reversed(self.actions):
            if k == kind:
                return t, a
        return None
