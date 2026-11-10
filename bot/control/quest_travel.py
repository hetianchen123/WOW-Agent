"""Closed-loop travel using verified flight, mount and movement bindings."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..perception.travel_channel import TravelFlags as Flags, TravelSample
from .navigation import compass_from_delta, heading_error
from .pathfinding import GridRouter

FINAL_APPROACH_YARDS = 32


@dataclass(frozen=True)
class TravelCommand:
    keys: frozenset[str] = frozenset()
    tap: str | None = None
    outcome: str | None = None


class QuestTrip:
    def __init__(self, world_size, destination, convention, *, arrival_radius=4, walk_only=False):
        self.destination, self.convention = destination, convention
        self.arrival_radius = arrival_radius
        self.walk_only = walk_only
        self.router = GridRouter(*world_size)
        self.mode = None
        self.phase, self.phase_at = "choose", 0.0
        self.failed_modes: set[str] = set()
        self.path = []
        self.motion_position, self.motion_at = None, 0.0
        self.altitude = None
        self.climbs = self.detours = 0
        self.style_attempted = False
        self.sky_started = None
        self.sky_landing_target = None
        self.sky_land_height = None

    def enter(self, phase, now):
        self.phase, self.phase_at = phase, now
        self.motion_position = None

    def fail_mode(self, now):
        self.failed_modes.add(self.mode)
        self.enter("choose", now)
        return TravelCommand()

    def step(self, now: float, sample: TravelSample, heading: float) -> TravelCommand:
        flags, position = sample.flags, sample.position
        descent_key = sample.descent_key
        sky_controls = bool(sample.sky_pitch_key and sample.sky_dismount_key)
        mounted, flying = bool(flags & Flags.MOUNTED), bool(flags & Flags.FLYING)
        can_fly = (
            bool(flags & Flags.CAN_FLY) and "fly" not in self.failed_modes
            and (sky_controls or (descent_key is not None and sample.height is not None))
        )
        can_ride = bool(mounted or flags & Flags.CAN_MOUNT) and "mount" not in self.failed_modes
        if not flags & Flags.KEYS_VERIFIED or position is None:
            return TravelCommand(outcome="travel_bindings_or_position_unavailable")
        distance = self.router.distance(position, self.destination)
        if self.walk_only or (not mounted and distance <= FINAL_APPROACH_YARDS):
            can_fly = can_ride = False
        if flags & (Flags.IN_COMBAT | Flags.SWIMMING):
            return TravelCommand(outcome="travel_state_unsafe")
        if self.mode == "skyriding" or (flags & Flags.SKYRIDING and sky_controls):
            return self.sky_step(now, sample, heading, mounted, flying)
        if descent_key is None:
            if flying:
                return TravelCommand(outcome="descent_binding_unavailable")
            if self.phase == "takeoff":
                return self.fail_mode(now)
        if flying and flags & Flags.SKYRIDING:
            return TravelCommand(outcome="steady_flight_not_confirmed")
        if flags & Flags.FALLING and not flying:
            return TravelCommand()

        near = distance <= self.arrival_radius
        elapsed = now - self.phase_at
        if self.phase == "land":
            if flying:
                return (TravelCommand(outcome="landing_not_confirmed") if elapsed > 18
                        else TravelCommand(frozenset({descent_key})))
            if not near:
                self.failed_modes.add("fly")
            self.enter("choose", now)
        if self.phase in {"dismount", "change_mount"}:
            if mounted:
                return TravelCommand(outcome="dismount_not_confirmed") if elapsed > 4 else TravelCommand()
            self.enter("choose", now)
        if near:
            if flying:
                self.enter("land", now)
                return TravelCommand(frozenset({descent_key}))
            if mounted:
                self.enter("dismount", now)
                return TravelCommand(tap="8")
            return TravelCommand(outcome="arrived")
        if self.phase == "style":
            if mounted and flags & Flags.SKYRIDING:
                return self.fail_mode(now) if elapsed > 9 else TravelCommand()
            if flags & Flags.CASTING:
                return self.fail_mode(now) if elapsed > 9 else TravelCommand()
            self.enter("choose", now)
        if self.phase == "mount":
            if not mounted:
                return self.fail_mode(now) if elapsed > 5 else TravelCommand()
            self.enter("choose", now)
        if self.phase == "recover":
            if elapsed < .6:
                return TravelCommand(frozenset({"s"}))
            self.enter("choose", now)
        if self.phase == "takeoff":
            if not flying:
                return self.fail_mode(now) if elapsed > 3 else TravelCommand(frozenset({"space"}))
            self.enter("climb", now)
        if self.phase == "climb":
            if not flying or sample.height is None:
                return TravelCommand(outcome="flight_height_unavailable")
            if sample.height < self.altitude - 2:
                return (TravelCommand(outcome="climb_not_confirmed") if elapsed > 7
                        else TravelCommand(frozenset({"space"})))
            self.enter("cruise", now)

        if self.phase == "cruise" and not flying and (
            (self.mode == "mount" and can_fly) or (self.mode == "walk" and (can_fly or can_ride))
        ):
            self.enter("choose", now)
        if self.phase == "choose":
            if flying:
                if sample.height is None:
                    return TravelCommand(outcome="flight_height_unavailable")
                self.mode, self.altitude = "fly", sample.height
            elif can_fly:
                self.mode = "fly"
                if not mounted:
                    self.enter("mount", now)
                    return TravelCommand(tap="6")
                if not sample.flight_mount_active:
                    self.enter("change_mount", now)
                    return TravelCommand(tap="8")
                if flags & Flags.SKYRIDING:
                    if self.style_attempted or not flags & Flags.STYLE_SWITCH:
                        return self.fail_mode(now)
                    self.style_attempted = True
                    self.enter("style", now)
                    return TravelCommand(tap="5")
                if descent_key is None or sample.height is None:
                    return self.fail_mode(now)
                self.altitude = sample.height + 12
                self.enter("takeoff", now)
                return TravelCommand(frozenset({"space"}))
            elif can_ride:
                self.mode = "mount"
                if not mounted:
                    self.enter("mount", now)
                    return TravelCommand(tap="7")
            else:
                self.mode = "walk"
            self.path = [self.destination] if self.mode == "fly" else self.router.route(position, self.destination)
            self.enter("cruise", now)

        if self.mode == "fly" and not flying:
            return self.fail_mode(now)
        if self.mode == "mount" and not mounted:
            return self.fail_mode(now)
        return self.steer(now, position, heading, sample.height, descent_key)

    def sky_step(self, now, sample, heading, mounted, flying):
        target = self.sky_landing_target or self.destination
        distance = self.router.distance(sample.position, target)
        if not mounted:
            self.mode = None
            self.failed_modes.add("fly")
            self.enter("choose", now)
            return TravelCommand()
        if not (sample.sky_pitch_key and sample.sky_dismount_key and sample.flags & Flags.SKYRIDING):
            return TravelCommand(outcome="skyriding_controls_unavailable")
        if sample.flags & Flags.FALLING and not flying:
            return TravelCommand()
        if self.mode != "skyriding":
            self.mode = "skyriding"
            self.sky_started = now
            self.enter("sky_glide" if flying else "sky_align", now)
        elapsed = now - self.phase_at
        if now - self.sky_started > 60:
            return TravelCommand(outcome="skyriding_timeout")

        if not flying and (
            distance <= FINAL_APPROACH_YARDS
            or self.phase in {"sky_glide", "sky_brake", "sky_land", "sky_recover", "sky_dismount"}
        ):
            if self.phase == "sky_dismount":
                return TravelCommand(outcome="dismount_not_confirmed") if elapsed > 4 else TravelCommand()
            self.enter("sky_dismount", now)
            return TravelCommand(tap=sample.sky_dismount_key)

        turn = self.turn_error(sample.position, target, heading)
        steering = self.turn_keys(turn, math.radians(8))
        if self.phase == "sky_align":
            if elapsed > 10:
                self.failed_modes.add("fly")
                self.enter("sky_dismount", now)
                return TravelCommand(tap=sample.sky_dismount_key)
            if steering or not sample.sky_ascent_key:
                return TravelCommand(steering)
            self.enter("sky_launch", now)
            return TravelCommand(tap=sample.sky_ascent_key)
        if self.phase == "sky_launch":
            if not flying:
                if elapsed <= 3:
                    return TravelCommand()
                self.enter("sky_dismount", now)
                return TravelCommand(tap=sample.sky_dismount_key)
            self.enter("sky_glide", now)
        if self.phase == "sky_recover":
            if elapsed < .6:
                return TravelCommand()
            self.enter("sky_glide", now)
        if self.phase == "sky_glide":
            brake_distance = 14 if self.sky_landing_target else FINAL_APPROACH_YARDS
            if distance > brake_distance:
                return TravelCommand(steering)
            self.enter("sky_brake", now)
            if sample.sky_halt_key:
                return TravelCommand(tap=sample.sky_halt_key)
        if self.phase == "sky_brake":
            if now - self.phase_at < .35:
                return TravelCommand(steering)
            self.enter("sky_land", now)
        if self.phase == "sky_land":
            landing_time = now - self.phase_at
            if landing_time > 8:
                return TravelCommand(outcome="skyriding_landing_not_confirmed")
            height_changed = (
                sample.height is not None and self.sky_land_height is not None
                and abs(sample.height - self.sky_land_height) >= 1.2
            )
            if (self.motion_position is None or height_changed
                    or self.router.distance(self.motion_position, sample.position) >= 1.2):
                self.motion_position, self.motion_at = sample.position, now
                self.sky_land_height = sample.height
            elif now - self.motion_at >= 1.4:
                return self.recover_sky_landing(now, sample)
            if distance <= 12:
                steering = frozenset()
            pitch = {sample.sky_pitch_key} if landing_time % .7 < .45 else set()
            return TravelCommand(steering | pitch)
        return TravelCommand(outcome="skyriding_state_unexpected")

    def recover_sky_landing(self, now, sample):
        if self.sky_landing_target or not sample.sky_ascent_key:
            return TravelCommand(outcome="skyriding_landing_blocked")
        x, y = self.router.metric(sample.position)
        tx, ty = self.router.metric(self.destination)
        length = math.hypot(tx - x, ty - y)
        side_x, side_y = (-(ty - y) / length, (tx - x) / length) if length > 1 else (1, 0)
        width, height = self.router.width, self.router.height
        for side in (1, -1):
            point = ((x + side_x * side * 30) / width, (y + side_y * side * 30) / height)
            if all(0 <= coordinate <= 1 for coordinate in point):
                self.sky_landing_target = point
                self.enter("sky_recover", now)
                return TravelCommand(tap=sample.sky_ascent_key)
        return TravelCommand(outcome="skyriding_landing_blocked")

    def turn_error(self, position, target, heading):
        x, y = self.router.metric(position)
        tx, ty = self.router.metric(target)
        return heading_error(heading, compass_from_delta(tx - x, ty - y, self.convention))

    def turn_keys(self, turn, deadband):
        if abs(turn) <= deadband:
            return frozenset()
        left = (turn > 0) == (self.convention == "ccw_west")
        return frozenset({"left" if left else "right"})

    def steer(self, now, position, heading, height, descent_key):
        if self.mode == "fly" and not self.path:
            self.path = [self.destination]
        if not self.path:
            return TravelCommand(outcome="route_blocked")
        while len(self.path) > 1 and self.router.distance(position, self.path[0]) <= 3:
            self.path.pop(0)
        target = self.path[0]
        turn = self.turn_error(position, target, heading)
        keys = set(self.turn_keys(turn, math.radians(5)))
        if abs(turn) < math.radians(25):
            keys.add("w")
        if self.mode == "fly":
            if height is None:
                return TravelCommand(outcome="flight_height_unavailable")
            if height < self.altitude - 3:
                keys.add("space")
            elif height > self.altitude + 3 and descent_key:
                keys.add(descent_key)
        if ("w" not in keys or self.motion_position is None
                or self.router.distance(position, self.motion_position) > 1.2):
            self.motion_position, self.motion_at = position, now
        elif now - self.motion_at > 3:
            if self.mode == "fly":
                if self.climbs >= 3:
                    return TravelCommand(outcome="flight_route_blocked")
                self.climbs += 1
                self.altitude += 12
                self.enter("climb", now)
                return TravelCommand(frozenset({"space"}))
            if self.detours >= 5:
                return TravelCommand(outcome="route_blocked")
            self.detours += 1
            self.router.block_ahead(position, target)
            self.enter("recover", now)
            return TravelCommand(frozenset({"s"}))
        return TravelCommand(frozenset(keys))
