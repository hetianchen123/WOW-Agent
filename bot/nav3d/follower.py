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
    @staticmethod
    def validate_route(route: Path3D) -> None:
        if not route.points or any(point3(p) != p for p in route.points):
            raise NavigationError("invalid_route_points")
        if any(math.dist(a,b) > .6 for a,b in zip(route.points,route.points[1:])):
            raise NavigationError("follower_requires_dense_surface_path")

    def __init__(self, route: Path3D, *, now: float, mesh_id: str, obstacle_revision: str,
                 alignment_verified: bool = False, supervised_trial: bool = False, budget: float = 90,
                 short_steps: bool = False, arrival_radius: float | None = None,
                 feedback_paced: bool = False):
        if route.frame.coordinate_system != "east_north_up":
            raise NavigationError("follower_requires_east_north_up")
        if not math.isfinite(now) or not 0 < budget <= 300 or not math.isfinite(budget):
            raise NavigationError("invalid_follower_budget")
        if arrival_radius is not None and not .1 <= arrival_radius <= .5:
            raise NavigationError("invalid_follower_arrival_radius")
        self.validate_route(route)
        self.route, self.mesh_id, self.revision = route, mesh_id, obstacle_revision
        self.alignment_verified = alignment_verified
        self.supervised_trial = supervised_trial
        self.short_steps = short_steps
        self.arrival_radius = arrival_radius
        self.feedback_paced = feedback_paced
        self.deadline = now+budget
        self.index, self.sequence, self.last_pose = 0, None, None
        self.progress_at, self.last_turn, self.last_direction = now, float("-inf"), None
        self.short_step_until = float("-inf")
        self.short_step_settle_at = None
        self.travelled = 0.0
        self.last_delta = 0.0
        self.last_capture_at = None
        self.speed = 0.0
        self.commanded_motion = False
        self.hold_forward = False
        self.approaching_goal = False
        self.terminal = None

    def stop(self, reason):
        self.terminal = reason
        return TravelCommand(outcome=reason)

    def resume_corridor(self, route: Path3D, *, now):
        if (self.terminal != "nav3d_route_deviation" or route.frame != self.route.frame
                or route.mesh_id != self.mesh_id or route.obstacle_revision != self.revision
                or route.requested_goal != self.route.requested_goal):
            raise NavigationError("invalid_corridor_repair")
        self.validate_route(route)
        self.route, self.index, self.terminal = route, 0, None
        self.hold_forward = self.commanded_motion = False
        self.approaching_goal = False
        self.progress_at, self.travelled = now, 0.0

    def route_corner(self, i):
        points = self.route.points
        if not 0 < i < len(points) - 1:
            return False
        before, center, after = points[i-1:i+2]
        incoming = (center[0] - before[0], center[1] - before[1])
        outgoing = (after[0] - center[0], after[1] - center[1])
        scale = math.hypot(*incoming) * math.hypot(*outgoing)
        return scale > 1e-6 and sum(a*b for a, b in zip(incoming, outgoing)) < scale * .94

    @staticmethod
    def distance_to_route(position, points):
        distance = math.dist(position, points[0])
        for a, b in zip(points, points[1:]):
            line = tuple(end - start for start, end in zip(a, b))
            length2 = sum(step*step for step in line)
            if length2 > 0:
                offset = tuple(value - start for start, value in zip(a, position))
                t = min(1, max(0, sum(step*value for step, value in zip(line, offset)) / length2))
                distance = min(distance, math.hypot(*(value - t*step for value, step in zip(offset, line))))
        return distance

    def step(self, now, pose: WorldPose | None, *, mesh_id: str, obstacle_revision: str,
             cancelled=False, retreating=False, forward_clearance=None) -> TravelCommand:
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
        if not pose.grounded or (pose.in_combat and not retreating) or not pose.bindings_verified:
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
            elapsed = pose.captured_at - self.last_capture_at
            if .025 <= elapsed <= .5:
                self.speed = .5 * self.speed + .5 * min(8., math.dist(position[:2], self.last_pose[:2]) / elapsed)
            if self.commanded_motion:
                self.travelled += delta
                if self.travelled >= .1:
                    self.progress_at, self.travelled = now, 0.0
                elif now-self.progress_at > 2:
                    return self.stop("nav3d_stuck_replan_required")
        self.last_pose, self.last_capture_at = position, pose.captured_at
        if self.short_step_settle_at is not None:
            # A new color-bar sequence can still contain the pre-pulse position.
            # Wait out late movement before measuring arrival or sending again.
            if self.last_delta >= .05:
                self.short_step_settle_at = max(now, self.short_step_until) + .12
            if now < self.short_step_settle_at:
                return TravelCommand()
            self.short_step_settle_at = None
        # At running speed a single receipt can skip several dense vertices.
        # Grow the lookahead on straight segments and approach bends separately.
        lookahead = .68 if self.short_steps else min(1.8, max(self.last_delta, .85 + .2 * self.speed))
        while self.index < len(self.route.points)-1:
            reach = .28 if self.route_corner(self.index) else lookahead
            if math.dist(position, self.route.points[self.index]) > reach:
                break
            self.index += 1
        target = self.route.points[self.index]
        corner = self.route_corner(self.index)
        near_edge = False
        if not self.short_steps and forward_clearance is not None:
            # Leave room for the next receipt plus delayed key-up at running speed.
            horizon = .35 + .22 * max(5., self.speed)
            try:
                verified = forward_clearance(pose.heading, horizon)
            except NavigationError:
                verified = 0.
            near_edge = verified + .001 < horizon
        if not self.short_steps and self.arrival_radius is not None and not self.approaching_goal:
            braking_distance = min(2.5, max(1.0, .35 + .26 * self.speed))
            if math.dist(position, self.route.points[-1]) <= braking_distance:
                tail = self.route.points[self.index:]
                remaining = math.dist(position, target) + sum(math.dist(a, b) for a, b in zip(tail, tail[1:]))
                self.approaching_goal = remaining <= braking_distance
        if self.hold_forward and (self.approaching_goal or (corner and self.feedback_paced) or near_edge):
            # A previous cruise hold may still produce a later position receipt.
            # Release before a short step near an edge or a precise arrival.
            self.hold_forward = self.commanded_motion = False
            self.short_step_until = now
            self.short_step_settle_at = now + .18
            return TravelCommand()
        arrival = self.arrival_radius if self.arrival_radius is not None else max(.18, self.last_delta)
        if (self.index == len(self.route.points)-1
                and math.dist(position, self.route.points[-1]) <= arrival
                and abs(position[2]-self.route.requested_goal[2]) <= .3):
            # The final frame can carry the character past a boundary vertex
            # (exit points sit on the mesh edge); arriving within one frame's
            # displacement still counts, with the floor check unchanged.
            return self.stop("nav3d_route_arrived")
        window = self.route.points[max(0, self.index-4):self.index+8]
        if self.distance_to_route(position, window) > 2.0:
            return self.stop("nav3d_route_deviation")
        dx, dy, dz = (b-a for a,b in zip(position,target))
        horizontal_distance = math.hypot(dx, dy)
        if horizontal_distance < .03 and abs(dz) > .3:
            return self.stop("nav3d_floor_mismatch")
        error = heading_error(pose.heading, math.atan2(-dx,dy) % math.tau)
        use_short_step = self.short_steps or self.approaching_goal or corner or near_edge
        turn_tolerance = math.radians(6 if use_short_step else 10)
        if use_short_step and abs(error) <= turn_tolerance:
            self.hold_forward = False
            self.commanded_motion = True
            if now < self.short_step_until:
                return TravelCommand()
            distance_to_target = horizontal_distance
            if self.feedback_paced and self.index == len(self.route.points) - 1:
                distance_to_target = max(0., distance_to_target - .08)
            duration = min(.085, max(.035, distance_to_target / 7))
            if self.feedback_paced and (corner or near_edge) and abs(dz) > max(.15, .75 * horizontal_distance):
                # Sample the slope before taking another full stride at a bend
                # or edge. The next pulse still waits for settled pose feedback.
                duration = .035
            self.short_step_until = now + duration
            if self.feedback_paced:
                self.short_step_settle_at = self.short_step_until + .30
            return TravelCommand(tap="w", hold_s=duration, precise=True)
        # Smooth travel holds W across control cycles (the field-tested legacy
        # pattern) and steers with bounded precise turn pulses on top. Only a
        # misalignment large enough to drift out of the dense-waypoint corridor
        # releases W to rotate in place; deviation replans and stuck checks
        # below still bound every hold.
        running = not use_short_step and abs(error) <= math.radians(15)
        self.hold_forward = running
        self.commanded_motion = running
        if abs(error) > turn_tolerance:
            direction = "left" if error > 0 else "right"
            opposite = self.last_direction and direction != self.last_direction
            interval = (.45 if opposite else .24) if self.short_steps else (.9 if opposite else .45)
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
