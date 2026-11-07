"""Synthetic end-to-end selftest: a fake game world renders the WowAgent
pixel channel (via the Python reference encoder), reacts to injected input
and drives a real FisherModule through cast -> bite -> hook -> loot cycles.

No game client or screen permissions needed. Run:
    uv run python -m bot.cli selftest
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from .control.estop import EStop
from .control.input_mock import MockInput
from .core.config import BotConfig, ChannelConfig
from .core.events import EventBus
from .data.db import Database
from .modules.context import ModuleContext
from .modules.fisher import FisherModule
from .ops.notify import Notifier
from .perception import channel_spec as spec
from .perception.audio import AudioMonitor
from .perception.capture import SyntheticCapture

log = logging.getLogger("selftest")

CANVAS_W, CANVAS_H = 640, 360
CHANNEL_X, CHANNEL_Y = 40, 320
BOBER_ROI = (300, 200, 80, 60)


class MockAudio(AudioMonitor):
    def __init__(self, game: "SyntheticGame", audio_cfg) -> None:  # noqa: ANN001
        super().__init__(dataclasses.replace(audio_cfg, enabled=False))
        self.game = game
        self.available = True  # pretend; is_bite consults the world

    def is_bite(self) -> bool:
        return self.game.audio_bite


class SyntheticGame:
    """Scripted world: renders the channel + bobber splash, reacts to taps."""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.tick = 0
        self.pos = (0.50, 0.50)
        self.heading = 0.30
        self.free_slots = 30
        self.tracked_total = 0
        self.gold = 12_345
        self.zone = 2022
        self.hp = 100.0
        self.flags = 0
        self.pending: List[Tuple[float, dict]] = []  # (t, event dict)
        self.ev_queue: List[Tuple[int, int, int]] = []  # (kind, seq, payload)
        self._seq = 0
        self.audio_bite = False
        self._cast_count = 0
        self._loot_count = 0
        self._bite_windows: List[Tuple[float, float]] = []  # splash (start,end)
        self._interact_deadline: Optional[Tuple[float, float]] = None
        self._seen_actions = 0

    # ----------------------------------------------------------- reactions
    def _consume_input(self, inp: MockInput, t: float) -> None:
        acts = inp.actions
        for _, kind, args in acts[self._seen_actions :]:
            if kind == "key_down" and args and args[0] == "1":
                self._cast_count += 1
                cast_t = t
                self.pending.append((cast_t + 0.25, {"ev": (spec.FastEvKind.CAST_START, 1)}))
                bite_t = cast_t + 1.2
                self._bite_windows.append((bite_t, bite_t + 0.45))
                self._interact_deadline = (bite_t, bite_t + 1.6)
                self.pending.append((bite_t + 1.7, {"ev": (spec.FastEvKind.CAST_DONE, 1)}))
            elif kind == "key_down" and args and args[0] == "f":
                if self._interact_deadline and self._interact_deadline[0] <= t <= self._interact_deadline[1]:
                    self.pending.append((t + 0.15, {"loot": 2}))
                    self._interact_deadline = None
        self._seen_actions = len(acts)

    def _advance(self, t: float) -> None:
        due = [p for p in self.pending if p[0] <= t]
        self.pending = [p for p in self.pending if p[0] > t]
        for _, act in due:
            if "ev" in act:
                kind, payload = act["ev"]
                self._seq = (self._seq + 1) % 32
                self.ev_queue.append((int(kind), self._seq, payload))
            if "loot" in act:
                self.tracked_total += act["loot"]
                self._loot_count += 1
        self.audio_bite = any(s <= t <= e for s, e in self._bite_windows)

    # ------------------------------------------------------------- messages
    def _message(self, tick: int) -> Optional[Tuple[int, int]]:
        kind = spec.schedule_kind(tick)
        if kind == spec.MSG_POS:
            return spec.MSG_POS, spec.pack_pos(*self.pos)
        if kind == spec.MSG_HDG:
            return spec.MSG_HDG, spec.pack_hdg(self.heading)
        if kind == spec.MSG_STATUS:
            return spec.MSG_STATUS, spec.pack_status(self.flags, self.hp)
        if kind == spec.MSG_BAGSUM:
            return spec.MSG_BAGSUM, spec.pack_bagsum(self.free_slots, self.tracked_total)
        if kind == spec.MSG_FASTEV:
            if self.ev_queue:
                k, s, p = self.ev_queue.pop(0)
                return spec.MSG_FASTEV, spec.pack_fastev(k, s, p)
            return spec.MSG_FASTEV, spec.pack_fastev(spec.FastEvKind.NONE, self._seq, 0)
        if kind is None:  # slow rotation
            idx = spec.slowrot_index(tick)
            if idx == 0:
                return spec.MSG_ZONE, self.zone
            if idx == 1:
                return spec.MSG_GOLD, self.gold
            return spec.MSG_BAG, 1 * 65536 + self.tracked_total
        return None

    # -------------------------------------------------------------- render
    def render(self, idx: int, t: float, rect: Tuple[int, int, int, int]) -> np.ndarray:
        if rect == (CHANNEL_X, CHANNEL_Y, spec.REGION_W, spec.REGION_H):
            self.tick += 1
            self._consume_input(self.input_ref, t)  # type: ignore[attr-defined]
            self._advance(t)
        canvas = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
        canvas[:] = (60, 60, 70)
        # water area + bobber splash
        x, y, w, h = BOBER_ROI
        splash = any(s <= t <= e for s, e in self._bite_windows)
        canvas[y : y + h, x : x + w] = (255, 255, 255) if splash else (40, 90, 160)
        msg = self._message(self.tick)
        if msg:
            mtype, value = msg
            spec.render_frame(canvas, CHANNEL_X, CHANNEL_Y, spec.encode_frame(mtype, value))
        else:
            spec.render_frame(canvas, CHANNEL_X, CHANNEL_Y, [0] * 8)
        return canvas


def build_selftest_context(db_path="/tmp/wowagnet_selftest.db", duration_scale: float = 1.0) -> Tuple[ModuleContext, MockInput, SyntheticGame, Database]:
    cfg = BotConfig()
    cfg.channel = ChannelConfig(x=CHANNEL_X, y=CHANNEL_Y, calibrated=True)
    cfg.capture.control_fps = 30
    f = cfg.fishing
    f.bite_window_min_s = 0.6 * duration_scale
    f.bite_window_max_s = 6.0 * duration_scale
    f.loot_confirm_timeout_s = 3.0
    f.cast_confirm_timeout_s = 2.5
    f.bobber_roi = BOBER_ROI
    f.use_audio_bite = True
    f.use_vision_bite = True

    game = SyntheticGame()
    capture = SyntheticCapture(game.render)
    from .perception.channel import ChannelReader

    channel = ChannelReader(cfg.channel)
    inp = MockInput()
    audio = MockAudio(game, cfg.audio)
    db = Database(db_path)
    bus = EventBus()
    estop = EStop("<ctrl>+<alt>+<f12>", bus)
    notifier = Notifier(cfg.notify)
    ctx = ModuleContext(cfg, capture, channel, inp, audio, db, bus, estop, notifier)
    game.input_ref = inp  # type: ignore[attr-defined]
    return ctx, inp, game, db


def run_fisher_session(max_duration_s: float = 9.0) -> dict:
    ctx, inp, game, db = build_selftest_context()
    module = FisherModule(ctx)
    outcome = module.run(max_duration_s)
    stats = dict(outcome.stats)
    stats["outcome"] = outcome.status
    stats["world_casts"] = game._cast_count
    stats["world_loots"] = game._loot_count
    stats["channel_ok"] = ctx.channel.frames_ok
    stats["channel_bad"] = ctx.channel.frames_bad
    db.close()
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    stats = run_fisher_session()
    ok = (
        stats.get("outcome") in ("timeout", "completed")
        and stats.get("casts", 0) >= 2
        and stats.get("loots", 0) >= 1
        and stats.get("channel_bad", 1) <= stats.get("channel_ok", 0) * 0.05 + 3
        and not stats.get("outcome", "").startswith("stopped")
    )
    print("\n===== selftest result =====")
    for k, v in sorted(stats.items()):
        print(f"  {k:16s} {v}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
