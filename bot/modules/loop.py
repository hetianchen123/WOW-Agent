"""Unattended loop: work/rest rhythm, module selection, bag-full handoff,
gold sampling and notifications. The top-level `run --module loop` entry."""
from __future__ import annotations

import datetime
import logging
import random
import time

from .base import Module, ModuleOutcome
from .fisher import FisherModule
from .gatherer import GathererModule
from .seller import SellerModule

log = logging.getLogger(__name__)

_MODULES = {"fisher": FisherModule, "gatherer": GathererModule, "seller": SellerModule}


def _in_maintenance(windows: list[list[str]], now: datetime.datetime) -> bool:
    day = now.strftime("%a")  # Mon..Sun
    hhmm = now.strftime("%H:%M")
    for w in windows:
        if len(w) != 2:
            continue
        d1, t1 = w[0].split()
        d2, t2 = w[1].split()
        if day == d1[:3] and t1 <= hhmm < t2:
            return True
    return False


class LoopRunner:
    def __init__(self, ctx, primary: str = "fisher") -> None:  # noqa: ANN001
        self.ctx = ctx
        self.primary = primary
        self.cycles = 0

    def _pick_module(self) -> str:
        # v1 policy: run the configured primary; night hours prefer fishing
        hour = datetime.datetime.now().hour
        if self.primary == "gatherer" and (hour < 7 or hour >= 23):
            return "fisher"
        return self.primary

    def _work_duration(self) -> float:
        r = self.ctx.cfg.rhythm
        return random.uniform(r.work_min_s, r.work_max_s)

    def _rest_duration(self) -> float:
        r = self.ctx.cfg.rhythm
        return random.uniform(r.rest_min_s, r.rest_max_s)

    def run(self, total_duration_s: float) -> ModuleOutcome:
        ctx = self.ctx
        t0 = time.time()
        session = ctx.db.start_session("loop", {"primary": self.primary})
        ctx.log_event("loop_start", {"primary": self.primary})
        last_status = "completed"
        while not ctx.estop.check() and time.time() - t0 < total_duration_s:
            if _in_maintenance(ctx.cfg.rhythm.maintenance_windows, datetime.datetime.now()):
                ctx.log.info("maintenance window — resting 10 min")
                time.sleep(600)
                continue
            name = self._pick_module()
            ctx.stats = {}
            module = _MODULES[name](ctx)
            outcome = module.run(min(self._work_duration(), max(60.0, total_duration_s - (time.time() - t0))))
            last_status = outcome.status
            self.cycles += 1
            ctx.db.log_event("cycle_end", {"module": name, "status": outcome.status, "cycle": self.cycles})
            if outcome.status.startswith("stopped"):
                ctx.notify("循环作业异常停止", f"{name}: {outcome.status}", level="error")
                break
            if outcome.status == "handoff:bagfull":
                seller = SellerModule(ctx)
                sell_out = seller.run(1800.0)
                ctx.db.log_event("sell_done", {"status": sell_out.status})
                continue  # back to work immediately after selling
            rest = self._rest_duration()
            ctx.log.info("resting %.0f s", rest)
            deadline = time.time() + rest
            while time.time() < deadline and not ctx.estop.check():
                time.sleep(2.0)
        ctx.db.end_session(session, last_status, {"cycles": self.cycles})
        ctx.log_event("loop_end", {"cycles": self.cycles, "status": last_status})
        return ModuleOutcome(status=last_status, stats={"cycles": self.cycles, "duration_s": round(time.time() - t0, 1)})
