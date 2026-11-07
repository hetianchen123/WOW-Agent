"""Module base: owns a Machine and the run loop."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..core.statemachine import Machine
from .context import ModuleContext

log = logging.getLogger(__name__)


@dataclass
class ModuleOutcome:
    status: str = "completed"  # completed | handoff:bagfull | stopped:<reason> | timeout
    stats: Dict[str, float] = field(default_factory=dict)


class Module:
    name = "module"

    def __init__(self, ctx: ModuleContext) -> None:
        self.ctx = ctx
        self.machine = Machine(ctx, default_timeout_next="SafeStop")
        self.build_states()

    def build_states(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, max_duration_s: float) -> ModuleOutcome:
        from .states_common import SafeStop  # local import avoids cycle

        if "SafeStop" not in self.machine.states:
            self.machine.add(SafeStop())
        self.ctx.log_event("module_start", {"module": self.name})
        t0 = time.time()
        last = t0
        try:
            self.machine.start(self.initial_state)
            while True:
                if self.ctx.estop.check():
                    return self._finish("stopped:estop", t0)
                now = time.time()
                dt = now - last
                last = now
                self.ctx.begin_tick()
                self.ctx.pump_channel()
                self.machine.tick(dt)
                self.ctx.sample_gold()
                if now - t0 >= max_duration_s:
                    return self._finish("timeout", t0)
                done = getattr(self.machine.current, "OUTCOME", None) if self.machine.current else None
                if done:
                    return self._finish(done, t0)
                fps = self.ctx.cfg.capture.control_fps
                time.sleep(max(0.001, 1.0 / fps - (time.time() - now)))
        except KeyboardInterrupt:
            return self._finish("stopped:interrupt", t0)
        except Exception as exc:  # noqa: BLE001
            log.exception("module crashed")
            self.ctx.log_event("module_crash", {"module": self.name, "error": repr(exc)})
            self.ctx.snapshot("crash_" + self.name)
            return self._finish(f"stopped:crash:{exc}", t0)

    def _finish(self, status: str, t0: float) -> ModuleOutcome:
        self.ctx.stats["duration_s"] = round(time.time() - t0, 1)
        self.ctx.log_event("module_end", {"module": self.name, "status": status, "stats": self.ctx.stats})
        try:
            if self.ctx.input is not None:
                self.ctx.input.release_all()
        except Exception:  # noqa: BLE001
            pass
        return ModuleOutcome(status=status, stats=dict(self.ctx.stats))

    initial_state = ""
