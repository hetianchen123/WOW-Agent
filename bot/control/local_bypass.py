"""Bounded left/right doglegs around a measured near-NPC contact.

Waypoints are hypotheses, never a claim that image pixels are world coordinates.
Only released input pulses and fresh position receipts establish traversed paths.
"""
import math

from .navigation import compass_from_delta, heading_error
from .quest_travel import TravelCommand
from .ground_trace import blocked_segment
from ..perception.travel_channel import TaskFlags


class LocalBypass:
    def __init__(self, now, nav, contact, destination, router, convention):
        self.started, self.origin = now, nav.position
        self.router, self.convention = router, convention
        self.height = nav.height
        cx, cy = router.metric(contact)
        tx, ty = router.metric(destination)
        length = math.hypot(tx-cx, ty-cy)
        self.candidates = []
        if .5 <= length <= 15:
            nx, ny = (tx-cx)/length, (ty-cy)/length
            sx, sy = router.metric(nav.position)
            for name, sign in (("right", -1), ("left", 1)):
                dx, dy = sign*ny*4, -sign*nx*4
                points = [((sx+dx)/router.width, (sy+dy)/router.height),
                          ((tx+dx)/router.width, (ty+dy)/router.height), destination]
                if all(0 <= x <= 1 and 0 <= y <= 1 for x, y in points):
                    cost = sum(blocked_segment(router, a, b) for a, b in zip([nav.position]+points, points))
                    self.candidates.append((cost, name, points))
            self.candidates.sort(key=lambda c: (c[0], c[1] != 'right'))
        self.side = None
        self.goals, self.trace = [], [nav.position]
        self.returning = False
        self.pulse = None
        self.pulses = 0
        self.total = 0.0
        self.last_turn, self.settle_until = now-1, now
        self.last_position = nav.position

    def choose(self, ctx, nav):
        if not self.candidates:
            return TravelCommand(outcome="bypass_exhausted")
        _, self.side, self.goals = self.candidates.pop(0)
        self.trace, self.returning = [nav.position], False
        ctx.log_event("quest_bypass_candidate", {"id": nav.quest_id, "side": self.side,
            "origin": nav.position, "waypoints": self.goals, "route_verified": False,
            "stages": ["side", "past_contact", "npc"]})
        return TravelCommand()

    def reject(self, ctx, nav, reason):
        ctx.log_event("quest_bypass_rejected", {"id": nav.quest_id, "side": self.side,
            "reason": reason, "position": nav.position, "returning": self.returning})
        if self.returning:
            return TravelCommand(outcome="bypass_return_blocked")
        self.returning = True
        # Return along actual traversed samples before trying the other side.
        self.goals = list(reversed(self.trace[:-1]))
        self.pulse = None
        return TravelCommand()

    def step(self, ctx, now, nav, heading):
        if now-self.started >= 40 or self.pulses >= 48 or self.total >= 40:
            return TravelCommand(outcome="bypass_budget_exhausted")
        if nav.height is not None and self.height is not None and abs(nav.height-self.height) > 1.5:
            return TravelCommand(outcome="probe_height_drop")
        if not nav.task & TaskFlags.NPC_SELECTED:
            return TravelCommand(outcome="probe_interrupted")
        if nav.task & TaskFlags.NPC_RANGE_KNOWN and nav.task & TaskFlags.NPC_WITHIN_5_YARDS:
            self.router.remember_passage(self.trace)
            ctx.log_event("quest_bypass_range_confirmed", {"id": nav.quest_id, "side": self.side,
                "position": nav.position, "sequence": nav.sequence, "yards": self.total})
            return TravelCommand(outcome="detoured")
        if self.pulse:
            at, origin, sequence, expected, target = self.pulse
            if abs(heading_error(heading, expected)) > math.radians(13):
                return TravelCommand(outcome="probe_pose_changed")
            if now-at < .7:
                return TravelCommand()
            if nav.sequence == sequence:
                return TravelCommand(outcome="probe_telemetry_stale") if now-at >= 1.5 else TravelCommand()
            moved = self.router.distance(origin, nav.position)
            if moved > 2:
                return TravelCommand(outcome="probe_pose_changed")
            if moved >= .15:
                x, y = self.router.metric(origin)
                tx, ty = self.router.metric(nav.position)
                actual = compass_from_delta(tx-x, ty-y, self.convention)
                if abs(heading_error(actual, expected)) > math.radians(35):
                    return TravelCommand(outcome="probe_pose_changed")
            progress = self.router.distance(origin, target)-self.router.distance(nav.position, target)
            self.total += moved
            self.last_position = nav.position
            self.pulse = None
            ctx.log_event("quest_bypass_step", {"id": nav.quest_id, "side": self.side,
                "returning": self.returning, "pulse": self.pulses, "sequence": nav.sequence,
                "from": origin, "position": nav.position, "yards": moved, "progress_yards": progress})
            if moved >= .15:
                self.router.remember_passage([origin, nav.position])
                if not self.returning:
                    self.trace.append(nav.position)
            if moved < .15 or progress < .05:
                self.router.block_ahead(nav.position, target)
                return self.reject(ctx, nav, "no_displacement" if moved < .15 else "sliding_without_progress")
            return TravelCommand()
        if self.router.distance(self.last_position, nav.position) > .2:
            return TravelCommand(outcome="probe_pose_changed")
        if self.side is None:
            return self.choose(ctx, nav)
        while self.goals and self.router.distance(nav.position, self.goals[0]) <= .45:
            self.goals.pop(0)
        if not self.goals:
            if self.returning:
                return self.choose(ctx, nav)
            return self.reject(ctx, nav, "npc_range_not_confirmed")
        target = self.goals[0]
        if blocked_segment(self.router, nav.position, target):
            # For candidate travel only test the NEXT short segment, not the
            # entire guessed line. New blocks still override retraced paths.
            distance = self.router.distance(nav.position, target)
            endpoint = tuple(a+(b-a)*min(1, 1/max(distance, .01)) for a,b in zip(nav.position,target))
            if blocked_segment(self.router, nav.position, endpoint):
                return self.reject(ctx, nav, "known_contact")
        x, y = self.router.metric(nav.position)
        tx, ty = self.router.metric(target)
        desired = compass_from_delta(tx-x, ty-y, self.convention)
        error = heading_error(heading, desired)
        if abs(error) > math.radians(9):
            if now-self.last_turn < .9:
                return TravelCommand()
            self.last_turn, self.settle_until = now, now+.55
            side = 'left' if (error > 0) == (self.convention == 'ccw_west') else 'right'
            return TravelCommand(tap=side, hold_s=min(.16, max(.035, abs(error)/math.radians(240))))
        if now < self.settle_until or now-ctx.heading_age() < self.last_turn+.2:
            return TravelCommand()
        self.pulses += 1
        self.pulse = now, nav.position, nav.sequence, heading, target
        ctx.log_event("quest_bypass_pulse", {"id": nav.quest_id, "side": self.side,
            "returning": self.returning, "pulse": self.pulses, "position": nav.position,
            "target": target, "sequence": nav.sequence, "key": "w", "hold_s": .14})
        return TravelCommand(tap='w', hold_s=.14)
