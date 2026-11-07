"""Fishing module — the first production loop.

States:
  Ready -> Cast -> CastWait -> WaitBite -> Hook -> LootCheck -> Ready
  Any state may punt to SafeStop (combat/dead/channel loss) or
  HandoffBagFull (free slots below threshold).
"""
from __future__ import annotations

import time
from typing import Optional

from ..core.statemachine import State
from ..perception import cv as cvmod
from ..perception.channel_spec import FastEvKind, StatusFlags
from .base import Module
from .states_common import HandoffBagFull, SafeStop


class _FState(State):
    def __init__(self) -> None:
        self.t_enter = 0.0
        self.retry = 0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.t_enter = time.time()
        self.retry = 0
        self.on_enter_extra(ctx)

    def on_enter_extra(self, ctx) -> None:  # noqa: ANN001
        pass

    def guard(self, ctx) -> Optional[str]:  # noqa: ANN001
        """Global interrupt checks; returns a state name to punt to."""
        if ctx.estop.check():
            return "SafeStop"
        st = ctx.status()
        if st is not None:
            flags, _hp = st
            if flags & StatusFlags.IN_COMBAT or flags & StatusFlags.DEAD or flags & StatusFlags.GHOST:
                return "SafeStop"
        if not ctx.channel.is_alive():
            if time.time() - self.t_enter > 5.0:
                return "SafeStop"
        bag = ctx.bagsum()
        if bag is not None and bag[0] <= ctx.cfg.fishing.min_free_slots:
            return "HandoffBagFull"
        return None

    def _fastev_since(self, ctx, t0: float):  # noqa: ANN001
        return [e for e in ctx.new_fastev() if e.t >= t0]


class Ready(_FState):
    def tick(self, ctx) -> Optional[str]:
        if self.guard(ctx):
            return self.guard(ctx)
        # require a live channel before casting
        if not ctx.channel.is_alive():
            return None
        if time.time() - self.t_enter < 0.6:
            return None
        return "Cast"


class Cast(_FState):
    def on_enter_extra(self, ctx) -> None:
        ctx.stats["casts"] = ctx.stats.get("casts", 0) + 1
        ctx.input.tap(ctx.cfg.fishing.key_cast)
        self.cast_t = time.time()

    def tick(self, ctx) -> Optional[str]:
        if self.guard(ctx):
            return self.guard(ctx)
        return "CastWait"


class CastWait(_FState):
    def on_enter_extra(self, ctx) -> None:
        self.t0 = time.time()
        self.got = False

    def tick(self, ctx) -> Optional[str]:
        if self.guard(ctx):
            return self.guard(ctx)
        for ev in self._fastev_since(ctx, self.t0):
            if ev.kind == FastEvKind.CAST_START and ev.payload in (1,):
                self.got = True
        cfgf = ctx.cfg.fishing
        if time.time() - self.t0 > cfgf.cast_confirm_timeout_s:
            self.retry += 1
            if self.retry >= cfgf.max_cast_retries:
                ctx.log_event("cast_failed", {})
                return "SafeStop"
            return "Cast"
        if self.got:
            return "WaitBite"
        return None


class WaitBite(_FState):
    def on_enter_extra(self, ctx) -> None:
        self.t0 = time.time()
        cfg = ctx.cfg.fishing
        # bite windows vary; arm after min, give up at max
        self.ref_frame = None
        if cfg.use_vision_bite and any(cfg.bobber_roi):
            r = cfg.bobber_roi
            try:
                self.ref_frame = ctx.frame(r)
            except Exception:  # noqa: BLE001
                self.ref_frame = None

    def _vision_bite(self, ctx) -> bool:  # noqa: ANN001
        cfg = ctx.cfg.fishing
        if self.ref_frame is None or not any(cfg.bobber_roi):
            return False
        try:
            cur = ctx.frame(cfg.bobber_roi)
        except Exception:  # noqa: BLE001
            return False
        return cvmod.absdiff_score(self.ref_frame, cur) >= cfg.bobber_diff_threshold

    def tick(self, ctx) -> Optional[str]:
        if self.guard(ctx):
            return self.guard(ctx)
        cfg = ctx.cfg.fishing
        elapsed = time.time() - self.t0
        # recast silently when the bobber despawns without a bite
        if elapsed > cfg.bite_window_max_s:
            ctx.stats["no_bite_recasts"] = ctx.stats.get("no_bite_recasts", 0) + 1
            return "Cast"
        if elapsed < cfg.bite_window_min_s:
            return None
        bite = False
        if cfg.use_audio_bite and ctx.audio.available:
            bite = ctx.audio.is_bite()
        if not bite and cfg.use_vision_bite:
            bite = self._vision_bite(ctx)
        if bite:
            ctx.stats["bites"] = ctx.stats.get("bites", 0) + 1
            return "Hook"
        return None


class Hook(_FState):
    def on_enter_extra(self, ctx) -> None:
        ctx.input.tap(ctx.cfg.fishing.key_interact)
        self.t0 = time.time()

    def tick(self, ctx) -> Optional[str]:
        if self.guard(ctx):
            return self.guard(ctx)
        if time.time() - self.t0 > ctx.cfg.fishing.bite_react_s:
            ctx.stats["late_hooks"] = ctx.stats.get("late_hooks", 0) + 1
            return "Cast"
        return "LootCheck"


class LootCheck(_FState):
    def on_enter_extra(self, ctx) -> None:
        self.t0 = time.time()
        start = ctx.bagsum()
        self.start_total = start[1] if start else None
        self.start_free = start[0] if start else None
        self.looted = False

    def tick(self, ctx) -> Optional[str]:
        if self.guard(ctx):
            return self.guard(ctx)
        bag = ctx.bagsum()
        if bag is not None:
            if self.start_total is not None and bag[1] > self.start_total:
                self.looted = True
            elif self.start_free is not None and bag[0] < self.start_free:
                self.looted = True
        st = ctx.status()
        if st is not None and (st[0] & StatusFlags.LOOT_OPEN):
            self.looted = True
        if self.looted:
            ctx.stats["loots"] = ctx.stats.get("loots", 0) + 1
            return "Ready"
        if time.time() - self.t0 > ctx.cfg.fishing.loot_confirm_timeout_s:
            ctx.stats["loot_missed"] = ctx.stats.get("loot_missed", 0) + 1
            return "Ready"
        return None


class FisherModule(Module):
    name = "fisher"

    def build_states(self) -> None:
        for s in (Ready(), Cast(), CastWait(), WaitBite(), Hook(), LootCheck(), SafeStop(), HandoffBagFull()):
            self.machine.add(s)
        self.initial_state = "Ready"
