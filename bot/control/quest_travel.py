"""Closed-loop travel using verified flight, mount and movement bindings."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..perception.travel_channel import TravelFlags as Flags, TravelSample
from .navigation import compass_from_delta, heading_error
from .pathfinding import GridRouter
from .approach import ApproachAttempt, ApproachGoal, ApproachObservation, ActionState, ActionResult

FINAL_APPROACH_YARDS = 32
GROUND_MOUNT_YARDS = 24
LOCAL_FLIGHT_YARDS = 60
INTERACT_ON_FOOT_YARDS = 10
TURN_INTERVAL = .45  # Same pulse rhythm as combat turning: tap, release, observe.
# Initial keyboard turn speed estimate. Bound each pulse because clients can differ.
TURN_RATE = math.radians(240)
FORWARD_TOLERANCE = math.radians(30)  # Keep walking through small heading errors.


@dataclass(frozen=True)
class TravelCommand:
    keys: frozenset[str] = frozenset()
    tap: str | None = None
    outcome: str | None = None
    hold_s: float | None = None
    movement_result: ActionResult | None = None
    camera_yaw_px: int | None = None  # bounded stationary calibration orbit
    precise: bool = False


class QuestTrip:
    def __init__(self, world_size, destination, convention, *, arrival_radius=4, walk_only=False,
                 local_travel=False, approach_on_foot=False, prefer_steady=False, log_event=None,
                 flight_threshold=FINAL_APPROACH_YARDS):
        self.destination, self.convention = destination, convention
        self.arrival_radius = arrival_radius
        self.walk_only = walk_only
        self.local_travel = local_travel
        self.approach_on_foot = approach_on_foot
        self.flight_threshold = flight_threshold
        self.router = GridRouter(*world_size)
        self.mode = None
        self.phase, self.phase_at = "choose", 0.0
        self.failed_modes: set[str] = set()
        self.path = []
        self.motion_position, self.motion_at = None, 0.0
        self.motion_goal = None
        self.altitude = None
        self.climbs = self.detours = 0
        self.stall_position = None
        self.stall_count = 0
        self.style_attempted = False
        self.prefer_steady = prefer_steady
        self.style_seen_cast = False
        self.sky_started = None
        self.sky_budget = 60
        self.sky_launch_position = None
        self.sky_launch_height = None
        self.sky_launch_repeated = False
        self.sky_relaunches = 0
        self.sky_height_sample = None
        self.sky_sinking = False
        self.sky_action_at = float("-inf")
        self.sky_pending_cast = None
        self.sky_unconfirmed = set()
        self.sky_ascent_misses = 0
        self.sky_airborne_controls = None
        self.log_event = log_event
        self.sky_landing_target = None
        self.sky_land_height = None
        self.precision_sequence = None
        self.precision_attempt = None
        self.turn_at = float("-inf")
        self.turn_observe_after = float("-inf")
        self.turn_sequence = None
        self.turn_direction = None
        self.turn_feedback = None

    def enter(self, phase, now):
        self.phase, self.phase_at = phase, now
        self.motion_position = None
        self.turn_feedback = None
        self.precision_attempt = None

    def fail_mode(self, now):
        self.failed_modes.add(self.mode)
        self.enter("choose", now)
        return TravelCommand()

    def dismount(self, now, sample):
        self.enter("dismount", now)
        return TravelCommand(tap=sample.sky_dismount_key or "8")

    def step(self, now: float, sample: TravelSample, heading: float, *,
             walk_verified: bool | None = None, heading_age: float = 0.0,
             retreating: bool = False) -> TravelCommand:
        flags, position = sample.flags, sample.position
        descent_key = sample.descent_key
        sky_controls = bool(sample.sky_pitch_key and sample.sky_dismount_key)
        mounted, flying = bool(flags & Flags.MOUNTED), bool(flags & Flags.FLYING)
        can_fly = (
            bool(flags & Flags.CAN_FLY) and "fly" not in self.failed_modes
            and (sky_controls or (descent_key is not None and sample.height is not None))
        )
        can_ride = bool(mounted or flags & Flags.CAN_MOUNT) and "mount" not in self.failed_modes
        keys_verified = (walk_verified if self.walk_only and walk_verified is not None
                         else bool(flags & Flags.KEYS_VERIFIED))
        if not keys_verified or position is None:
            return TravelCommand(outcome="travel_bindings_or_position_unavailable")
        distance = self.router.distance(position, self.destination)
        ground_available = bool(flags & Flags.CAN_MOUNT) and "mount" not in self.failed_modes
        using_ground_mount = mounted and self.mode == "mount"
        flight_yards = (LOCAL_FLIGHT_YARDS if self.local_travel and (ground_available or using_ground_mount)
                        else self.flight_threshold)
        can_fly = can_fly and not self.walk_only and distance > flight_yards
        can_ride = can_ride and not self.walk_only and (mounted or distance >= GROUND_MOUNT_YARDS)
        if flags & Flags.SWIMMING or (flags & Flags.IN_COMBAT and not retreating):
            return TravelCommand(outcome="travel_state_unsafe")
        if retreating and (not self.walk_only or mounted or flying or flags & Flags.FALLING):
            return TravelCommand(outcome="travel_state_unsafe")
        can_launch_mounted = (mounted and distance > flight_yards and not self.walk_only
                              and "fly" not in self.failed_modes and not self.prefer_steady)
        if self.mode == "skyriding" or (flags & Flags.SKYRIDING and sky_controls and (flying or can_launch_mounted)):
            return self.sky_step(now, sample, heading, mounted, flying)
        if descent_key is None:
            if flying:
                return TravelCommand(outcome="descent_binding_unavailable")
            if self.phase == "takeoff":
                return self.fail_mode(now)
        if flying and flags & Flags.SKYRIDING:
            return TravelCommand(outcome="steady_flight_not_confirmed")
        if self.phase == "recover_turn":
            if flags & Flags.FALLING:
                return TravelCommand()
            self.enter("recover_jump", now)
            return TravelCommand(frozenset({"w"}), tap="space")
        if self.phase.startswith("recover_"):
            recovery_key = {"recover_jump": "w", "recover_strafe_left": "q",
                            "recover_strafe_right": "e", "recover_backtrack": "s"}[self.phase]
            if now - self.phase_at < .60 and (recovery_key not in {"q", "e"} or sample.strafe_verified):
                return TravelCommand(frozenset({recovery_key}))
            self.enter("choose", now)
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
                return self.dismount(now, sample)
            return TravelCommand(outcome="arrived")
        if (mounted and not flying and (self.approach_on_foot or flags & Flags.SKYRIDING)
                and distance <= INTERACT_ON_FOOT_YARDS):
            return self.dismount(now, sample)
        if self.phase == "style":
            if flags & Flags.CASTING:
                self.style_seen_cast = True
                return self.fail_mode(now) if elapsed > 9 else TravelCommand()
            if flags & Flags.SKYRIDING or (not mounted and not self.style_seen_cast and elapsed < 6):
                return self.fail_mode(now) if elapsed > 9 else TravelCommand()
            self.enter("choose", now)
        if self.phase == "mount":
            if not mounted:
                return self.fail_mode(now) if elapsed > 5 else TravelCommand()
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
                if (self.prefer_steady and mounted and flags & Flags.SKYRIDING
                        and descent_key and flags & Flags.STYLE_SWITCH and not self.style_attempted):
                    return self.dismount(now, sample)
                if self.prefer_steady and not mounted and descent_key and flags & Flags.STYLE_SWITCH \
                        and not self.style_attempted:
                    self.style_attempted = True
                    self.enter("style", now)
                    return TravelCommand(tap="5")
                if not mounted:
                    self.enter("mount", now)
                    return TravelCommand(tap="6")
                if not sample.flight_mount_active:
                    self.enter("change_mount", now)
                    return TravelCommand(tap=sample.sky_dismount_key or "8")
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
                if mounted:
                    return self.dismount(now, sample)
                self.mode = "walk"
            self.path = [self.destination] if self.mode == "fly" else self.router.route(position, self.destination)
            self.enter("cruise", now)

        if self.mode == "fly" and not flying:
            return self.fail_mode(now)
        if self.mode == "mount" and not mounted:
            return self.fail_mode(now)
        return self.steer(now, position, heading, sample.height, descent_key, sample.sequence,
                          sample.strafe_verified, heading_age)

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
        if sample.flags & Flags.FALLING and not flying and self.phase != "sky_launch":
            return TravelCommand()
        if self.mode != "skyriding":
            self.mode = "skyriding"
            self.sky_started = now
            self.sky_budget = min(240, max(60, distance / 18 + 25))
            self.enter("sky_glide" if flying else "sky_align", now)
            flight = sample.sky_flight
            if flight and self.log_event:
                self.log_event("quest_skyriding_ready", {
                    "charges": flight.charges, "speed": flight.speed, "surge": flight.surge_key,
                    "second_wind": flight.second_wind_key, "whirling": flight.whirling_key,
                    "ascent": sample.sky_ascent_key, "halt": sample.sky_halt_key,
                    "distance_yards": round(distance, 1),
                })
        elapsed = now - self.phase_at
        if now - self.sky_started > self.sky_budget:
            return TravelCommand(outcome="skyriding_timeout")

        self.check_sky_cast(now, sample)
        if not flying and self.phase == "sky_glide" and distance > 180:
            moved = (self.router.distance(self.sky_launch_position, sample.position)
                     if self.sky_launch_position is not None else 0)
            relaunch = (self.sky_relaunches == 0 and moved >= 25 and sample.sky_flight is not None
                        and self.sky_ascent_misses == 0 and sample.sky_ascent_key is not None)
            steady_flags = Flags.STYLE_SWITCH | Flags.CAN_FLY
            steady = bool(sample.descent_key and sample.height is not None and sample.flags & steady_flags == steady_flags)
            if self.log_event:
                self.log_event("quest_skyriding_early_landing", {
                    "distance_yards": round(distance, 1), "moved_yards": round(moved, 1),
                    "ascent_misses": self.sky_ascent_misses,
                    "next": "relaunch" if relaunch else "steady" if steady else "ground",
                })
            if relaunch:
                self.sky_relaunches += 1
                self.enter("sky_align", now)
                elapsed = 0
            elif steady:
                self.prefer_steady = True
                self.mode = None
                return self.dismount(now, sample)
        if not flying and (
            distance <= self.flight_threshold
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
            launch_key = "space" if sample.sky_flight is not None else sample.sky_ascent_key
            if steering or not launch_key:
                return TravelCommand(steering)
            self.sky_launch_position, self.sky_launch_height = sample.position, sample.height
            self.sky_height_sample = self.sky_airborne_controls = None
            self.sky_sinking = False
            self.sky_launch_repeated = False
            self.enter("sky_launch", now)
            return TravelCommand(tap=launch_key)
        if self.phase == "sky_launch":
            if not flying:
                if (sample.sky_flight is not None and not self.sky_launch_repeated
                        and .35 <= elapsed <= 1.2):
                    self.sky_launch_repeated = True
                    return TravelCommand(tap="space")
                if elapsed <= 3:
                    return TravelCommand()
                if sample.flags & Flags.FALLING:
                    return TravelCommand()
                self.enter("sky_dismount", now)
                return TravelCommand(tap=sample.sky_dismount_key)
            self.enter("sky_glide", now)
        if self.phase == "sky_recover":
            if elapsed < .6:
                return TravelCommand()
            self.enter("sky_glide", now)
        if self.phase == "sky_glide":
            brake_distance = 14 if self.sky_landing_target else self.flight_threshold
            flight = sample.sky_flight
            if flight:
                controls = (flight.surge_key, flight.second_wind_key, flight.whirling_key,
                            sample.sky_ascent_key, sample.sky_halt_key)
                previous = self.sky_airborne_controls
                if previous is None or any(key and key != old for key, old in zip(controls, previous)):
                    self.sky_airborne_controls = (tuple(key or old for key, old in zip(controls, previous))
                                                  if previous else controls)
                    if self.log_event:
                        self.log_event("quest_skyriding_airborne_controls", dict(zip(
                            ("surge", "second_wind", "whirling", "ascent", "halt"), controls)))
            if flight and flight.speed is not None:
                brake_distance = max(brake_distance, 16 + flight.speed * 1.25)
            if distance > brake_distance:
                command = self.sky_cruise(now, sample, distance, turn)
                if command:
                    return command
                return TravelCommand(steering)
            self.enter("sky_brake", now)
            if sample.sky_halt_key:
                return self.cast_sky(now, sample, "halt", sample.sky_halt_key, distance)
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

    def check_sky_cast(self, now, sample):
        if self.sky_pending_cast is None or sample.sky_flight is None:
            return
        action, epoch, sequence, sent_at, previous_charges = self.sky_pending_cast
        flight = sample.sky_flight
        fresh = sample.sequence != sequence
        charge_delta = (flight.charges - previous_charges
                        if flight.charges is not None and previous_charges is not None else None)
        spell_event = fresh and flight.cast_epoch != epoch and flight.cast == action
        charge_spent = fresh and action in {"ascent", "surge"} and charge_delta == -1
        confirmed = spell_event or charge_spent
        replenished = action != "second_wind" or (charge_delta is not None and charge_delta > 0)
        if not (confirmed and replenished) and now - sent_at < 2:
            return
        self.sky_pending_cast = None
        if not confirmed or not replenished:
            if action == "ascent":
                self.sky_ascent_misses += 1
            if action != "ascent" or self.sky_ascent_misses >= 2:
                self.sky_unconfirmed.add(action)
        elif action == "ascent":
            self.sky_ascent_misses = 0
        if self.log_event:
            self.log_event("quest_skyriding_cast_result", {
                "action": action, "confirmed": confirmed, "charges": flight.charges, "speed": flight.speed,
                "charge_delta": charge_delta, "spell_event": spell_event,
            })

    def cast_sky(self, now, sample, action, key, distance):
        flight = sample.sky_flight
        if flight:
            if (self.sky_pending_cast is not None and action != "halt") or action in self.sky_unconfirmed:
                return TravelCommand()
            self.sky_pending_cast = action, flight.cast_epoch, sample.sequence, now, flight.charges
            self.sky_action_at = now
            if self.log_event:
                self.log_event("quest_skyriding_cast_request", {
                    "action": action, "key": key, "charges": flight.charges, "speed": flight.speed,
                    "distance_yards": round(distance, 1),
                })
        return TravelCommand(tap=key)

    def sky_cruise(self, now, sample, distance, turn):
        flight = sample.sky_flight
        if sample.height is not None:
            if self.sky_height_sample is None:
                self.sky_height_sample = now, sample.height
            elif now - self.sky_height_sample[0] >= 1.2:
                self.sky_sinking = self.sky_height_sample[1] - sample.height >= 3
                self.sky_height_sample = now, sample.height
        if (flight is None or flight.charges is None or self.sky_landing_target
                or distance < 140 or abs(turn) > math.radians(8) or sample.flags & Flags.CASTING
                or self.sky_pending_cast is not None or now - self.sky_action_at < 1.5
                or now - self.phase_at < .4):
            return
        if flight.charges <= 1 and distance >= 180 and "second_wind" not in self.sky_unconfirmed:
            if flight.second_wind_key:
                return self.cast_sky(now, sample, "second_wind", flight.second_wind_key, distance)
        if flight.charges <= 1:
            return  # Keep the last charge for a necessary climb or landing recovery.
        low = (self.sky_launch_height is not None and sample.height is not None
               and sample.height < self.sky_launch_height + 14)
        if (distance >= 220 and (low or self.sky_sinking) and sample.sky_ascent_key
                and "ascent" not in self.sky_unconfirmed
                and now - self.phase_at >= 1.0 and now - self.sky_action_at >= 2.5):
            return self.cast_sky(now, sample, "ascent", sample.sky_ascent_key, distance)
        if flight.speed is None or flight.speed >= 32:
            return
        if distance >= 400 and flight.whirling_key and "whirling" not in self.sky_unconfirmed:
            return self.cast_sky(now, sample, "whirling", flight.whirling_key, distance)
        if flight.surge_key and "surge" not in self.sky_unconfirmed:
            return self.cast_sky(now, sample, "surge", flight.surge_key, distance)

    def recover_sky_landing(self, now, sample):
        if (self.sky_landing_target or not sample.sky_ascent_key
                or self.sky_pending_cast is not None or "ascent" in self.sky_unconfirmed):
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
                return self.cast_sky(now, sample, "ascent", sample.sky_ascent_key, length)
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

    def steer(self, now, position, heading, height, descent_key, sequence, strafe_verified=False,
              heading_age=0.0):
        if self.mode == "fly" and not self.path:
            self.path = [self.destination]
        if not self.path:
            return TravelCommand(outcome="route_blocked")
        while len(self.path) > 1 and self.router.distance(position, self.path[0]) <= 3:
            self.path.pop(0)
        target = self.path[0]
        turn = self.turn_error(position, target, heading)
        remaining = self.router.distance(position, self.destination)
        precision = self.mode == "walk" and self.arrival_radius <= 5 and len(self.path) == 1 and remaining <= 8
        identity = ("waypoint", self.destination)
        observation = ApproachObservation(identity, sequence, remaining <= self.arrival_radius,
                                          True, position, (self.router.width, self.router.height))
        if self.precision_attempt is not None:
            result = self.precision_attempt.poll(now, observation)
            if result.state == ActionState.WAITING:
                return TravelCommand()
            if self.log_event:
                self.log_event("quest_approach_result", {"kind": "waypoint", "reason": result.reason,
                    "moved_yards": result.moved_yards, "position": position, "sequence": sequence})
            self.precision_attempt = None
            if result.state != ActionState.CONFIRMED:
                if result.reason in {"movement_evidence_missing", "displacement_unexpected", "clock_changed"}:
                    return TravelCommand(outcome="travel_position_invalid", movement_result=result)
                return TravelCommand(outcome="travel_telemetry_stale" if result.reason == "feedback_stale"
                                     else "route_blocked", movement_result=result)
        if precision and sequence == self.precision_sequence:
            return TravelCommand()
        if precision:
            self.precision_sequence = sequence
        turn_tap = None
        if self.mode == "fly":
            keys = set(self.turn_keys(turn, math.radians(5)))
            if abs(turn) <= math.radians(25):
                keys.add("w")
            if height is None:
                return TravelCommand(outcome="flight_height_unavailable")
            if height < self.altitude - 3:
                keys.add("space")
            elif height > self.altitude + 3 and descent_key:
                keys.add(descent_key)
        else:
            # Ground steering pulses the turn key and releases it before the
            # next decision. A held key keeps turning while heading telemetry
            # lags, overshoots and starts a reverse-correction oscillation.
            if heading_age > .75:
                self.motion_position = None
                return TravelCommand()
            forward = abs(turn) <= (math.radians(8) if precision else FORWARD_TOLERANCE)
            keys = {"w"} if forward else set()
            direction = next(iter(self.turn_keys(turn, math.radians(8 if precision else 12))), None)
            if direction and abs(turn) >= math.radians(170) and self.turn_direction:
                direction = self.turn_direction  # Avoid switching sides across ±180°.
            interval = .9 if self.turn_direction and direction != self.turn_direction else TURN_INTERVAL
            heading_settled = now - heading_age >= self.turn_observe_after
            if not heading_settled and precision:
                return TravelCommand()
            if (direction and heading_settled and now - self.turn_at >= interval
                    and self.turn_sequence != sequence):
                turn_tap = direction
                if self.turn_feedback is None or abs(heading_error(self.turn_feedback[1], heading)) >= math.radians(4):
                    self.turn_feedback = now, heading
                elif now - self.turn_feedback[0] >= 2.5:
                    return TravelCommand(outcome="heading_turn_unconfirmed")
            elif direction is None:
                self.turn_feedback = None
        progress = False
        if self.motion_position is not None:
            if self.mode == "fly":
                progress = self.router.distance(position, self.motion_position) > 1.2
            else:
                progress = self.router.distance(self.motion_position, target) - self.router.distance(position, target) > 1.2
        if "w" not in keys or self.motion_position is None or target != self.motion_goal or progress:
            self.motion_position, self.motion_at = position, now
            self.motion_goal = target
        elif now - self.motion_at > 3:
            if self.mode == "fly":
                if self.climbs >= 3:
                    return TravelCommand(outcome="flight_route_blocked")
                self.climbs += 1
                self.altitude += 12
                self.enter("climb", now)
                return TravelCommand(frozenset({"space"}))
            if self.stall_position is None or self.router.distance(self.stall_position, position) > 9:
                self.stall_position, self.stall_count = position, 1
            else:
                self.stall_count += 1
            actions = ["jump", "turn"]
            if strafe_verified:
                actions.extend(("strafe_left", "strafe_right"))
            actions.append("backtrack")
            if self.detours >= len(actions):
                return TravelCommand(outcome="route_blocked")
            action = actions[self.detours]
            self.detours += 1
            if action == "backtrack":
                # Retreat distance and rear clearance belong to TerrainDetour.
                # Do not spend another blind S pulse pressed against this contact.
                self.router.block_ahead(position, target)
                return TravelCommand(outcome="route_blocked")
            self.enter("recover_" + action, now)
            if action == "jump":
                return TravelCommand(frozenset({"w"}), tap="space")
            if action == "turn":
                return TravelCommand(tap="left" if turn >= 0 else "right", hold_s=.20)
            if action.startswith("strafe"):
                return TravelCommand(frozenset({"q" if action == "strafe_left" else "e"}))
        if turn_tap is not None:
            hold = max(.03, min(.16, abs(turn) / TURN_RATE))
            self.turn_at, self.turn_sequence, self.turn_direction = now, sequence, turn_tap
            self.turn_observe_after = now + hold + .08
            return TravelCommand(frozenset(keys), tap=turn_tap, hold_s=hold)
        if precision and keys:
            hold = min(.16, max(.055, (remaining - self.arrival_radius) / 8))
            self.precision_attempt = ApproachAttempt(ApproachGoal("waypoint", identity, pulse_s=hold),
                                                       now, observation)
            if self.log_event:
                self.log_event("quest_approach_issued", {"kind": "waypoint", "position": position,
                    "destination": self.destination, "sequence": sequence, "hold_s": hold})
            return TravelCommand(tap="w", hold_s=hold)
        return TravelCommand(frozenset(keys))
