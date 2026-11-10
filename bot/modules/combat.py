"""Bounded use of a client-verified action against one selected creature."""
from __future__ import annotations

import math
import time

from ..control.navigation import heading_error
from ..data.combat import client_edition
from ..perception.combat_channel import CombatSample

TURN_OFFSETS = (math.pi, math.pi / 2, -math.pi / 2)


class Fight:
    def __init__(self, target_id: int):
        self.target_id = target_id
        self.identity = None
        self.state_epoch = None
        self.key = None
        self.last_sequence = None
        self.last_sample = None
        self.last_action = float("-inf")
        self.presses = 0
        self.last_facing_error = None
        self.turn_origin: float | None = None
        self.turn_goal: float | None = None
        self.turn_deadline = 0.0
        self.turn_attempts = 0

    def step(self, now: float, sample: CombatSample | None) -> tuple[str, str | None]:
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
        if self.identity is not None and sample.identity != self.identity:
            return "character_profile_changed", None
        if sample.target_id != self.target_id:
            return "target_missing_or_changed", None
        if self.state_epoch is not None and sample.state_epoch != self.state_epoch:
            return "target_or_arm_changed", None
        if sample.target_dead:
            return "target_defeated" if self.presses else "target_already_dead", None
        if not sample.key_verified or not 1 <= sample.key <= 9:
            return "unverified_action_binding", None
        if self.key is not None and sample.key != self.key:
            return "action_binding_changed", None
        starting = self.identity is None
        self.identity, self.state_epoch, self.key = sample.identity, sample.state_epoch, sample.key
        if starting:
            self.last_facing_error = sample.facing_error
            return "waiting", None
        if self.turn_goal is not None or (sample.facing_error is not None
                                          and sample.facing_error != self.last_facing_error):
            turn = self.correct_facing(now, sample)
            if turn is not None:
                return turn
        if now - self.last_action < .8:
            return "waiting", None
        self.last_action = now
        self.presses += 1
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
            if self.turn_attempts >= len(TURN_OFFSETS):
                return "facing_unconfirmed", None
            if self.turn_origin is None:
                self.turn_origin = sample.facing
            self.turn_goal = (self.turn_origin + TURN_OFFSETS[self.turn_attempts]) % math.tau
            self.turn_attempts += 1
            self.turn_deadline = now + 5
        error = heading_error(sample.facing, self.turn_goal)
        if abs(error) <= math.radians(15):
            self.turn_goal = None
            return None
        if now >= self.turn_deadline:
            return "facing_turn_timeout", None
        return "turn", "left" if error > 0 else "right"


def tap_combat_key(backend, status, key):
    if status == "turn":
        backend.tap(key, hold_s=.10)
    else:
        backend.tap(key)


class QuestHunt:
    """Select quest creatures at a waypoint and wait for quest credit."""

    def __init__(self, targets: frozenset[int]):
        self.targets = targets
        self.fight: Fight | None = None
        self.started_at = 0.0
        self.baseline = 0
        self.credit_deadline: float | None = None
        self.search_at = float("-inf")
        self.searches = 0
        self.sequence = None
        self.frame_at = 0.0
        self.binding = None

    def step(
        self, now: float, sample: CombatSample | None, progress: int, in_combat: bool, verified_target: int = 0,
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
        if self.binding is not None and binding != self.binding:
            return "action_binding_changed", None
        self.binding = binding

        if self.credit_deadline is not None:
            if progress > self.baseline:
                self.credit_deadline, self.fight, self.searches = None, None, 0
                self.search_at = now
                return "credited", None
            return ("no_quest_credit" if now >= self.credit_deadline else "waiting"), None

        matches = sample.target_id in self.targets or (not self.targets and verified_target != 0
                                                        and sample.target_id == verified_target)
        if self.fight is None:
            if matches and not sample.target_dead:
                self.fight = Fight(sample.target_id)
                self.started_at, self.baseline = now, progress
            elif in_combat:
                if (sample.target_dead or sample.target_id == 0) and now - self.search_at <= 5:
                    return "waiting", None
                return "unexpected_combat_target", None
            elif now - self.search_at < 1:
                return "waiting", None
            elif self.searches >= 5:
                return "target_not_found", None
            else:
                self.searches += 1
                self.search_at = now
                return "select_target", "tab"

        if now - self.started_at >= 20:
            return "fight_timeout", None
        if sample.target_id == self.fight.target_id and not sample.target_dead and not matches:
            return "waiting", None
        status, key = self.fight.step(now, sample)
        target_gone = status in {"target_defeated", "target_missing_or_changed"}
        if status == "target_or_arm_changed" and sample.target_dead:
            target_gone = True
        if self.fight.presses and target_gone:
            self.credit_deadline = now + 5
            return "waiting", None
        return status, key


class QuestDefense:
    """Select an attacker and reuse the verified single-target output loop."""

    def __init__(self, now: float):
        self.started_at = now
        self.sequence = None
        self.binding = None
        self.fight: Fight | None = None
        self.next_select_at = now
        self.selects = 0

    def step(self, now: float, sample: CombatSample | None) -> tuple[str, str | None]:
        if now - self.started_at >= 35:
            return ("timeout" if self.binding else "binding_or_telemetry_unavailable"), None
        if sample is None or sample.sequence == self.sequence:
            return "waiting", None
        self.sequence = sample.sequence
        if sample.unsafe or not sample.key_verified or not 1 <= sample.key <= 9:
            return "waiting", None
        if not client_edition(sample.build) or not all(sample.identity[:4]):
            return "unknown_client_profile", None
        binding = (sample.identity, sample.key)
        if self.binding and self.binding != binding:
            return "action_binding_changed", None
        self.binding = binding

        if not (sample.target_id and sample.target_engaged and not sample.target_dead):
            self.fight = None
            if self.selects < 8 and now >= self.next_select_at:
                self.selects += 1
                self.next_select_at = now + 1
                return "select_target", "tab"
            return "waiting", None
        if (self.fight is None or self.fight.target_id != sample.target_id
                or self.fight.state_epoch != sample.state_epoch):
            self.fight = Fight(sample.target_id)
        return self.fight.step(now, sample)


def run_fight(fight, read, backend, estop, foreground, duration: float) -> str:
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            if estop():
                return "estop"
            if not foreground():
                return "game_not_frontmost"
            status, key = fight.step(time.monotonic(), read())
            if key:
                if estop() or not foreground():
                    return "input_interrupted"
                tap_combat_key(backend, status, key)
            elif status != "waiting":
                return status
            time.sleep(.08)
        return "timeout"
    finally:
        backend.release_all()
