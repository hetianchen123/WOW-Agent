"""Bounded use of a client-verified action against one selected creature."""
from __future__ import annotations

import time

from ..data.combat import client_edition
from ..perception.combat_channel import CombatSample


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
            return "waiting", None
        if now - self.last_action < .8:
            return "waiting", None
        self.last_action = now
        self.presses += 1
        return "action", str(sample.key)


class QuestHunt:
    """Select allowlisted creatures at a waypoint and wait for quest credit."""

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
        self, now: float, sample: CombatSample | None, progress: int, in_combat: bool,
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

        if self.fight is None:
            if sample.target_id in self.targets and not sample.target_dead:
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
        status, key = self.fight.step(now, sample)
        target_gone = status in {"target_defeated", "target_missing_or_changed"}
        if status == "target_or_arm_changed" and sample.target_dead:
            target_gone = True
        if self.fight.presses and target_gone:
            self.credit_deadline = now + 5
            return "waiting", None
        return status, key


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
                backend.tap(key)
            elif status != "waiting":
                return status
            time.sleep(.08)
        return "timeout"
    finally:
        backend.release_all()
