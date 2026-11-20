"""Recover the player and verify class preparation before resuming a quest."""
import math

from ..control.input_helper import HelperActionRejected
from ..control.quest_travel import QuestTrip
from ..control.navigation import heading_error
from ..control.pathfinding import GridRouter
from ..perception.capture import CaptureError, ScreenCapture, frontmost_game_bounds, game_ui_bounds
from ..perception.cv import minimap_corpse_pin, minimap_corpse_bearing, player_bar_percent
from ..perception.nav3d_channel import nav3d_region
from ..perception.road_map import ROAD_STRIP_RAISED_BUILD, road_region
from ..perception.travel_channel import LifeState, PetState, SupplyAction, TaskFlags, TravelFlags, travel_region


class CorpseDistanceSearch:
    """Estimate a nearby corpse from three same-map range readings."""

    def __init__(self, now, nav, heading, convention):
        self.started, self.map_id = now, nav.map_id
        self.width, self.height = nav.world_size
        self.origin = nav.position
        self.readings = [(nav.position, nav.recovery.corpse_distance)]
        self.probes = []
        self.estimate = None
        self.progress = None
        # Two short, perpendicular samples, initially using the current heading.
        east = math.sin(heading) * (-1 if convention == "ccw_west" else 1)
        south = -math.cos(heading)
        for dx, dy in ((east, south), (-south, east)):
            for side in (1, -1):
                point = (self.origin[0] + side * 9 * dx / self.width,
                         self.origin[1] + side * 9 * dy / self.height)
                if all(0 <= coordinate <= 1 for coordinate in point):
                    self.probes.append(point)
                    break
        if len(self.probes) != 2:
            self.probes.clear()

    def target(self, now, nav):
        if self.estimate is None:
            return self.probes[0] if self.probes and now - self.started < 26 else None
        remaining = math.hypot((nav.position[0] - self.estimate[0]) * self.width,
                               (nav.position[1] - self.estimate[1]) * self.height)
        distance = nav.recovery.corpse_distance
        if distance is None:
            return self.estimate
        if self.progress is None:
            self.progress = remaining, distance
        elif self.progress[0] - remaining >= 6:
            if self.progress[1] - distance < 1:
                return None
            self.progress = remaining, distance
        return self.estimate

    def observe(self, nav):
        if nav.recovery.corpse_distance is None:
            return False
        self.readings.append((nav.position, nav.recovery.corpse_distance))
        self.probes.pop(0)
        if self.probes:
            return True
        (origin, r0), (first, r1), (second, r2) = self.readings
        ax, ay = (first[0] - origin[0]) * self.width, (first[1] - origin[1]) * self.height
        bx, by = (second[0] - origin[0]) * self.width, (second[1] - origin[1]) * self.height
        determinant = ax * by - ay * bx
        if abs(determinant) < 25:
            return True
        a = (r0 * r0 - r1 * r1 + ax * ax + ay * ay) / 2
        b = (r0 * r0 - r2 * r2 + bx * bx + by * by) / 2
        x, y = (a * by - ay * b) / determinant, (ax * b - a * bx) / determinant
        point = origin[0] + x / self.width, origin[1] + y / self.height
        if abs(math.hypot(x, y) - r0) <= max(5, r0 * .10) and all(0 <= v <= 1 for v in point):
            self.estimate = point
        return True


class CorpseMinimapTracker:
    def __init__(self, nav):
        self.map_id = nav.map_id
        self.router = GridRouter(*nav.world_size)
        self.next_read = 0.0
        self.pending = self.confirmed = None
        self.last_seen = 0.0
        self.last_position = nav.position
        self.kind = self.confirmed_kind = None
        self.pending_bearing = self.bearing = None
        self.next_diagnostic = 0.0

    def update(self, ctx, now, nav, heading, window_bounds):
        if now >= self.next_read:
            self.next_read = now + .30
            point = self.read(ctx, nav, heading, window_bounds)
            if point is not None and self.kind == "minimap_arrow":
                self.pending = None
                direction = self.bearing
                agreed = (self.pending_bearing is not None and
                          sum(a*b for a, b in zip(direction, self.pending_bearing)) >= .966)
                self.pending_bearing = direction
                if agreed:
                    if (self.confirmed_kind != self.kind or self.confirmed is None
                            or self.router.distance(nav.position, self.confirmed) <= 4
                            or self.router.distance(self.confirmed, point) > 7):
                        self.confirmed = point
                        ctx.log_event("quest_recovery_minimap_arrow", {"map": nav.map_id,
                            "waypoint": point, "step_yards": 12, "direction_only": True})
                    self.confirmed_kind = self.kind
                    self.last_seen, self.last_position = now, nav.position
            elif point is not None and self.kind != self.confirmed_kind:
                # Switching from bearing-only travel to a real pin requires two pin frames.
                if self.confirmed_kind == "minimap_arrow":
                    self.confirmed = None
                if self.pending is not None and self.router.distance(self.pending, point) <= 7:
                    self.confirmed, self.confirmed_kind = point, self.kind
                    self.last_seen, self.last_position = now, nav.position
                    ctx.log_event("quest_recovery_minimap_pin", {"map": nav.map_id, "position": point,
                        "yards": round(self.router.distance(nav.position, point), 1)})
                self.pending = point
                self.pending_bearing = None
            elif self.confirmed is not None:
                if point is not None and self.router.distance(self.confirmed, point) <= 7:
                    self.last_seen, self.last_position = now, nav.position
                elif point is not None:
                    if self.pending is not None and self.router.distance(self.pending, point) <= 7:
                        self.confirmed = point
                        self.last_seen, self.last_position = now, nav.position
                    self.pending = point
                elif now - self.last_seen > 1.5 or self.router.distance(self.last_position, nav.position) > 10:
                    self.confirmed = self.pending = None
            elif point is not None and self.pending is not None and self.router.distance(self.pending, point) <= 7:
                self.confirmed = point
                self.last_seen, self.last_position = now, nav.position
                ctx.log_event("quest_recovery_minimap_pin", {"map": nav.map_id, "position": point,
                    "yards": round(self.router.distance(nav.position, point), 1)})
            else:
                self.pending = point
            if point is None:
                self.pending_bearing = self.pending = None
                if now >= self.next_diagnostic:
                    self.next_diagnostic = now + 5
                    ctx.log_event("quest_recovery_minimap_missing", {"map": nav.map_id,
                        "reason": "no_unique_tombstone_or_direction_arrow"})
        if (self.confirmed is not None and now - self.last_seen <= 1.5
                and self.router.distance(self.last_position, nav.position) <= 10):
            return self.confirmed

    def read(self, ctx, nav, heading, window_bounds):
        minimap = nav.minimap
        if minimap is None:
            return None
        cx, cy = minimap.center
        mw, mh = minimap.size
        if not (0 <= cx - mw / 2 < cx + mw / 2 <= 1 and 0 <= cy - mh / 2 < cy + mh / 2 <= 1):
            return None
        try:
            x, y, width, height = game_ui_bounds(window_bounds(), minimap)
            rect = (round(x + (cx - mw / 2) * width), round(y + (cy - mh / 2) * height),
                    round(mw * width), round(mh * height))
            frame = ctx.capture.grab(*rect)
            offset = minimap_corpse_pin(frame)
            self.kind = "minimap_pin"
            if offset is None:
                offset = minimap_corpse_bearing(frame)
                self.kind = "minimap_arrow"
        except (CaptureError, ValueError):
            return None
        if offset is None:
            return None
        theta = heading if minimap.rotates else 0
        if ctx.cfg.navigation.facing_convention == "cw_east":
            theta = -theta
        dx, dy = offset
        cosine, sine = math.cos(theta), math.sin(theta)
        scale = 12 if self.kind == "minimap_arrow" else minimap.radius
        east = (dx * cosine + dy * sine) * scale
        south = (-dx * sine + dy * cosine) * scale
        self.bearing = (east / scale, south / scale)
        point = nav.position[0] + east / self.router.width, nav.position[1] + south / self.router.height
        if all(0 <= coordinate <= 1 for coordinate in point):
            return point


class QuestRecovery:
    def __init__(self, now):
        self.started = self.life_at = self.frame_at = now
        self.life = None
        self.sequence = None
        self.trip = None
        self.search = None
        self.minimap_tracker = None
        self.route_source = None
        self.map_id = None
        self.arrived_at = None
        self.mesh_arrival = None
        self.route_missing = ()
        self.route_wait_at = now
        self.hover = None
        self.clicked_at = float("-inf")
        self.clicks = 0
        self.dialog_key_attempted = False
        self.preparation = None
        self.approach_heading = None
        self.near_probe = None
        self.near_steps = 0
        self.near_failed = False

    def tick(self, ctx, now, nav, heading, set_keys, window_bounds, *, ground_route=None):
        if now - self.started >= 360:
            return "timeout"
        sample = nav.recovery if nav else None
        if not sample or sample.life == LifeState.UNKNOWN:
            set_keys(frozenset())
            if now - self.frame_at >= 8:
                return "telemetry_unavailable"
            return
        if nav.sequence == self.sequence:
            if now - self.frame_at >= .75:
                set_keys(frozenset())
            if now - self.frame_at >= 3:
                return "telemetry_stale"
            return
        self.sequence, self.frame_at = nav.sequence, now
        if self.life != sample.life:
            set_keys(frozenset())
            self.life, self.life_at = sample.life, now
            self.hover, self.clicks, self.clicked_at = None, 0, float("-inf")
            self.dialog_key_attempted = False
            self.trip = None
            self.search = None
            self.minimap_tracker = None
            self.arrived_at = None
            self.mesh_arrival = None
            self.approach_heading = self.near_probe = None
            self.near_steps, self.near_failed = 0, False
            self.route_missing = ()
            self.preparation = ClassPreparation(now) if self.life == LifeState.ALIVE else None
            ctx.log_event("quest_recovery_state", {"life": self.life.name, "map": nav.map_id})

        if self.life == LifeState.ALIVE:
            set_keys(frozenset())
            return self.preparation.tick(ctx, now, nav)
        if sample.dialog or self.life == LifeState.DEAD or now - self.clicked_at < 4:
            set_keys(frozenset())
            if sample.dialog:
                return self.confirm(ctx, now, sample, window_bounds)
            self.hover = None
            if now - self.life_at > 15 and self.life == LifeState.DEAD:
                return "release_spirit_unavailable"
            return
        self.hover = None
        # V11 reports walking bindings and facing independently of the mount macros
        # and samples heading alongside the ghost position. Keep V10 readable.
        walk_verified = sample.walk_verified
        if walk_verified is None:
            walk_verified = bool(nav.flags & TravelFlags.KEYS_VERIFIED)
        else:
            heading = sample.facing
        destination = sample.corpse
        source = sample.corpse_source.name.lower()
        if self.map_id is not None and self.map_id != nav.map_id:
            self.search = self.trip = self.minimap_tracker = None
            self.arrived_at = None
            self.mesh_arrival = None
            self.approach_heading = self.near_probe = None
            self.near_steps, self.near_failed = 0, False
        self.map_id = nav.map_id
        if self.mesh_arrival is not None:
            # A completed mesh route only confirms proximity. The native
            # resurrection dialog above still owns the life-state transition.
            point, arrived_at = self.mesh_arrival
            if destination is None or destination == point:
                set_keys(frozenset())
                return "corpse_confirmation_unavailable" if now - arrived_at >= 10 else None
            self.mesh_arrival = None
        if (destination is None and nav.minimap and nav.position and nav.world_size
                and heading is not None and walk_verified):
            if self.minimap_tracker is None or self.minimap_tracker.map_id != nav.map_id:
                self.minimap_tracker = CorpseMinimapTracker(nav)
                ctx.log_event("quest_recovery_minimap_search", {"map": nav.map_id, "radius": nav.minimap.radius})
            destination = self.minimap_tracker.update(ctx, now, nav, heading, window_bounds)
            if destination is not None:
                source = self.minimap_tracker.confirmed_kind
        missing = tuple(name for name, available in (
            ("corpse_position", destination is not None or sample.corpse_distance is not None
             or self.search is not None and self.search.estimate is not None),
            ("player_position", nav.position is not None),
            ("map_scale", nav.world_size is not None),
            ("heading", heading is not None),
            ("movement_bindings", walk_verified),
        ) if not available)
        if missing:
            set_keys(frozenset())
            if not self.route_missing:
                self.route_wait_at = now
            if missing != self.route_missing:
                self.route_missing = missing
                ctx.log_event("quest_recovery_route_wait", {"map": nav.map_id, "missing": missing})
            if now - self.route_wait_at >= 15:
                return "corpse_position_unavailable" if missing == ("corpse_position",) else "corpse_route_unavailable"
            return
        self.route_missing = ()
        if ground_route and ground_route(nav):
            return
        probing = False
        if destination is None:
            if self.search is None or self.search.map_id != nav.map_id:
                self.search = CorpseDistanceSearch(now, nav, heading, ctx.cfg.navigation.facing_convention)
                ctx.log_event("quest_recovery_distance_search", {"map": nav.map_id,
                                                                "yards": sample.corpse_distance})
            destination = self.search.target(now, nav)
            if destination is None:
                set_keys(frozenset())
                return "corpse_distance_search_unconfirmed"
            probing = self.search.estimate is None
            source = "distance_probe" if probing else "distance_estimate"
        if self.near_probe is not None:
            started, distance, origin = self.near_probe
            set_keys(frozenset())
            if now - started < .65:
                return
            current = sample.corpse_distance
            moved = self.trip.router.distance(origin, nav.position)
            if current is None or current >= distance or moved > 4:
                self.near_failed = True
            ctx.log_event("quest_recovery_near_receipt", {"before": distance, "after": current,
                          "moved": round(moved, 1), "progress": not self.near_failed})
            self.near_probe = None
        if (self.trip is None or self.route_source != source
                or self.trip.router.distance(self.trip.destination, destination) > 2):
            radius = 1.5 if probing else 4 if source in ("minimap_pin", "minimap_arrow") else 2
            self.trip = QuestTrip(nav.world_size, destination, ctx.cfg.navigation.facing_convention,
                                  arrival_radius=radius, walk_only=True)
            self.route_source = source
            self.arrived_at = None
            ctx.log_event("quest_recovery_route", {"map": nav.map_id, "destination": destination, "source": source})
        heading_age = ctx.heading_age() if sample.walk_verified is None else 0.0
        if self.arrived_at is not None and not probing:
            set_keys(frozenset())
            waited = now - self.arrived_at
            if waited >= 10:
                return "corpse_confirmation_unavailable"
            if source != "minimap_arrow" and waited >= 1.2:
                self.refine_approach(ctx, now, nav, heading, heading_age)
            return
        command = self.trip.step(now, nav, heading, walk_verified=walk_verified, heading_age=heading_age)
        if not probing and source != "minimap_arrow" and ("w" in command.keys or command.tap == "w"):
            self.approach_heading = heading
        set_keys(command.keys)
        if command.tap:
            if command.hold_s is None:
                ctx.input.tap(command.tap)
            else:
                ctx.input.tap(command.tap, hold_s=command.hold_s)
        if command.outcome == "arrived":
            if probing:
                if self.search.observe(nav):
                    self.trip = None
                elif now - self.search.started >= 26:
                    return "corpse_distance_search_unconfirmed"
            else:
                self.arrived_at = now
        elif command.outcome:
            return command.outcome
        else:
            self.arrived_at = None

    def refine_approach(self, ctx, now, nav, heading, heading_age):
        distance = nav.recovery.corpse_distance
        if (self.near_failed or self.near_steps >= 3 or distance is None or not 1 <= distance <= 35
                or self.approach_heading is None or heading_age > .55):
            return
        if nav.flags & (TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING):
            return
        if abs(heading_error(heading, self.approach_heading)) > math.radians(15):
            return
        self.near_steps += 1
        self.near_probe = now, distance, nav.position
        ctx.log_event("quest_recovery_near_step", {"yards": distance, "attempt": self.near_steps})
        ctx.input.tap("w", hold_s=.16)

    def confirm(self, ctx, now, sample, window_bounds):
        # A visible disabled popup can be waiting for the game's corpse timer.
        if sample.button is None and not sample.dialog_key:
            self.hover = None
            return
        if now - self.clicked_at < 4:
            return
        if self.clicks >= 2:
            return "confirmation_unacknowledged"
        if sample.dialog_key and not self.dialog_key_attempted:
            ctx.input.tap_precise(sample.dialog_key, .05)
            self.dialog_key_attempted = True
            self.clicks += 1
            self.clicked_at = now
            self.hover = None
            ctx.log_event("quest_recovery_dialog_key", {"life": self.life.name, "key": sample.dialog_key,
                "attempt": self.clicks})
            return
        if sample.button is None:
            return "confirmation_unacknowledged"
        try:
            x, y, width, height = window_bounds()
        except ValueError:
            return "game_window_unavailable"
        ui_height = width * sample.ui_aspect
        if not 0 < ui_height <= height + 2 or height - ui_height > 64:
            return "dialog_coordinates_unverified"
        bx, by = sample.button
        point = (round(x + bx * width), round(y + height - (1 - by) * ui_height))
        needs_hover = self.hover is None or math.dist(self.hover[0], point) > 3
        if not needs_hover and not sample.hovered:
            if now - self.hover[1] >= 3:
                return "dialog_hover_unverified"
            return
        try:
            if needs_hover:
                ctx.input.move_to(*point, jitter_px=0)
            else:
                # Native popups may poll system button state. Use the helper's verified click.
                ctx.input.click_ui()
        except HelperActionRejected as exc:
            self.hover = None
            ctx.log_event("quest_recovery_dialog_input_rejected", {"life": self.life.name,
                "operation": "hover" if needs_hover else "click", "point": point, "reason": str(exc)})
            return "dialog_input_rejected"
        if needs_hover:
            self.hover = point, now
            ctx.log_event("quest_recovery_hover", {"life": self.life.name, "point": point})
            return
        self.clicks += 1
        self.clicked_at = now
        self.hover = None
        ctx.log_event("quest_recovery_click", {"life": self.life.name, "attempt": self.clicks, "point": point})


class ConsumableRecovery:
    """Use a verified inventory binding, then observe the resulting recovery."""

    def __init__(self):
        self.pending = None
        self.rest = None
        self.next_use = 0.0
        self.visual_sequence = None
        self.visual_vitals = (None, None)

    def read_bars(self, ctx, nav, sample):
        if (sample.bars is None or not isinstance(ctx.capture, ScreenCapture)
                or sample.health is not None and sample.mana is not None):
            return sample.health, sample.mana
        if self.visual_sequence == nav.sequence:
            return self.visual_vitals
        self.visual_sequence = nav.sequence
        x, y = ctx.cfg.channel.x, ctx.cfg.channel.y
        left, right, hp_y, mana_y = sample.bars
        width = right - left
        strips = (travel_region(x, y, supply_bars=True), nav3d_region(x, y, raised=True))
        if nav.task & TaskFlags.PREPARED:
            strips += (road_region(x, y, raised=(nav.addon_build or 0) >= ROAD_STRIP_RAISED_BUILD),)
        try:
            wx, wy, ww, wh = frontmost_game_bounds()
            results = [sample.health, sample.mana]
            for idx, center in enumerate((hp_y, mana_y)):
                if center is None or results[idx] is not None:
                    continue
                bx, by = x + left, y + center - 1
                if not (wx <= bx and bx + width <= wx + ww and wy <= by and by + 3 <= wy + wh):
                    continue
                if any(bx < sx + sw and bx + width > sx and by < sy + sh and by + 3 > sy
                       for sx, sy, sw, sh in strips):
                    continue
                results[idx] = player_bar_percent(ctx.capture.grab(bx, by, width, 3), mana=idx == 1)
            previous = self.visual_vitals
            self.visual_vitals = tuple(results)
            if any(native is None and current is not None and old is None
                   for native, current, old in zip((sample.health, sample.mana), results, previous)):
                ctx.log_event("quest_consumable_visible_vitals", {"health": results[0], "mana": results[1],
                    "bars": sample.bars})
            return self.visual_vitals
        except (CaptureError, ValueError):
            self.visual_vitals = sample.health, sample.mana
            return self.visual_vitals

    def tick(self, ctx, now, nav, *, in_combat, can_start, release):
        sample = nav.vitals
        if not nav.recovery or nav.recovery.life != LifeState.ALIVE:
            self.pending = self.rest = None
            return False
        health, mana = self.read_bars(ctx, nav, sample) if sample else (None, None)
        if self.pending:
            action, item, started = self.pending
            if sample and sample.action == action and sample.item_id == item and sample.result:
                ctx.log_event("quest_consumable_receipt", {
                    "item": item, "action": action.name, "result": sample.result,
                    "health": health, "mana": mana,
                })
                self.pending = None
                if sample.result == "consumed" and action != SupplyAction.POTION and not in_combat:
                    self.rest = action, now + 18
            elif now - started >= 3.5 or (in_combat and action != SupplyAction.POTION):
                ctx.log_event("quest_consumable_wait_ended", {"item": item, "combat": in_combat})
                self.pending = None
            elif action != SupplyAction.POTION:
                release()
                return True
            else:
                return False
        if self.rest:
            action, deadline = self.rest
            value = health if action == SupplyAction.FOOD else mana
            if not in_combat and now < deadline and (value is None or value < 90):
                release()
                return True
            self.rest = None
            ctx.log_event("quest_consumable_rest_ended", {
                "action": action.name, "percent": value, "combat": in_combat,
            })
        if (not can_start or not sample or sample.result or now < self.next_use
                or nav.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING
                                | TravelFlags.SWIMMING | TravelFlags.CASTING)):
            return False
        potion, food, water = sample.supplies or tuple(
            sample.item_id if sample.action == action else 0
            for action in (SupplyAction.POTION, SupplyAction.FOOD, SupplyAction.WATER)
        )
        if not in_combat and food and health is not None and health <= 65:
            action, item, key = SupplyAction.FOOD, food, "f3"
        elif not in_combat and water and mana is not None and mana <= 45:
            action, item = SupplyAction.WATER, water
            key = "f2" if (nav.addon_build or 0) >= 303 else "f3"
        elif potion and health is not None and health <= (40 if in_combat else 30):
            action, item, key = SupplyAction.POTION, potion, "f1"
        else:
            return False
        self.pending = action, item, now
        self.next_use = now + 5
        release()
        ctx.log_event("quest_consumable_requested", {
            "item": item, "action": action.name, "health": health, "mana": mana,
            "visual_health": sample.health is None and health is not None,
            "visual_mana": sample.mana is None and mana is not None,
        })
        ctx.input.tap(key)
        return True


class ClassPreparation:
    """Wait for fresh in-game confirmation of each pet or class action."""

    def __init__(self, now, *, pet_only=False):
        self.started = now
        self.pet_only = pet_only
        self.sequence = None
        self.action_until = now
        self.last_action = None
        self.attempts = {}

    def tick(self, ctx, now, nav):
        if now - self.started >= 45:
            return "class_preparation_timeout"
        if nav.sequence == self.sequence:
            return
        self.sequence = nav.sequence
        sample, tried = nav.recovery, self.attempts
        if self.pet_only and sample.pet == PetState.ALIVE and not tried:
            return "done"
        pet_confirmed = self.last_action in {"summon", "revive"} and sample.pet == PetState.ALIVE
        buff_confirmed = self.last_action == "buff" and not (sample.buff_missing or sample.buff_unverified)
        if (now - self.started < .6
                or now < self.action_until and not (pet_confirmed or buff_confirmed)
                or nav.flags & (TravelFlags.CASTING | TravelFlags.FALLING
                                | TravelFlags.FLYING | TravelFlags.MOUNTED | TravelFlags.SWIMMING)):
            return
        if sample.pet_expected:
            if sample.pet == PetState.ALIVE:
                if not tried.get("follow"):
                    if not sample.follow:
                        return "pet_follow_binding_unverified"
                    return self.action(ctx, now, "follow", "f9", .6)
            elif sample.pet == PetState.DEAD:
                if not sample.revive:
                    if self.pet_only and sample.summon and not tried.get("summon"):
                        return self.action(ctx, now, "summon", "f10", 7)
                    return "pet_revive_binding_unverified"
                if tried.get("revive", 0) < (2 if self.pet_only else 1):
                    return self.action(ctx, now, "revive", "f11", 6)
                return "pet_revive_unconfirmed"
            elif sample.pet == PetState.ABSENT:
                if sample.summon and not tried.get("summon"):
                    return self.action(ctx, now, "summon", "f10", 2.5 if self.pet_only and sample.revive else 7)
                if sample.revive and not tried.get("revive"):
                    return self.action(ctx, now, "revive", "f11", 6)
                if sample.summon and tried.get("revive") and tried.get("summon", 0) < 2:
                    return self.action(ctx, now, "summon", "f10", 7)
                return "pet_summon_unconfirmed"
            else:
                return
        if self.pet_only:
            return "done"
        if sample.buff_unverified:
            return  # Aura access may be restricted for a short period after combat.
        if sample.buff_missing:
            if tried.get("buff", 0) >= 2:
                return "buff_unconfirmed"
            return self.action(ctx, now, "buff", "f12", 3)
        return "done"

    def action(self, ctx, now, action, key, wait):
        self.attempts[action] = self.attempts.get(action, 0) + 1
        self.last_action = action
        self.action_until = now + wait
        ctx.input.tap(key)
        event = "quest_pet_prepare" if self.pet_only else "quest_recovery_prepare"
        ctx.log_event(event, {"action": action, "attempt": self.attempts[action]})
