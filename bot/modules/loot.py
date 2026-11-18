"""Finish a nearby pet-loot batch before resuming movement or quest turn-in."""
from ..perception.travel_channel import TravelFlags


class LootSweep:
    def __init__(self, now):
        self.started = now
        self.attempts = 0
        self.sent_at = None
        self.epoch = None
        self.received_at = None
        self.last_receipt = None
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
        ctx.log_event("quest_loot_batch_fetch", {"attempt": self.attempts, "key": "o"})
        ctx.input.tap("o")
        return True
