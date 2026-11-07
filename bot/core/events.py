"""Tiny synchronous pub/sub event bus used to wire estop/watchdog into modules."""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable, Dict, List

log = logging.getLogger(__name__)

Handler = Callable[[dict], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, kind: str, handler: Handler) -> None:
        with self._lock:
            self._handlers[kind].append(handler)

    def publish(self, kind: str, payload: dict | None = None) -> None:
        payload = payload or {}
        with self._lock:
            handlers = list(self._handlers.get(kind, []))
        for h in handlers:
            try:
                h(payload)
            except Exception:  # noqa: BLE001 - a bad handler must not kill the bus
                log.exception("event handler failed for %s", kind)
