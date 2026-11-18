"""Bounded ground-route follower yielding existing TravelCommand intents.

Caller must supply fresh, verified XYZ in the artifact's frame and preserve the
normal input executor's focus, session authorization and key-release checks.
This module never sends keys and is not silently selected by the task runner.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from ..control.navigation import heading_error
from ..control.quest_travel import TravelCommand
from .planner import Frame, NavigationError, Path3D, point3


@dataclass(frozen=True)
class WorldPose:
    frame: Frame
    position: tuple[float, float, float]
    heading: float  # GetPlayerFacing: north=0, west=pi/2, in east/north/up frame.
    sequence: int
    captured_at: float  # monotonic clock, same time base as step(now)
    grounded: bool
    bindings_verified: bool
    in_combat: bool = False


class WorldRouteFollower:
    def __init__(self, route: Path3D, *, now: float, mesh_id: str, obstacle_revision: str,
                 alignment_verified: bool = False, budget: float = 90):
        if route.frame.coordinate_system != "east_north_up":
            raise NavigationError("follower_requires_east_north_up")
        if not math.isfinite(now) or not 0 < budget <= 300 or not math.isfinite(budget):
            raise NavigationError("invalid_follower_budget")
        if not route.points or any(point3(p) != p for p in route.points):
            raise NavigationError("invalid_route_points")
        if any(math.dist(a,b) > .6 for a,b in zip(route.points,route.points[1:])):
            raise NavigationError("follower_requires_dense_surface_path")
        self.route, self.mesh_id, self.revision = route, mesh_id, obstacle_revision
        self.alignment_verified = alignment_verified
        self.deadline = now+budget
        self.index, self.sequence, self.last_pose = 0, None, None
        self.progress_at, self.last_turn, self.last_direction = now, float("-inf"), None
        self.travelled = 0.0
        self.commanded_motion = False
        self.terminal = None

    def stop(self, reason):
        self.terminal = reason
        return TravelCommand(outcome=reason)

    def step(self, now, pose: WorldPose | None, *, mesh_id: str, obstacle_revision: str,
             cancelled=False) -> TravelCommand:
        if self.terminal:
            return TravelCommand(outcome=self.terminal)
        if cancelled:
            return self.stop("nav3d_cancelled")
        if not math.isfinite(now) or now > self.deadline:
            return self.stop("nav3d_budget_exhausted")
        if not self.alignment_verified:
            return self.stop("nav3d_world_alignment_unverified")
        if mesh_id != self.mesh_id or self.mesh_id != self.route.mesh_id or obstacle_revision != self.revision or self.revision != self.route.obstacle_revision:
            return self.stop("nav3d_replan_required")
        if pose is None:
            return self.stop("nav3d_position_unavailable")
        if pose.frame != self.route.frame:
            return self.stop("nav3d_world_frame_changed")
        try:
            position = point3(pose.position)
        except NavigationError:
            return self.stop("nav3d_position_unavailable")
        if (not math.isfinite(pose.captured_at) or not 0 <= now-pose.captured_at <= .5
                or not math.isfinite(pose.heading)):
            return self.stop("nav3d_stale_pose")
        if not pose.grounded or pose.in_combat or not pose.bindings_verified:
            return self.stop("nav3d_state_unsafe")
        if self.sequence == pose.sequence:
            return TravelCommand()  # Release keys while awaiting a new receipt.
        self.sequence = pose.sequence
        # Never snap a pose to a different floor or jump to a later route branch.
        target = self.route.points[self.index]
        distance = math.dist(position, target)
        if self.last_pose is None:
            if distance > .5:
                return self.stop("nav3d_start_mismatch")
        else:
            delta = math.dist(position, self.last_pose)
            if delta > 2:
                return self.stop("nav3d_displacement_discontinuity")
            if self.commanded_motion:
                self.travelled += delta
                if self.travelled >= .1:
                    self.progress_at, self.travelled = now, 0.0
                elif now-self.progress_at > 2:
                    return self.stop("nav3d_stuck_replan_required")
        self.last_pose = position
        # Dense path points are consumed sequentially; no look-ahead across walls.
        while math.dist(position, self.route.points[self.index]) <= .18:
            if self.index == len(self.route.points)-1:
                if (math.dist(position, self.route.points[-1]) <= .18
                        and abs(position[2]-self.route.requested_goal[2]) <= .3):
                    return self.stop("nav3d_route_arrived")
                break
            self.index += 1
        target = self.route.points[self.index]
        if math.dist(position, target) > 1.0:
            return self.stop("nav3d_route_deviation")
        dx, dy, dz = (b-a for a,b in zip(position,target))
        if math.hypot(dx,dy) < .03 and abs(dz) > .3:
            return self.stop("nav3d_floor_mismatch")
        error = heading_error(pose.heading, math.atan2(-dx,dy) % math.tau)
        self.commanded_motion = False
        if abs(error) > math.radians(10):
            direction = "left" if error > 0 else "right"
            interval = .9 if self.last_direction and direction != self.last_direction else .45
            if now-self.last_turn < interval:
                return TravelCommand()
            self.last_turn, self.last_direction = now, direction
            self.progress_at = now
            return TravelCommand(tap=direction, hold_s=min(.16,max(.03,abs(error)/math.radians(240))), precise=True)
        if now-self.last_turn < .45:
            return TravelCommand()
        self.commanded_motion = True
        # Short pulses keep the next target within the verified corridor. Inputs
        # must be released by the existing executor before observing again.
        return TravelCommand(tap="w", hold_s=min(.03,max(.015,math.hypot(dx,dy)/7)), precise=True)
