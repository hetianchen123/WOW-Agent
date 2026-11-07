"""Navigation: route following with closed-loop steering, stuck detection and
recovery — pure logic, no I/O, fully unit-testable.

Coordinate conventions:
  * map position (x, y): fractions 0..1 of the uiMap canvas; x east+, y south+
  * heading: radians as reported by GetPlayerFacing (north = 0). Two possible
    handednesses exist; configured via facing_convention:
      ccw_west: heading increases counterclockwise (west = pi/2)  [WoW standard]
      cw_east : heading increases clockwise (east = pi/2)
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

from ..core.config import NavigationConfig


def wrap_pi(a: float) -> float:
    """Wrap to (-pi, pi]."""
    a = (a + math.pi) % (2.0 * math.pi) - math.pi
    return a if a != -math.pi else math.pi


def compass_from_delta(dx: float, dy: float, convention: str = "ccw_west") -> float:
    """Compass heading (north=0) toward the delta (dx east+, dy south+)."""
    if convention == "cw_east":
        return (math.atan2(dx, -dy)) % (2.0 * math.pi)
    return math.atan2(-dx, -dy) % (2.0 * math.pi)


def heading_error(current: float, target: float) -> float:
    """Signed shortest correction (target - current) in (-pi, pi]."""
    return wrap_pi(target - current)


def dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


@dataclass
class Route:
    name: str
    ui_map_id: int
    mode: str = "fly"  # fly | ground
    points: List[Tuple[float, float]] = field(default_factory=list)
    arrival_radius: Optional[float] = None  # map units; None -> auto

    @classmethod
    def from_dict(cls, d: dict) -> "Route":
        pts = [(float(p[0]), float(p[1])) for p in (d.get("points") or [])]
        ar = d.get("arrival_radius")
        return cls(
            name=str(d.get("name", "route")),
            ui_map_id=int(d.get("ui_map_id", 0)),
            mode=str(d.get("mode", "fly")),
            points=pts,
            arrival_radius=float(ar) if ar else None,
        )

    def median_segment(self) -> float:
        if len(self.points) < 2:
            return 0.005
        segs = sorted(dist(p, q) for p, q in zip(self.points, self.points[1:]))
        return segs[len(segs) // 2]

    def effective_radius(self) -> float:
        if self.arrival_radius:
            return self.arrival_radius
        return max(1e-4, self.median_segment() * 0.4)


@dataclass
class NavDecision:
    move_forward: bool = False
    turn_err: Optional[float] = None  # rad, signed; None = keep current heading
    turn_to: Optional[float] = None
    dist_to_wp: float = 0.0
    wp_index: int = 0
    finished: bool = False
    finish_reason: str = ""
    stuck_event: bool = False
    recovery: Optional[str] = None  # 'back' | 'jump' | None


class RouteFollower:
    """Drives waypoint-to-waypoint movement decisions.

    The module translates decisions into input: turn_err -> rmb_turn, 
    move_forward -> hold W, recovery -> tap S / space.
    """

    def __init__(self, route: Route, cfg: NavigationConfig, loop: bool = True) -> None:
        self.route = route
        self.cfg = cfg
        self.loop = loop
        self.wp = 0
        self._history: Deque[Tuple[float, float, float]] = deque(maxlen=64)
        self.stuck_count = 0
        self._recovery_queue: List[str] = []
        self._recovery_wait_s = 0.0
        self._intended_move_s = 0.0

    # ---------------------------------------------------------------- helpers
    def _record(self, t: float, x: float, y: float) -> None:
        self._history.append((t, x, y))

    def _displacement_over(self, window_s: float) -> float:
        if len(self._history) < 2:
            return 0.0
        t_new, x_new, y_new = self._history[-1]
        for t_old, x_old, y_old in self._history:
            if t_new - t_old <= window_s:
                continue
            return math.hypot(x_new - x_old, y_new - y_old)
        # whole history inside the window: use oldest sample
        t_old, x_old, y_old = self._history[0]
        return math.hypot(x_new - x_old, y_new - y_old)

    # ------------------------------------------------------------------ tick
    def update(self, t: float, pos: Tuple[float, float], heading: float, moving_flag: bool) -> NavDecision:
        d = NavDecision()
        if not self.route.points:
            d.finished = True
            d.finish_reason = "empty_route"
            return d

        # recovery takes priority: execute queued actions one at a time
        if self._recovery_wait_s > 0.0:
            self._recovery_wait_s = 0.0
        if self._recovery_queue:
            d.recovery = self._recovery_queue.pop(0)
            return d

        self._record(t, pos[0], pos[1])
        target = self.route.points[self.wp]
        d.wp_index = self.wp
        d.dist_to_wp = dist(pos, target)

        if d.dist_to_wp <= self.route.effective_radius():
            self.wp += 1
            if self.wp >= len(self.route.points):
                if self.loop:
                    self.wp = 0
                else:
                    d.finished = True
                    d.finish_reason = "route_complete"
                    return d
            target = self.route.points[self.wp]
            d.wp_index = self.wp
            d.dist_to_wp = dist(pos, target)

        d.turn_to = compass_from_delta(target[0] - pos[0], target[1] - pos[1], self.cfg.facing_convention)
        d.turn_err = heading_error(heading, d.turn_to)
        d.move_forward = abs(d.turn_err) < math.radians(45.0)

        # ------------------------------------------------- stuck detection
        disp = self._displacement_over(self.cfg.stuck_window_s)
        if (
            d.move_forward
            and len(self._history) >= 8
            and disp < self.cfg.stuck_displacement
        ):
            # require that we have a full window of history
            span = self._history[-1][0] - self._history[0][0]
            if span >= self.cfg.stuck_window_s * 0.8:
                self.stuck_count += 1
                d.stuck_event = True
                self._history.clear()
                self._recovery_queue = ["back", "jump"]
                if self.stuck_count >= self.cfg.stuck_max_events:
                    d.finished = True
                    d.finish_reason = "stuck_escalation"
                    d.stuck_event = False
                    self._recovery_queue = []
                return d
        elif disp >= self.cfg.stuck_displacement * 2:
            self.stuck_count = 0
        return d


def minimap_bearing(offset_x_px: float, offset_y_px: float, convention: str = "ccw_west") -> float:
    """Bearing (north=0) of a point offset from minimap center (north-up lock)."""
    return compass_from_delta(offset_x_px, offset_y_px, convention)


def minimap_distance_yards(offset_x_px: float, offset_y_px: float, yards_per_px: float) -> float:
    return math.hypot(offset_x_px, offset_y_px) * yards_per_px
