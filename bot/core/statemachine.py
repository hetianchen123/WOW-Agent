"""Minimal hierarchical-flat state machine used by all automation modules.

States are objects with a ``tick(ctx) -> next_state_name | None`` method.
The Machine handles per-state timeouts and logs every transition.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)


class State:
    name: str = "state"
    timeout_s: float = 0.0  # 0 = no timeout
    timeout_next: str = ""  # state to enter on timeout ("" -> machine default)

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        pass

    def on_exit(self, ctx) -> None:  # noqa: ANN001
        pass

    def tick(self, ctx) -> Optional[str]:  # noqa: ANN001
        return None


class Machine:
    def __init__(self, ctx, default_timeout_next: str = "SafeStop") -> None:  # noqa: ANN001
        self.ctx = ctx
        self.states: Dict[str, State] = {}
        self.current: Optional[State] = None
        self._in_state_s = 0.0
        self.default_timeout_next = default_timeout_next
        self.transition_count = 0

    def add(self, state: State) -> State:
        if not state.name or state.name == "state":
            state.name = type(state).__name__
        self.states[state.name] = state
        return state

    @property
    def state_name(self) -> str:
        return self.current.name if self.current else ""

    @property
    def in_state_s(self) -> float:
        return self._in_state_s

    def start(self, name: str) -> None:
        self.transition_to(name)

    def transition_to(self, name: str) -> None:
        if name not in self.states:
            raise KeyError(f"unknown state: {name}")
        if self.current is not None:
            self.current.on_exit(self.ctx)
            log.debug("state exit %s after %.2fs", self.current.name, self._in_state_s)
        self.current = self.states[name]
        self._in_state_s = 0.0
        self.transition_count += 1
        log.info("state -> %s", name)
        self.current.on_enter(self.ctx)

    def tick(self, dt: float) -> None:
        if self.current is None:
            return
        self._in_state_s += dt
        nxt = self.current.tick(self.ctx)
        if nxt:
            self.transition_to(nxt)
            return
        if self.current.timeout_s and self._in_state_s >= self.current.timeout_s:
            target = self.current.timeout_next or self.default_timeout_next
            log.warning("state %s timed out after %.1fs -> %s", self.current.name, self._in_state_s, target)
            if target in self.states:
                self.transition_to(target)
            else:
                self.transition_to(self.default_timeout_next)
