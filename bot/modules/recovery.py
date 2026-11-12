"""Recover the player and verify class preparation before resuming a quest."""
import math

from ..control.quest_travel import QuestTrip
from ..perception.travel_channel import LifeState, PetState, TravelFlags


class QuestRecovery:
    def __init__(self, now):
        self.started = self.life_at = self.frame_at = now
        self.life = None
        self.sequence = None
        self.trip = None
        self.map_id = None
        self.arrived_at = None
        self.hover = None
        self.clicked_at = float("-inf")
        self.clicks = 0
        self.action_until = 0.0
        self.prep_action = None
        self.prep_attempts = {}

    def tick(self, ctx, now, nav, heading, set_keys, window_bounds):
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
            self.trip = None
            self.arrived_at = None
            self.prep_attempts.clear()
            self.prep_action = None
            self.action_until = now
            ctx.log_event("quest_recovery_state", {"life": self.life.name, "map": nav.map_id})

        if self.life == LifeState.ALIVE:
            set_keys(frozenset())
            return self.prepare(ctx, now, nav)
        if sample.dialog or self.life == LifeState.DEAD or now - self.clicked_at < 4:
            set_keys(frozenset())
            if sample.dialog:
                return self.confirm(ctx, now, sample, window_bounds)
            self.hover = None
            if now - self.life_at > 15 and self.life == LifeState.DEAD:
                return "release_spirit_unavailable"
            return
        self.hover = None
        if not (sample.corpse and nav.position and nav.world_size and heading is not None
                and nav.flags & TravelFlags.KEYS_VERIFIED):
            set_keys(frozenset())
            if now - self.life_at >= 15:
                return "corpse_route_unavailable"
            return
        if self.trip is None or self.map_id != nav.map_id:
            self.trip = QuestTrip(nav.world_size, sample.corpse, ctx.cfg.navigation.facing_convention,
                                  arrival_radius=2, walk_only=True)
            self.map_id = nav.map_id
            self.arrived_at = None
            ctx.log_event("quest_recovery_route", {"map": nav.map_id, "corpse": sample.corpse})
        command = self.trip.step(now, nav, heading)
        set_keys(command.keys)
        if command.tap:
            if command.hold_s is None:
                ctx.input.tap(command.tap)
            else:
                ctx.input.tap(command.tap, hold_s=command.hold_s)
        if command.outcome == "arrived":
            if self.arrived_at is None:
                self.arrived_at = now
            elif now - self.arrived_at >= 10:
                return "corpse_confirmation_unavailable"
        elif command.outcome:
            return command.outcome
        else:
            self.arrived_at = None

    def confirm(self, ctx, now, sample, window_bounds):
        # A visible disabled popup can be waiting for the game's corpse timer.
        if sample.button is None:
            self.hover = None
            return
        if now - self.clicked_at < 4:
            return
        if self.clicks >= 2:
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
        if self.hover is None or math.dist(self.hover[0], point) > 3:
            ctx.input.move_to(*point, jitter_px=0)
            self.hover = point, now
            return
        if not sample.hovered:
            if now - self.hover[1] >= 3:
                return "dialog_hover_unverified"
            return
        ctx.input.click()
        self.clicks += 1
        self.clicked_at = now
        self.hover = None
        ctx.log_event("quest_recovery_click", {"life": self.life.name, "attempt": self.clicks})

    def prepare(self, ctx, now, nav):
        if now - self.life_at >= 45:
            return "class_preparation_timeout"
        sample, tried = nav.recovery, self.prep_attempts
        pet_confirmed = self.prep_action in {"summon", "revive"} and sample.pet == PetState.ALIVE
        buff_confirmed = self.prep_action == "buff" and not (sample.buff_missing or sample.buff_unverified)
        if (now - self.life_at < .6
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
                if sample.revive and not tried.get("revive"):
                    return self.action(ctx, now, "revive", "f11", 6)
                return "pet_revive_unconfirmed"
            elif sample.pet == PetState.ABSENT:
                if sample.summon and not tried.get("summon"):
                    return self.action(ctx, now, "summon", "f10", 7)
                if sample.revive and not tried.get("revive"):
                    return self.action(ctx, now, "revive", "f11", 6)
                if sample.summon and tried.get("revive") and tried.get("summon", 0) < 2:
                    return self.action(ctx, now, "summon", "f10", 7)
                return "pet_summon_unconfirmed"
            else:
                return
        if sample.buff_unverified:
            return  # Aura access may be restricted for a short period after combat.
        if sample.buff_missing:
            if tried.get("buff", 0) >= 2:
                return "buff_unconfirmed"
            return self.action(ctx, now, "buff", "f12", 3)
        return "done"

    def action(self, ctx, now, action, key, wait):
        self.prep_attempts[action] = self.prep_attempts.get(action, 0) + 1
        self.prep_action = action
        self.action_until = now + wait
        ctx.input.tap(key)
        ctx.log_event("quest_recovery_prepare", {"action": action, "attempt": self.prep_attempts[action]})
