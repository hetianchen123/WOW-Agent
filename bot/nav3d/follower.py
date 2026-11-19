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
                 alignment_verified: bool = False, supervised_trial: bool = False, budget: float = 90):
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
        self.supervised_trial = supervised_trial
        self.deadline = now+budget
        self.index, self.sequence, self.last_pose = 0, None, None
        self.progress_at, self.last_turn, self.last_direction = now, float("-inf"), None
        self.travelled = 0.0
        self.last_delta = 0.0
        self.commanded_motion = False
        self.hold_forward = False
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
        if not (self.alignment_verified or self.supervised_trial):
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
            # Same receipt: keep the established hold instead of stutter-stepping;
            # freshness is still bounded by the captured_at check above.
            return TravelCommand(keys=frozenset({"w"}) if self.hold_forward else frozenset())
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
            self.last_delta = delta
            if self.commanded_motion:
                self.travelled += delta
                if self.travelled >= .1:
                    self.progress_at, self.travelled = now, 0.0
                elif now-self.progress_at > 2:
                    return self.stop("nav3d_stuck_replan_required")
        self.last_pose = position
        # Pure-pursuit targeting: dense waypoints inside the lookahead radius
        # are consumed so one 5 Hz receipt at running speed (~1.4 yards) cannot
        # leave the target behind the character. Run 20261118T142539 chased
        # blown-past early waypoints backward and spun in place.
        while (self.index < len(self.route.points)-1
                and math.dist(position, self.route.points[self.index]) <= 1.8):
            self.index += 1
        if (self.index == len(self.route.points)-1
                and math.dist(position, self.route.points[-1]) <= max(.18, self.last_delta)
                and abs(position[2]-self.route.requested_goal[2]) <= .3):
            # The final frame can carry the character past a boundary vertex
            # (exit points sit on the mesh edge); arriving within one frame's
            # displacement still counts, with the floor check unchanged.
            return self.stop("nav3d_route_arrived")
        target = self.route.points[self.index]
        window = self.route.points[max(0, self.index-4):self.index+8]
        if min(math.dist(position, point) for point in window) > 2.0:
            # Drift is judged against the polyline, not the pursuit target, so
            # the lookahead distance itself can never trigger deviation.
            return self.stop("nav3d_route_deviation")
        dx, dy, dz = (b-a for a,b in zip(position,target))
        if math.hypot(dx,dy) < .03 and abs(dz) > .3:
            return self.stop("nav3d_floor_mismatch")
        error = heading_error(pose.heading, math.atan2(-dx,dy) % math.tau)
        # Smooth travel holds W across control cycles (the field-tested legacy
        # pattern) and steers with bounded precise turn pulses on top. Only a
        # misalignment large enough to drift out of the dense-waypoint corridor
        # releases W to rotate in place; deviation replans and stuck checks
        # below still bound every hold.
        running = abs(error) <= math.radians(15)
        self.hold_forward = running
        self.commanded_motion = running
        if abs(error) > math.radians(10):
            direction = "left" if error > 0 else "right"
            interval = .9 if self.last_direction and direction != self.last_direction else .45
            if now-self.last_turn < interval:
                return TravelCommand(keys=frozenset({"w"}) if running else frozenset())
            self.last_turn, self.last_direction = now, direction
            self.progress_at = now
            # InputBase.tap_precise accepts movement pulses up to 120 ms.
            # Larger turns use successive pulses with fresh heading feedback.
            # Rate calibrated from run 20261118T142539 receipts: 0.12 s pulses
            # turned 22.5 deg, i.e. ~190 deg/s (was assumed 240).
            return TravelCommand(keys=frozenset({"w"}) if running else frozenset(),
                tap=direction, hold_s=min(.12,max(.03,abs(error)/math.radians(190))), precise=True)
        return TravelCommand(keys=frozenset({"w"}))
