"""Finish confirmed kills before resuming travel or selecting another target."""
import math

from ..control.approach import ActionState, ApproachAttempt, ApproachGoal, ApproachObservation
from ..control.navigation import heading_error
from ..control.quest_travel import TURN_RATE, TravelCommand
from ..perception.travel_channel import LifeState, TaskFlags, TravelFlags


CORPSE_LOOT_BUILD = 323
LOOT_BLOCKED = (TravelFlags.IN_COMBAT | TravelFlags.MOUNTED | TravelFlags.FLYING |
                TravelFlags.FALLING | TravelFlags.SWIMMING | TravelFlags.CASTING)


def loot_prepared(nav):
    return bool(nav and (getattr(nav, 'addon_build', None) or 0) >= CORPSE_LOOT_BUILD
                and nav.task & TaskFlags.PREPARED and getattr(nav, 'loot_epoch', None) is not None)


class RequiredCorpseLoot:
    def __init__(self, target, epoch, *, allow_approach=True, death_verified=True):
        self.target, self.epoch = target, epoch
        self.allow_approach = allow_approach
        self.death_verified = death_verified
        self.started = None
        self.sequence = None
        self.restored = self.fetched = False
        self.presses = 0
        self.sent_at = None
        self.loot_epoch = None
        self.action_map = None
        self.receipt_at_action = None
        self.full_receipt_at_action = None
        self.loot_seen = False
        self.denied_target_epoch = None
        self.loot_allowed_epoch = None
        self.approach = None
        self.steps = 0

    def matches(self, sample):
        return bool(self.epoch is not None and sample and not sample.unsafe and sample.target_dead
                    and sample.target_id == self.target
                    and (sample.state_epoch == self.epoch or getattr(sample, 'previous_target_epoch', None) == self.epoch))

    def still_alive(self, sample):
        return (not self.death_verified and sample and not sample.unsafe and not sample.target_dead
                and sample.target_id == self.target
                and self.epoch is not None
                and self.epoch in (sample.state_epoch, getattr(sample, 'previous_target_epoch', None)))

    def has_clear_receipt(self, nav):
        receipt = getattr(nav, 'corpse_loot_receipt', None)
        return (self.sent_at is not None and nav and nav.map_id == self.action_map and receipt
                and receipt != self.receipt_at_action and receipt.target_epoch == self.epoch
                and nav.loot_epoch != self.loot_epoch)

    def has_full_receipt(self, nav):
        receipt = getattr(nav, 'corpse_loot_full_receipt', None)
        return (self.sent_at is not None and self.epoch is not None and receipt and nav.map_id == self.action_map
                and receipt != self.full_receipt_at_action and receipt.target_epoch == self.epoch)

    def step(self, now, nav, sample):
        if self.started is None:
            self.started = now
        if self.has_full_receipt(nav):
            return TravelCommand(outcome='corpse_loot_bag_full')
        if now - self.started >= 18:
            return TravelCommand(outcome='loot_unconfirmed')
        if (not loot_prepared(nav) or nav.flags & LOOT_BLOCKED
                or nav.recovery and nav.recovery.life != LifeState.ALIVE or nav.sequence == self.sequence):
            return TravelCommand()
        self.sequence = nav.sequence
        if self.still_alive(sample):
            return TravelCommand(outcome='same_target_still_alive')
        receipt = getattr(nav, 'corpse_loot_receipt', None)
        if self.has_clear_receipt(nav):
            return TravelCommand(outcome='corpse_cleared')
        if not self.matches(sample):
            self.approach = None
            self.denied_target_epoch = None
            self.loot_allowed_epoch = None
            if (self.sent_at is None and not self.restored and sample and not sample.unsafe
                    and (not sample.target_id or sample.target_dead)
                    and nav.last_target_verified):
                self.restored = True
                return TravelCommand(tap='p')
            return TravelCommand()
        self.death_verified = True
        if getattr(nav, "corpse_loot_denied", False):
            self.loot_allowed_epoch = None
            if self.denied_target_epoch == sample.state_epoch:
                return TravelCommand(outcome="corpse_loot_not_owned")
            self.denied_target_epoch = sample.state_epoch
            self.approach = None
            return TravelCommand()
        self.denied_target_epoch = None
        in_range = bool(nav.task & TaskFlags.LOOT_IN_RANGE)
        self.loot_seen |= nav.corpse_lootable is True
        own_loot = nav.corpse_lootable is True
        permission_confirmed = own_loot and self.loot_allowed_epoch == sample.state_epoch
        self.loot_allowed_epoch = sample.state_epoch if own_loot else None
        if self.sent_at is not None:
            if nav.corpse_lootable is False and (in_range or self.loot_seen or nav.loot_epoch != self.loot_epoch):
                if self.loot_seen or nav.loot_epoch != self.loot_epoch:
                    return TravelCommand(outcome='corpse_cleared')
                if self.presses and now - self.sent_at >= .6:
                    return TravelCommand(outcome='corpse_empty')
            if now - self.sent_at < (2.5 if self.presses else 8):
                return TravelCommand()
            self.sent_at = None
        key = None
        explicit_permission = (nav.addon_build or 0) >= 336
        if in_range and self.presses < 2 and (permission_confirmed or not explicit_permission):
            self.presses += 1
            key = 'f'
        elif not self.fetched and nav.fetch_ready and (not explicit_permission or nav.corpse_lootable is False
                                                       or permission_confirmed):
            self.fetched = True
            key = 'l'
        if key:
            self.sent_at, self.loot_epoch = now, nav.loot_epoch
            self.action_map, self.receipt_at_action = nav.map_id, receipt
            self.full_receipt_at_action = getattr(nav, 'corpse_loot_full_receipt', None)
            return TravelCommand(tap=key)
        if explicit_permission and not permission_confirmed:
            return TravelCommand()
        if (self.allow_approach and not in_range and nav.corpse_lootable is True
                and nav.flags & TravelFlags.KEYS_VERIFIED and nav.position is not None
                and nav.world_size is not None and self.steps < 4):
            identity = (nav.map_id, self.target, sample.state_epoch)
            observation = ApproachObservation(identity, nav.sequence, False, True, nav.position, nav.world_size)
            if self.approach:
                result = self.approach.poll(now, observation)
                if result.state == ActionState.WAITING:
                    return TravelCommand()
                if result.state != ActionState.CONFIRMED:
                    return TravelCommand(outcome='loot_approach_' + result.reason)
            self.approach = ApproachAttempt(ApproachGoal('corpse', identity, pulse_s=.18), now, observation)
            self.steps += 1
            return TravelCommand(tap='w', hold_s=.18)
        return TravelCommand(outcome='loot_unconfirmed')


class NearbyCorpseScan:
    """Check the native interaction target from a few stationary headings."""

    HEADINGS = tuple(map(math.radians, (0, 80, 160, 240, 320)))

    def __init__(self, now, nav, combat):
        self.map_id = nav.map_id
        self.heading = combat.facing
        self.view = 0
        self.deadline = now + 14
        self.settle_at = now
        self.sequence = None

    def step(self, now, nav, combat):
        if (nav.map_id != self.map_id or combat.facing is None or not combat.turn_verified
                or self.heading is None):
            return TravelCommand(outcome='facing_unavailable')
        if now >= self.deadline or self.view >= len(self.HEADINGS):
            return TravelCommand(outcome='scan_budget' if now >= self.deadline else 'views_checked')
        if now < self.settle_at or self.sequence == combat.sequence:
            return TravelCommand()
        error = heading_error(combat.facing, self.heading + self.HEADINGS[self.view])
        if abs(error) <= math.radians(15):
            return TravelCommand(outcome='view_reached')
        hold = max(.05, min(.34, (abs(error) - math.radians(5)) / TURN_RATE))
        self.settle_at = now + hold + .22
        self.sequence = combat.sequence
        return TravelCommand(tap='left' if error > 0 else 'right', hold_s=hold)


class LootSweep:
    def __init__(self, now):
        self.started = now
        self.attempts = 0
        self.sent_at = None
        self.epoch = None
        self.received_at = None
        self.last_receipt = None
        self.full_receipt_at_action = None
        self.done = False

    def tick(self, ctx, now, nav):
        if self.done:
            return False
        if now - self.started >= 65:
            self.done = True
            ctx.log_event("quest_loot_batch_end", {"reason": "time_budget", "attempts": self.attempts})
            return False
        if not nav.sweep_ready or nav.loot_epoch is None:
            self.done = True
            ctx.log_event("quest_loot_batch_end", {"reason": "binding_or_pet_unavailable"})
            return False
        if nav.flags & (TravelFlags.IN_COMBAT | TravelFlags.MOUNTED | TravelFlags.FLYING |
                        TravelFlags.FALLING | TravelFlags.SWIMMING | TravelFlags.CASTING):
            return True
        if self.sent_at is not None:
            full = getattr(nav, 'corpse_loot_full_receipt', None)
            if full and full != self.full_receipt_at_action:
                self.done = True
                ctx.log_event("quest_loot_batch_end", {"reason": "bag_full", "attempts": self.attempts})
                return False
            if nav.loot_epoch != self.epoch:
                if self.last_receipt != nav.loot_epoch:
                    self.received_at, self.last_receipt = now, nav.loot_epoch
                    ctx.log_event("quest_loot_batch_receipt", {"attempt": self.attempts,
                                                             "receipt": nav.loot_epoch})
                if now - self.received_at < 1.5:
                    return True
                self.sent_at = None
            elif now - self.sent_at < 8:
                return True
            else:
                self.done = True
                ctx.log_event("quest_loot_batch_end", {"reason": "no_further_receipt",
                    "attempts": self.attempts, "all_corpses_cleared": "unknown"})
                return False
        if self.attempts >= 6:
            self.done = True
            ctx.log_event("quest_loot_batch_end", {"reason": "attempt_budget", "attempts": self.attempts})
            return False
        if not nav.fetch_ready:
            return True
        self.attempts += 1
        self.sent_at, self.epoch = now, nav.loot_epoch
        self.received_at = self.last_receipt = None
        self.full_receipt_at_action = getattr(nav, 'corpse_loot_full_receipt', None)
        ctx.log_event("quest_loot_batch_fetch", {"attempt": self.attempts, "key": "o"})
        ctx.input.tap("o")
        return True
