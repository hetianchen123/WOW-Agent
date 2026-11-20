"""Bounded use of a client-verified action against one selected creature."""
from __future__ import annotations

from ..control.approach import ApproachAttempt, ApproachGoal, ApproachObservation, ActionState
from ..perception.travel_channel import TravelFlags

import math
import time

from ..control.navigation import heading_error
from ..data.combat import client_edition
from ..perception.combat_channel import CombatSample

FACING_PROBE_ANGLE = math.radians(24)
SEARCH_ARC = tuple(map(math.radians, (0, 20, 40, 60, 80, 60, 40, 20, 0, -20, -40, -60, -80)))
TURN_INTERVAL = .45
TURN_SETTLE = .50


def same_combat_character(previous, sample):
    current = sample.identity
    return (previous == current or client_edition(sample.build) == "forever"
            and previous[:-1] == current[:-1] and current[-1] > previous[-1])


def same_output_binding(previous_key, sample):
    return (previous_key == sample.key or client_edition(sample.build) == "forever" and sample.class_id == 3
            and {previous_key, sample.key} == {4, 5})


class Fight:
    def __init__(self, target_id: int, *, approach=False, defense=False, immediate=False):
        self.target_id = target_id
        self.immediate = immediate
        self.identity = None
        self.state_epoch = None
        self.key = None
        self.last_sequence = None
        self.last_sample = None
        self.last_action = float("-inf")
        self.presses = 0
        self.last_facing_error = None
        self.turn_goal: float | None = None
        self.turn_deadline = 0.0
        self.facing_probes = 0
        self.turn_at = float("-inf")
        self.last_facing_at = None
        self.approach = approach
        self.defense = defense
        self.approach_at = float("-inf")
        self.approach_deadline: float | None = None
        self.approach_attempt = None
        self.out_of_range_at = float("-inf")
        self.last_health = None
        self.cast_epoch = None
        self.pending_action_at = None
        self.no_receipts = 0
        self.last_damage_at = None
        self.last_cast_at = None
        self.first_output_at = None
        self.last_credit_at = None
        self.receipt_at = None

    def last_progress(self, sample: CombatSample, since: float) -> float:
        times = [since, self.last_damage_at, self.last_credit_at]
        if (sample.target_health is None and sample.target_engaged
                and client_edition(sample.build) == "forever"):
            # Keep a bounded fight open while verified casts are arriving and
            # HP is hidden. The native quest counter still decides credit.
            times.extend((self.last_cast_at, self.last_facing_at))
        return max(stamp for stamp in times if stamp is not None)

    def step(self, now: float, sample: CombatSample | None, navigation=None) -> tuple[str, str | None]:
        if sample is None:
            return "telemetry_missing", None
        if sample.sequence == self.last_sequence:
            if self.last_sample is not None and now - self.last_sample > .75:
                return "telemetry_stale", None
            return "waiting", None
        self.last_sequence, self.last_sample = sample.sequence, now
        if sample.unsafe:
            return "unsafe_player_state", None
        if not client_edition(sample.build) or not all(sample.identity[:4]):
            return "unknown_client_profile", None
        if self.identity is not None and not same_combat_character(self.identity, sample):
            return "character_profile_changed", None
        if sample.target_id != self.target_id:
            return "target_missing_or_changed", None
        if self.state_epoch is not None and sample.state_epoch != self.state_epoch:
            return "target_or_arm_changed", None
        if sample.target_dead:
            return "target_defeated" if self.presses else "target_already_dead", None
        if not sample.key_verified or not 1 <= sample.key <= 9:
            return "unverified_action_binding", None
        if self.key is not None and not same_output_binding(self.key, sample):
            return "action_binding_changed", None
        if self.key is not None and sample.key != self.key:
            self.pending_action_at, self.no_receipts = None, 0
            self.first_output_at = None
            self.turn_goal, self.facing_probes = None, 0
            self.last_facing_error = sample.facing_error
        starting = self.identity is None
        self.identity, self.state_epoch, self.key = sample.identity, sample.state_epoch, sample.key
        if starting:
            self.last_facing_error = sample.facing_error
            self.last_health, self.cast_epoch = sample.target_health, sample.cast_epoch
            self.receipt_at = now if sample.cast_epoch is not None else None
            if not self.immediate:
                return "waiting", None
        if self.receipt_at is not None and sample.cast_epoch is None:
            return ("telemetry_stale" if now - self.receipt_at > .75 else "waiting"), None
        if sample.cast_epoch is not None:
            self.receipt_at = now
        if sample.cast_epoch is not None and self.cast_epoch != sample.cast_epoch:
            if self.cast_epoch is not None and self.pending_action_at is not None:
                self.last_cast_at = now
                self.pending_action_at, self.no_receipts = None, 0
                if client_edition(sample.build) == "forever":
                    self.facing_probes = 0
            self.cast_epoch = sample.cast_epoch
        if sample.target_health is not None:
            if self.last_health is not None and sample.target_health < self.last_health:
                self.last_damage_at = now
                self.pending_action_at, self.no_receipts = None, 0
            self.last_health = sample.target_health
        if self.presses and sample.line_of_sight_blocked:
            self.pending_action_at = None
            return "line_of_sight_blocked", None
        if self.turn_goal is not None or (sample.facing_error is not None
                                          and sample.facing_error != self.last_facing_error):
            self.pending_action_at = None
            turn = self.correct_facing(now, sample)
            if turn is not None:
                return turn
        if sample.in_range is False:
            self.pending_action_at = None
            self.out_of_range_at = now
            if not self.approach:
                return "attack_out_of_range", None
            if self.approach_deadline is None:
                self.approach_deadline = now + 10
            if now >= self.approach_deadline:
                return "approach_timeout", None
            identity = (sample.target_id, sample.state_epoch, navigation.map_id if navigation else None)
            safe = navigation is None or (bool(navigation.flags & TravelFlags.KEYS_VERIFIED)
                and not navigation.flags & (TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
                                             | TravelFlags.MOUNTED | TravelFlags.CASTING))
            if not safe:
                return "unsafe_player_state", None
            observation = ApproachObservation(identity, navigation.sequence if navigation else sample.sequence,
                sample.in_range, safe, navigation.position if navigation else None,
                navigation.world_size if navigation else None)
            if self.approach_attempt is not None:
                result = self.approach_attempt.poll(now, observation)
                if result.state == ActionState.WAITING:
                    return "waiting", None
                if result.state != ActionState.CONFIRMED:
                    if result.reason in {"movement_evidence_missing", "displacement_unexpected", "clock_changed"}:
                        return "unsafe_player_state", None
                    return ("telemetry_stale" if result.reason == "feedback_stale" else "approach_blocked"), None
            self.approach_attempt = ApproachAttempt(ApproachGoal("combat", identity, pulse_s=.14), now, observation)
            self.approach_at = now
            return "approach", "w"
        if sample.in_range is True:
            self.approach_deadline = None
            self.approach_attempt = None
        elif now - self.out_of_range_at < .6:
            return "waiting", None
        if sample.cast_epoch is not None:
            if self.first_output_at is not None and now - self.last_progress(sample, self.first_output_at) >= 12:
                return "no_progress", None
            if self.pending_action_at is not None:
                # A verified Forever hunter melee macro queues the strike for
                # the next swing. Give that swing time to produce a receipt.
                queued_melee = client_edition(sample.build) == "forever" and sample.class_id == 3 and sample.key == 5
                if now - self.pending_action_at < (3.8 if queued_melee else 1.6):
                    return "waiting", None
                self.no_receipts += 1
                self.pending_action_at = None
                if self.no_receipts >= 3:
                    return "no_cast_or_damage_receipt", None
        if now - self.last_action < .8:
            return "waiting", None
        self.last_action = now
        self.presses += 1
        if sample.cast_epoch is not None:
            self.pending_action_at = now
            if self.first_output_at is None:
                self.first_output_at = now
        return "action", str(sample.key)

    def correct_facing(self, now: float, sample: CombatSample) -> tuple[str, str | None] | None:
        if sample.facing_error is not None:
            self.last_facing_error = sample.facing_error
        if self.turn_goal is None and not self.presses:
            return None
        if self.turn_goal is not None and (sample.facing_error is None or sample.facing is None):
            return ("waiting" if now < self.turn_deadline else "facing_turn_timeout"), None
        if sample.facing_error is None or sample.facing is None or not sample.turn_verified:
            return "facing_feedback_unavailable", None
        if self.turn_goal is None:
            forever_attacker = client_edition(sample.build) == "forever" and sample.target_engaged
            probe_budget = 9 if forever_attacker else 1
            if self.facing_probes >= probe_budget:
                return "facing_unconfirmed", None
            angle = math.radians(48) if forever_attacker else FACING_PROBE_ANGLE
            self.turn_goal = (sample.facing + angle) % math.tau
            self.facing_probes += 1
            self.turn_deadline = now + 3
        error = heading_error(sample.facing, self.turn_goal)
        if abs(error) <= math.radians(15):
            if now - self.turn_at < TURN_SETTLE:
                return "waiting", None
            self.turn_goal = None
            self.last_facing_at = now
            return None
        if now >= self.turn_deadline:
            return "facing_turn_timeout", None
        if now - self.turn_at < TURN_INTERVAL:
            return "waiting", None
        self.turn_at = now
        return "turn", "left" if error > 0 else "right"


def tap_combat_key(backend, status, key):
    if status == "turn":
        backend.tap(key, hold_s=.06)
    elif status == "search_turn":
        backend.tap(key, hold_s=.06)
    elif status == "approach":
        backend.tap(key, hold_s=.14)
    else:
        backend.tap(key)


class QuestHunt:
    """Select quest creatures at a waypoint and wait for quest credit."""

    def __init__(self, targets: frozenset[int], *, resource: bool = False, counted_drop: bool = False):
        self.targets = targets
        self.resource = resource
        self.counted_drop = counted_drop
        self.requires_loot = resource or counted_drop
        self.uncredited_kills = 0
        self.resource_best = -1
        self.fight: Fight | None = None
        self.started_at = 0.0
        self.baseline = 0
        self.observed_progress = 0
        self.credit_deadline: float | None = None
        self.search_at = float("-inf")
        self.searches = 0
        self.sequence = None
        self.frame_at = 0.0
        self.binding = None
        self.search_heading: float | None = None
        self.rejected_selection: tuple[int, int] | None = None
        self.search_turn_at = float("-inf")
        self.search_sector_at: float | None = None

    def reset_search(self, now=float("-inf")):
        self.searches, self.search_at = 0, now
        self.search_heading = self.search_sector_at = None
        self.search_turn_at = float("-inf")

    def search_target(self, now, sample):
        if now - self.search_at < 1:
            return "waiting", None
        if sample.facing is None or not sample.turn_verified:
            if self.searches >= 5:
                return "target_not_found", None
        else:
            if self.search_heading is None:
                self.search_heading = sample.facing
            if self.searches >= len(SEARCH_ARC):
                return "target_not_found", None
            if self.search_sector_at is None:
                self.search_sector_at = now
            goal = self.search_heading + SEARCH_ARC[self.searches]
            error = heading_error(sample.facing, goal)
            if abs(error) > math.radians(7):
                if now - self.search_sector_at > 4:
                    return "target_not_found", None
                if now - self.search_turn_at >= TURN_INTERVAL:
                    self.search_turn_at = now
                    return "search_turn", "left" if error > 0 else "right"
                return "waiting", None
            if now - self.search_turn_at < TURN_SETTLE:
                return "waiting", None
        self.searches += 1
        self.search_at, self.search_sector_at = now, None
        return "select_target", "tab"

    def step(
        self, now: float, sample: CombatSample | None, progress: int, in_combat: bool, verified_target: int = 0,
        navigation=None,
    ) -> tuple[str, str | None]:
        if sample is None:
            return "telemetry_missing", None
        if sample.sequence == self.sequence:
            return ("telemetry_stale" if now - self.frame_at > .75 else "waiting"), None
        self.sequence, self.frame_at = sample.sequence, now
        if sample.unsafe:
            return "unsafe_player_state", None
        if not client_edition(sample.build) or not all(sample.identity[:4]):
            return "unknown_client_profile", None
        if not sample.key_verified or not 1 <= sample.key <= 9:
            return "unverified_action_binding", None
        binding = (sample.identity, sample.key)
        if self.binding is not None and (not same_output_binding(self.binding[1], sample)
                                         or not same_combat_character(self.binding[0], sample)):
            return "action_binding_changed", None
        self.binding = binding

        if self.requires_loot and progress > self.resource_best:
            self.resource_best, self.uncredited_kills = progress, 0
        if self.credit_deadline is not None:
            if progress > self.baseline:
                self.credit_deadline, self.fight = None, None
                self.uncredited_kills = 0
                self.reset_search(now)
                return "credited", None
            if now < self.credit_deadline:
                return "waiting", None
            if self.resource and not in_combat:
                self.uncredited_kills += 1
                if self.uncredited_kills < 4:
                    self.credit_deadline, self.fight = None, None
                    self.reset_search(now)
                    return "resource_try_next", None
                return "resource_no_progress", None
            if self.counted_drop and not in_combat:
                self.uncredited_kills += 1
                if self.uncredited_kills >= 12:
                    return "no_quest_credit", None
                self.credit_deadline, self.fight = None, None
                self.reset_search(now)
                return "drop_try_next", None
            if (not in_combat and client_edition(sample.build) == "forever" and self.fight
                    and sample.target_id == self.fight.target_id and sample.target_dead):
                self.uncredited_kills += 1
                if self.uncredited_kills < 4:
                    self.credit_deadline, self.fight = None, None
                    self.reset_search(now)
                    return "uncredited_target", None
            return "no_quest_credit", None

        selection = (sample.target_id, sample.state_epoch)
        if selection != self.rejected_selection:
            self.rejected_selection = None
        matches = sample.target_id in self.targets or (not self.targets and verified_target != 0
                                                        and sample.target_id == verified_target)
        if self.fight is None:
            if (matches and (in_combat or self.rejected_selection is None)
                    and not sample.target_dead and not sample.target_danger and not sample.tap_denied):
                self.fight = Fight(sample.target_id, approach=True)
                self.started_at, self.baseline, self.observed_progress = now, progress, progress
            elif in_combat:
                if (sample.target_dead or sample.target_id == 0) and now - self.search_at <= 5:
                    return "waiting", None
                return "unexpected_combat_target", None
            else:
                return self.search_target(now, sample)
        elif (not in_combat and sample.tap_denied and sample.target_id == self.fight.target_id
              and sample.state_epoch == self.fight.state_epoch and not sample.target_dead):
            # Another player can tag the creature between selection and our first hit.
            self.fight = None
            return "target_tagged_by_other_player", None

        if progress > self.observed_progress:
            self.observed_progress = progress
            self.fight.last_credit_at = now
            self.fight.pending_action_at, self.fight.no_receipts = None, 0
        last_progress = self.fight.last_progress(sample, self.started_at)
        if now - self.started_at >= 60 or (now - self.started_at >= 20
                                            and now - last_progress >= 8):
            return "fight_timeout", None
        locked_resource_target = (self.requires_loot and self.fight.state_epoch is not None
                                  and sample.state_epoch == self.fight.state_epoch)
        if (sample.target_id == self.fight.target_id and not sample.target_dead
                and not matches and not locked_resource_target):
            return "waiting", None
        status, key = self.fight.step(now, sample, navigation)
        if self.fight.presses and status in {"target_defeated", "target_missing_or_changed", "target_or_arm_changed"}:
            self.credit_deadline = now + 5
            return "waiting", None
        return status, key


class QuestDefense:
    """Select an attacker and reuse the verified single-target output loop."""

    def __init__(self, now: float):
        self.started_at = now
        self.grounded_at = None
        self.airborne_since = None
        self.quest_progress = None
        self.dismount_at = float("-inf")
        self.dismount_attempts = 0
        self.repositioned = False
        self.retreat = None
        self.retreat_at = None
        self.retreat_origin = None
        self.sequence = None
        self.binding = None
        self.fight: Fight | None = None
        self.next_select_at = now
        self.selects = 0
        self.target_health = None
        self.last_progress_at = now
        self.damage_at = float("-inf")
        self.progress_presses = 0
        self.select_started_at = None
        self.handoff_probe = None
        self.handoff_tried = []
        self.handoff_scene = None
        self.handoff_until = 0.0
        self.handoff_started = None

    def step(self, now: float, sample: CombatSample | None, navigation=None) -> tuple[str, str | None]:
        if self.grounded_at is None:
            self.grounded_at = now
        recent_turn = (sample and self.fight and sample.target_engaged
                       and client_edition(sample.build) == "forever"
                       and (self.fight.turn_goal is not None or (self.fight.last_facing_at is not None
                            and now - self.fight.last_facing_at < 8)))
        budget = 60 if recent_turn or now - self.damage_at < 8 else 35
        if now - self.grounded_at >= budget:
            return ("timeout" if self.binding else "binding_or_telemetry_unavailable"), None
        if sample is None or sample.sequence == self.sequence:
            return "waiting", None
        self.sequence = sample.sequence
        if sample.unsafe or not sample.key_verified or not 1 <= sample.key <= 9:
            return "waiting", None
        if not client_edition(sample.build) or not all(sample.identity[:4]):
            return "unknown_client_profile", None
        binding = (sample.identity, sample.key)
        if self.binding and (not same_output_binding(self.binding[1], sample)
                             or not same_combat_character(self.binding[0], sample)):
            return "action_binding_changed", None
        self.binding = binding

        if sample.target_id and not sample.target_dead and sample.target_engaged and sample.target_danger:
            return "dangerous_attacker", None
        if not (sample.target_id and sample.target_engaged and not sample.target_dead):
            self.fight = None
            if now < self.next_select_at:
                return "waiting", None
            if self.selects >= 4 or (self.select_started_at is not None and now - self.select_started_at >= 3):
                return "attacker_unconfirmed", None
            if self.select_started_at is None:
                self.select_started_at = now
            self.selects += 1
            self.next_select_at = now + 1
            return "select_target", "tab"
        self.select_started_at, self.selects = None, 0
        if (self.fight is None or self.fight.target_id != sample.target_id
                or self.fight.state_epoch != sample.state_epoch):
            self.fight = Fight(sample.target_id, approach=True, defense=True)
            self.target_health = sample.target_health
            self.last_progress_at, self.progress_presses = now, 0
        elif sample.target_health is not None:
            if self.target_health is not None and sample.target_health < self.target_health:
                self.damage_at = now
            if self.target_health is None or sample.target_health < self.target_health:
                self.target_health = sample.target_health
                self.last_progress_at, self.progress_presses = now, self.fight.presses

        if sample.line_of_sight_blocked and self.fight.presses:
            return "line_of_sight_blocked", None
        stalled_for, attempts = (8, 4) if sample.target_health is not None else (12, 6)
        if (now - self.fight.last_progress(sample, self.last_progress_at) >= stalled_for
                and self.fight.presses - self.progress_presses >= attempts):
            return "no_progress", None
        status, key = self.fight.step(now, sample, navigation)
        if status == "action" and self.fight.presses == 1:
            self.last_progress_at = now
        return status, key


def run_fight(fight, read, backend, estop, foreground, duration: float, *, read_with_loot=None) -> str:
    from .loot import RequiredCorpseLoot, loot_prepared

    deadline = time.monotonic() + duration
    corpse = None
    try:
        while time.monotonic() < deadline:
            if estop():
                return "estop"
            if not foreground():
                return "game_not_frontmost"
            sample, nav = read_with_loot() if read_with_loot else (read(), None)
            now = time.monotonic()
            if corpse:
                cmd = corpse.step(now, nav, sample)
                if cmd.outcome:
                    return cmd.outcome
                if cmd.tap:
                    if estop() or not foreground():
                        return "input_interrupted"
                    backend.tap(cmd.tap, **({'hold_s': cmd.hold_s} if cmd.hold_s is not None else {}))
                time.sleep(.08)
                continue
            status, key = fight.step(now, sample, nav)
            if key:
                if status == 'action' and read_with_loot and not loot_prepared(nav):
                    return 'loot_addon_required'
                if estop() or not foreground():
                    return "input_interrupted"
                tap_combat_key(backend, status, key)
            elif read_with_loot and (status == 'target_defeated' or fight.presses
                    and status in {'target_missing_or_changed', 'target_or_arm_changed'}):
                corpse = RequiredCorpseLoot(fight.target_id, fight.state_epoch)
            elif status != "waiting":
                return status
            time.sleep(.08)
        return "loot_unconfirmed" if corpse else "timeout"
    finally:
        backend.release_all()
