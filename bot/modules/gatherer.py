"""Gathering module: patrol a route, spot nodes on the locked minimap,
align, approach, click, verify via cast events / bag deltas.

Ground and flying routes share the logic; flying adds descend-near-node.
"""
from __future__ import annotations

import math
import time
from typing import Optional, Tuple

from ..control.navigation import Route, RouteFollower, minimap_bearing, minimap_distance_yards
from ..core.statemachine import State
from ..perception import cv as cvmod
from ..perception.channel_spec import FastEvKind, StatusFlags
from .base import Module
from .states_common import SafeStop


class _GState(State):
    def __init__(self) -> None:
        self.t_enter = 0.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.t_enter = time.time()

    def guard(self, ctx, allow_combat: bool = False) -> Optional[str]:  # noqa: ANN001
        if ctx.estop.check():
            return "SafeStop"
        st = ctx.status()
        if st is not None:
            flags, _hp = st
            if flags & StatusFlags.DEAD or flags & StatusFlags.GHOST:
                return "SafeStop"
            if not allow_combat and (flags & StatusFlags.IN_COMBAT):
                return "Evade"
        return None


class Patrol(_GState):
    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.last_scan = 0.0

    def _scan(self, ctx) -> Optional[Tuple[str, float, float]]:  # noqa: ANN001
        """Look for node template hits in the minimap ROI.

        Returns (template_name, bearing_rad, yards) of the best hit.
        """
        cfg = ctx.cfg.gathering
        roi = cfg.minimap_roi
        if not any(roi):
            return None
        try:
            img = ctx.frame(roi)
        except Exception:  # noqa: BLE001
            return None
        best = None
        for name in cfg.node_templates:
            tpl = cvmod.load_template(name, ctx.cfg.template_dir)
            if tpl is None:
                continue
            m = cvmod.match_best(img, tpl, threshold=cfg.template_threshold, template_name=name)
            if m and (best is None or m.score > best.score):
                cx, cy = roi[2] / 2.0, roi[3] / 2.0
                bearing = minimap_bearing(m.x + m.w / 2 - cx, m.y + m.h / 2 - cy, ctx.cfg.navigation.facing_convention)
                yards = minimap_distance_yards(m.x + m.w / 2 - cx, m.y + m.h / 2 - cy, cfg.yards_per_px)
                best = (name, bearing, yards)
        return best

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        follower: RouteFollower = ctx.gather_follower
        pos, hdg = ctx.pos(), ctx.heading()
        if pos is None or hdg is None:
            return None
        d = follower.update(time.time(), pos, hdg, True)
        if d.finished:
            ctx.stats["finish_reason"] = d.finish_reason
            return "SafeStop"
        if d.recovery == "back":
            ctx.input.press_for("s", 0.8)
            return None
        if d.recovery == "jump":
            ctx.input.tap("space")
            return None
        if d.stuck_event:
            ctx.stats["stuck_events"] = ctx.stats.get("stuck_events", 0) + 1
            ctx.log_event("stuck", {"wp": d.wp_index})
            return None
        # steering
        icfg = ctx.cfg.input
        if d.turn_err is not None and abs(d.turn_err) > math.radians(icfg.turn_deadband_deg):
            px = int(max(-icfg.mouse_turn_max_px, min(icfg.mouse_turn_max_px, d.turn_err * icfg.mouse_turn_gain)))
            if icfg.invert_turn:
                px = -px
            ctx.input.rmb_turn(px)
        if d.move_forward:
            ctx.input.down("w")
        else:
            ctx.input.up("w")
        # node scan
        now = time.time()
        if now - self.last_scan >= ctx.cfg.gathering.scan_interval_s:
            self.last_scan = now
            hit = self._scan(ctx)
            if hit:
                name, bearing, yards = hit
                ctx.node_target = (name, bearing, yards, time.time())
                ctx.log_event("node_spotted", {"template": name, "yards": round(yards, 1)})
                return "Align"
        return None


class Align(_GState):
    """Turn toward the spotted node until within deadband."""

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        name, bearing, yards, seen_t = ctx.node_target
        if time.time() - seen_t > 8.0:
            return "Patrol"
        hdg = ctx.heading()
        if hdg is None:
            return None
        err = (bearing - hdg + math.pi) % (2 * math.pi) - math.pi
        icfg = ctx.cfg.input
        if abs(err) > math.radians(icfg.turn_deadband_deg):
            px = int(max(-icfg.mouse_turn_max_px, min(icfg.mouse_turn_max_px, err * icfg.mouse_turn_gain)))
            if icfg.invert_turn:
                px = -px
            ctx.input.rmb_turn(px)
            return None
        if yards <= ctx.cfg.gathering.interact_range_yards:
            return "InteractNode"
        return "Approach"


class Approach(_GState):
    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.t0 = time.time()

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        name, bearing, yards, seen_t = ctx.node_target
        # re-scan periodically — node may despawn (world object, not tracked)
        if time.time() - seen_t > 3.0:
            return "Patrol"
        hdg = ctx.heading()
        if hdg is None:
            return None
        err = (bearing - hdg + math.pi) % (2 * math.pi) - math.pi
        icfg = ctx.cfg.input
        if abs(err) > math.radians(icfg.turn_deadband_deg):
            px = int(max(-icfg.mouse_turn_max_px, min(icfg.mouse_turn_max_px, err * icfg.mouse_turn_gain)))
            if icfg.invert_turn:
                px = -px
            ctx.input.rmb_turn(px)
        if yards > ctx.cfg.gathering.interact_range_yards * 1.2:
            ctx.input.down("w")
            if yards < 30 and ctx.cfg.gathering.descend_before_interact and getattr(ctx, "route_mode", "fly") == "fly":
                ctx.input.down(ctx.cfg.gathering.key_descend)
            else:
                ctx.input.up(ctx.cfg.gathering.key_descend)
            return None
        ctx.input.up("w")
        ctx.input.up(ctx.cfg.gathering.key_descend)
        return "InteractNode"


class InteractNode(_GState):
    def on_enter(self, ctx) -> None:  # noqa: ANN001
        # node should sit near screen center + configured offset
        self.t0 = time.time()
        self.clicked = False
        self.bag_before = ctx.bagsum()

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx, allow_combat=False)):
            return g
        if not self.clicked:
            w, h = 1920, 1080
            try:
                w, h = ctx.capture.size
            except Exception:  # noqa: BLE001
                pass
            ox, oy = ctx.cfg.gathering.node_click_offset
            ctx.input.right_click(w // 2 + ox, h // 2 + oy)
            self.clicked = True
            self.t0 = time.time()
            return None
        # verification happens in Harvest once cast starts
        return "Harvest"


class Harvest(_GState):
    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.t0 = time.time()
        self.cast_seen = False
        before = ctx.bagsum()
        self.total_before = before[1] if before else None

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx, allow_combat=True)):
            return g
        for ev in ctx.new_fastev():
            if ev.kind == FastEvKind.CAST_START and ev.payload in (2, 3):
                self.cast_seen = True
            elif ev.kind == FastEvKind.CAST_DONE and ev.payload in (2, 3):
                ctx.stats["nodes_harvested"] = ctx.stats.get("nodes_harvested", 0) + 1
                ctx.input.up("w")
                return "Patrol"
        bag = ctx.bagsum()
        if self.total_before is not None and bag is not None and bag[1] > self.total_before:
            ctx.stats["nodes_harvested"] = ctx.stats.get("nodes_harvested", 0) + 1
            ctx.input.up("w")
            return "Patrol"
        if time.time() - self.t0 > (6.0 if self.cast_seen else 2.5):
            # node gone or contested — try one more click, then back to patrol
            ctx.stats["node_misses"] = ctx.stats.get("node_misses", 0) + 1
            return "InteractNode" if not self.cast_seen else "Patrol"
        return None


class Evade(_GState):
    """Break combat: fly up / back off, wait out; give up after a minute."""

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        ctx.input.up("w")
        ctx.input.press_for("space", 1.2)
        self.t0 = time.time()

    def tick(self, ctx) -> Optional[str]:
        if ctx.estop.check():
            return "SafeStop"
        st = ctx.status()
        if st is not None and not (st[0] & StatusFlags.IN_COMBAT):
            ctx.stats["evades_ok"] = ctx.stats.get("evades_ok", 0) + 1
            return "Patrol"
        if time.time() - self.t0 > 60.0:
            ctx.log_event("evade_failed", {})
            return "SafeStop"
        return None


class GathererModule(Module):
    name = "gatherer"

    def build_states(self) -> None:
        for s in (Patrol(), Align(), Approach(), InteractNode(), Harvest(), Evade(), SafeStop()):
            self.machine.add(s)
        self.initial_state = "Patrol"

    def run(self, max_duration_s: float):
        # route wiring happens in the runner; fail fast if missing
        if getattr(self.ctx, "gather_follower", None) is None:
            from ..core.config import load_route
            from ..control.navigation import Route as _R

            route_name = getattr(self.ctx, "route_name", None) or "gather_route"
            route = _R.from_dict(load_route(route_name))
            self.ctx.gather_follower = RouteFollower(route, self.ctx.cfg.navigation, loop=True)
            self.ctx.route_mode = route.mode
        self.ctx.node_target = ("", 0.0, 999.0, 0.0)
        return super().run(max_duration_s)
