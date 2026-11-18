"""Recent, input-correlated ground segments; never interpolate missing travel."""
from dataclasses import dataclass
import math

from .navigation import compass_from_delta, heading_error
from .pathfinding import GridRouter
from .quest_travel import TravelCommand
from ..perception.travel_channel import TravelFlags, LifeState

UNSAFE = (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING |
          TravelFlags.SWIMMING | TravelFlags.IN_COMBAT | TravelFlags.CASTING)


def blocked_segment(router, start, end):
    """New collision cells override every part of a previously traversed segment."""
    count = max(1, math.ceil(router.distance(start, end) / (router.cell_size / 2)))
    origin_cell = router.cell(start)
    for i in range(1, count + 1):
        point = tuple(a + (b - a) * i / count for a, b in zip(start, end))
        cell = router.cell(point)
        if cell != origin_cell and cell in router.blocked:
            return True
    return False


@dataclass(frozen=True)
class GroundPoint:
    position: tuple
    at: float
    sequence: int
    height: float | None


class GroundTrace:
    """One uninterrupted local segment. Unknown layers are not persisted."""
    def __init__(self):
        self.clear()

    def clear(self):
        self.points = []
        self.identity = None
        self.previous = None
        self.movement_sent = False

    def note_input(self, keys):
        self.movement_sent |= bool(set(keys) & {"w", "s", "q", "e"})

    def observe(self, nav, now, keys=()):
        if (nav is None or not nav.position or not nav.world_size or nav.flags & UNSAFE
                or not nav.flags & TravelFlags.KEYS_VERIFIED
                or nav.recovery and nav.recovery.life != LifeState.ALIVE):
            self.clear()
            return
        identity = nav.map_id, nav.world_size
        if identity != self.identity:
            self.clear()
            self.identity = identity
        router = GridRouter(*nav.world_size)
        point = GroundPoint(nav.position, now, nav.sequence, nav.height)
        self.note_input(keys)
        if self.previous and self.previous.sequence == nav.sequence:
            if now - self.previous.at > 1.2:
                self.clear()
            return
        if self.previous:
            dt = now - self.previous.at
            distance = router.distance(self.previous.position, point.position)
            drop = (point.height is not None and self.previous.height is not None
                    and abs(point.height - self.previous.height) > 1.5)
            if (dt <= 0 or dt > 1.2 or drop or distance > min(3, 10 * dt + .3)
                    or distance > .03 and not self.movement_sent):
                self.points = []
            # Stationary samples refresh receipt age, not the age of walked segments.
            if distance > .03 and self.points:
                self.points.append(point)
        if not self.points:
            self.points = [point]
        self.points = [p for p in self.points[-160:] if now - p.at <= 30]
        self.previous = point
        self.movement_sent = False

    def backtrack(self, nav, now, router):
        if (not self.previous or self.identity != (nav.map_id, nav.world_size)
                or now - self.previous.at > 1.2 or nav.flags & UNSAFE
                or router.distance(self.previous.position, nav.position) > .15):
            return []
        path, distance, last = [], 0.0, nav.position
        for point in reversed(self.points):
            if now - point.at > 30:
                break
            delta = router.distance(last, point.position)
            if delta < .03:
                continue
            if blocked_segment(router, last, point.position):
                break
            distance += delta
            if distance > 6:
                break
            path.append(point)
            last = point.position
        return path if distance >= .5 else []


class ContactRelease:
    """Probe away from a measured ground contact before requiring a usable image.

    This is a bounded movement experiment, not evidence of a safe rear route.
    Each helper tap releases independently of the next control-loop iteration.
    """
    def __init__(self, now, nav, heading, router, convention, *, target_yards=4, max_pulses=8, budget_s=12):
        self.started, self.origin = now, nav.position
        self.heading, self.height = heading, nav.height
        self.router, self.convention = router, convention
        self.pulses, self.total, self.pulse = 0, 0.0, None
        self.trace = [nav.position]
        self.target_yards, self.max_pulses, self.budget_s = target_yards, max_pulses, budget_s

    def step(self, ctx, now, nav, heading):
        if now - self.started >= self.budget_s:
            return TravelCommand(outcome="escape_time_budget")
        if nav.height is not None and self.height is not None and self.height - nav.height > 1.5:
            return TravelCommand(outcome="probe_height_drop")
        if abs(heading_error(heading, self.heading)) > math.radians(13):
            return TravelCommand(outcome="probe_pose_changed")
        if self.pulse is not None:
            at, origin, sequence = self.pulse
            # .18 s tap plus at least .5 s released; an old frame cannot confirm it.
            if now - at < .68:
                return TravelCommand()
            if nav.sequence == sequence:
                return TravelCommand(outcome="probe_telemetry_stale") if now - at >= 1.5 else TravelCommand()
            moved = self.router.distance(origin, nav.position)
            if moved >= .15:
                x, y = self.router.metric(origin)
                tx, ty = self.router.metric(nav.position)
                actual = compass_from_delta(tx - x, ty - y, self.convention)
                if moved > 1.25 or abs(heading_error(actual, self.heading + math.pi)) > math.radians(30):
                    return TravelCommand(outcome="probe_pose_changed")
            self.total += moved
            ctx.log_event("quest_escape_step", {"basis": "contact_release_probe", "path_verified": False,
                "pulse": self.pulses, "id": nav.quest_id, "from": origin,
                "position": nav.position, "sequence": nav.sequence, "yards": moved,
                "total_yards": self.total})
            self.pulse = None
            if moved < .15:
                return TravelCommand(outcome="escape_no_displacement")
            self.trace.append(nav.position)
            if self.pulses >= self.max_pulses or self.total + .01 >= self.target_yards:
                return TravelCommand(outcome="escape_moved")
            return TravelCommand()
        if self.router.distance(self.trace[-1], nav.position) > .15:
            return TravelCommand(outcome="probe_pose_changed")
        self.pulses += 1
        self.pulse = now, nav.position, nav.sequence
        ctx.log_event("quest_escape_pulse", {"basis": "contact_release_probe", "id": nav.quest_id,
            "pulse": self.pulses, "position": nav.position, "sequence": nav.sequence,
            "heading": heading, "key": "s", "hold_s": .18, "path_verified": False})
        return TravelCommand(tap="s", hold_s=.18)


class GroundBacktrack:
    """Reverse a measured polyline in released pulses, stopping before planning."""
    def __init__(self, now, nav, path, router, convention):
        self.started, self.origin = now, nav.position
        self.path, self.router, self.convention = list(path), router, convention
        self.phase, self.pulses, self.total = "align", 0, 0.0
        self.last_turn, self.settle_until = now - 1, now
        self.pulse = None
        self.released_at = None
        self.height = nav.height
        self.pulse_seconds = .18
        self.trace = [nav.position]

    def step(self, ctx, now, nav, heading):
        if now - self.started >= 30:
            return TravelCommand(outcome="escape_time_budget")
        if nav.height is not None and self.height is not None and self.height - nav.height > 1.5:
            return TravelCommand(outcome="probe_height_drop")
        if self.phase == "walk":
            at, origin, sequence, expected = self.pulse
            moved = self.router.distance(origin, nav.position)
            if moved > .3:
                x, y = self.router.metric(origin)
                tx, ty = self.router.metric(nav.position)
                actual = compass_from_delta(tx - x, ty - y, self.convention)
                if moved > 2 or abs(heading_error(actual, expected + math.pi)) > math.radians(30):
                    return TravelCommand(outcome="probe_pose_changed")
            if abs(heading_error(heading, expected)) > math.radians(13):
                return TravelCommand(outcome="probe_pose_changed")
            if now - at < self.pulse_seconds and moved < .65:
                return TravelCommand(frozenset({"s"}))
            if self.released_at is None:
                self.released_at = now
                return TravelCommand()
            if now - self.released_at < .5:
                return TravelCommand()
            if nav.sequence == sequence:
                return TravelCommand(outcome="probe_telemetry_stale") if now - at > 1.5 else TravelCommand()
            self.total += moved
            ctx.log_event("quest_escape_step", {"basis": "recent_ground_trace", "pulse": self.pulses,
                "id": nav.quest_id, "from": origin, "position": nav.position, "sequence": nav.sequence,
                "yards": moved, "total_yards": self.total})
            if moved < .15:
                self.router.block_ahead(nav.position, self.path[0].position)
                return TravelCommand(outcome="escape_no_displacement")
            self.trace.append(nav.position)
            if self.total >= 1.95 or self.pulses >= 8:
                return TravelCommand(outcome="escape_moved")
            self.phase = "align"
            # Consume only points actually reached, never cut across a corner.
            while self.path and self.router.distance(nav.position, self.path[0].position) <= .35:
                self.path.pop(0)
            return TravelCommand()
        while self.path and self.router.distance(nav.position, self.path[0].position) < .2:
            self.path.pop(0)
        if not self.path:
            return TravelCommand(outcome="escape_moved")
        target = self.path[0]
        if now - target.at > 30:
            return TravelCommand(outcome="escape_trace_expired")
        if blocked_segment(self.router, nav.position, target.position):
            return TravelCommand(outcome="escape_trace_blocked")
        x, y = self.router.metric(nav.position)
        tx, ty = self.router.metric(target.position)
        desired = (compass_from_delta(tx - x, ty - y, self.convention) + math.pi) % (2 * math.pi)
        error = heading_error(heading, desired)
        if abs(error) > math.radians(9):
            if now - self.last_turn < .9:
                return TravelCommand()
            self.last_turn, self.settle_until = now, now + .55
            side = "left" if (error > 0) == (self.convention == "ccw_west") else "right"
            return TravelCommand(tap=side, hold_s=min(.16, max(.035, abs(error) / math.radians(240))))
        if now < self.settle_until or now - ctx.heading_age() < self.last_turn + .2:
            return TravelCommand()
        self.pulses += 1
        self.pulse_seconds = min(.18, max(.04, self.router.distance(nav.position, target.position) / 4.5))
        self.pulse, self.released_at = (now, nav.position, nav.sequence, heading), None
        self.phase = "walk"
        return TravelCommand(frozenset({"s"}))
