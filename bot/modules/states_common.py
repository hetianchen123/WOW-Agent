"""States shared across modules."""
from __future__ import annotations

from ..core.statemachine import State


class SafeStop(State):
    """Clean terminal state: release inputs, snapshot, notify."""

    OUTCOME = "stopped:safe_stop"

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        ctx.input.release_all()
        ctx.log_event("safe_stop", {})
        ctx.snapshot("safe_stop")
        ctx.notify("WowAgent 已安全停止", "模块状态机进入 SafeStop", level="warn")


class HandoffBagFull(State):
    """Bag below free-slot threshold -> switch to selling."""

    OUTCOME = "handoff:bagfull"

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        ctx.input.release_all()
        ctx.log_event("handoff_bagfull", {})
