"""Selling module: hearth -> auction house (Auctionator posts) -> mailbox ->
return to the work spot.

Design rule: pricing belongs to the Auctionator addon (its own rules), the
bot only performs physical actions: walking fixed hub routes, opening
dialogs and clicking fixed buttons verified by template matching.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from ..control.navigation import Route, RouteFollower
from ..core.statemachine import State
from ..perception import cv as cvmod
from ..perception.channel_spec import FastEvKind, StatusFlags
from .base import Module
from .states_common import SafeStop


class _SState(State):
    def __init__(self) -> None:
        self.t_enter = 0.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.t_enter = time.time()

    def guard(self, ctx) -> Optional[str]:  # noqa: ANN001
        if ctx.estop.check():
            return "SafeStop"
        st = ctx.status()
        if st is not None and (st[0] & (StatusFlags.DEAD | StatusFlags.GHOST)):
            return "SafeStop"
        return None


def _walk(ctx, follower: RouteFollower, timeout_s: float) -> Optional[str]:  # noqa: ANN001
    """One steering step along a hub route. Returns 'done' | None."""
    pos, hdg = ctx.pos(), ctx.heading()
    if pos is None or hdg is None:
        return None
    d = follower.update(time.time(), pos, hdg, True)
    if d.finished:
        ctx.input.up("w")
        return "done"
    icfg = ctx.cfg.input
    if d.recovery == "back":
        ctx.input.press_for("s", 0.8)
        return None
    if d.recovery == "jump":
        ctx.input.tap("space")
        return None
    if d.turn_err is not None and abs(d.turn_err) > math.radians(icfg.turn_deadband_deg):
        px = int(max(-icfg.mouse_turn_max_px, min(icfg.mouse_turn_max_px, d.turn_err * icfg.mouse_turn_gain)))
        if icfg.invert_turn:
            px = -px
        ctx.input.rmb_turn(px)
    if d.move_forward:
        ctx.input.down("w")
    else:
        ctx.input.up("w")
    return None


def _find_template(ctx, name_key: str):  # noqa: ANN001
    cfg = ctx.cfg.selling
    tpl_name = cfg.templates.get(name_key, name_key)
    tpl = cvmod.load_template(tpl_name, ctx.cfg.template_dir)
    if tpl is None:
        return None
    try:
        w, h = ctx.capture.size
    except Exception:  # noqa: BLE001
        w, h = 1920, 1080
    img = ctx.frame((0, 0, w, h))
    return cvmod.match_best(img, tpl, threshold=0.78, template_name=tpl_name)


class Hearth(_SState):
    timeout_s = 45.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.tapped = False
        self.start_pos = ctx.pos()

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        cfg = ctx.cfg.selling
        if not self.tapped:
            ctx.input.tap(cfg.key_hearthstone)
            self.tapped = True
            return None
        pos = ctx.pos()
        if pos and self.start_pos:
            import math as _m

            hub = getattr(ctx, "hub_pos", None)
            if hub and _m.hypot(pos[0] - hub[0], pos[1] - hub[1]) < cfg.hub_radius_map_units:
                return "ToAH"
        # fallback: near enough time passed and we moved -> at hub
        if time.time() - self.t_enter > cfg.hearth_timeout_s * 0.8:
            return "ToAH"
        return None


class ToAH(_SState):
    timeout_s = 90.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        from ..core.config import load_route

        route = Route.from_dict(load_route(ctx.cfg.selling.route_to_ah))
        self.follower = RouteFollower(route, ctx.cfg.navigation, loop=False)

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        r = _walk(ctx, self.follower, self.timeout_s)
        return "OpenAH" if r == "done" else None


class OpenAH(_SState):
    timeout_s = 20.0

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        if _find_template(ctx, "ah_open"):
            return "PostLoop"
        ctx.input.tap(ctx.cfg.selling.key_interact)
        time.sleep(0.4)
        return None


class PostLoop(_SState):
    timeout_s = 1800.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.posts = 0
        self.fails = 0
        self.ev_cursor = len(ctx.channel.events)

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        if self.posts >= ctx.cfg.selling.max_posts_per_run:
            return "CloseAH"
        m = _find_template(ctx, "post_button")
        if m is None:
            # no postable button visible: either done or tab not open
            if self.posts > 0:
                return "CloseAH"
            self.fails += 1
            if self.fails >= 6:
                ctx.log_event("post_button_missing", {})
                return "CloseAH"
            return None
        before_free = (ctx.bagsum() or (None, None))[0]
        ctx.input.click(m.x + m.w // 2, m.y + m.h // 2)
        t0 = time.time()
        confirmed = False
        while time.time() - t0 < ctx.cfg.selling.post_confirm_timeout_s:
            for ev in ctx.channel.events[self.ev_cursor :]:
                self.ev_cursor += 1
                if ev.kind == FastEvKind.AH_POST:
                    confirmed = True
            bag = ctx.bagsum()
            if not confirmed and before_free is not None and bag is not None and bag[0] > before_free:
                confirmed = True
            if confirmed:
                break
            time.sleep(0.1)
        if confirmed:
            self.posts += 1
            self.fails = 0
            ctx.stats["posts"] = self.posts
            return None
        self.fails += 1
        if self.fails >= ctx.cfg.selling.post_max_consecutive_failures:
            ctx.log_event("post_failed_streak", {"posts": self.posts})
            return "CloseAH"
        return None


class CloseAH(_SState):
    timeout_s = 8.0

    def tick(self, ctx) -> Optional[str]:
        if _find_template(ctx, "ah_open"):
            ctx.input.tap("esc")
            time.sleep(0.3)
            return None
        return "ToMail"


class ToMail(_SState):
    timeout_s = 60.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        from ..core.config import load_route

        route = Route.from_dict(load_route(ctx.cfg.selling.route_to_mail))
        self.follower = RouteFollower(route, ctx.cfg.navigation, loop=False)

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        r = _walk(ctx, self.follower, self.timeout_s)
        return "OpenMail" if r == "done" else None


class OpenMail(_SState):
    timeout_s = 30.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        self.loops = 0

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        self.loops += 1
        if self.loops > ctx.cfg.selling.mail_max_loops:
            return "Return"
        m = _find_template(ctx, "mail_frame")
        if m is None:
            ctx.input.tap(ctx.cfg.selling.key_interact)
            time.sleep(0.5)
            return None
        btn = _find_template(ctx, "open_all_mail")
        if btn is None:
            return "Return"  # inbox empty or addon absent
        ctx.input.click(btn.x + btn.w // 2, btn.y + btn.h // 2)
        time.sleep(1.2)
        return None


class Return(_SState):
    timeout_s = 600.0

    def on_enter(self, ctx) -> None:  # noqa: ANN001
        from ..core.config import load_route

        try:
            route = Route.from_dict(load_route(ctx.cfg.selling.route_return))
        except FileNotFoundError:
            self.follower = None
            return
        self.follower = RouteFollower(route, ctx.cfg.navigation, loop=False)

    def tick(self, ctx) -> Optional[str]:
        if (g := self.guard(ctx)):
            return g
        if self.follower is None:
            return "Done"
        r = _walk(ctx, self.follower, self.timeout_s)
        return "Done" if r == "done" else None


class Done(_SState):
    OUTCOME = "completed:sell"

    def tick(self, ctx) -> Optional[str]:  # noqa: ANN001
        ctx.input.release_all()
        return None


class SellerModule(Module):
    name = "seller"

    def build_states(self) -> None:
        for s in (Hearth(), ToAH(), OpenAH(), PostLoop(), CloseAH(), ToMail(), OpenMail(), Return(), Done(), SafeStop()):
            self.machine.add(s)
        self.initial_state = "Hearth"
