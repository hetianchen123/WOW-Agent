"""Independent channel freshness; repeated decoding never refreshes a sample."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChannelObservation:
    value: Any
    sequence: int | None
    received_at: float
    changed_at: float

    def fresh(self, now: float, max_age_s: float) -> bool:
        return self.value is not None and 0 <= now - self.changed_at <= max_age_s


@dataclass(frozen=True)
class ObservationSnapshot:
    captured_at: float
    quest: ChannelObservation
    navigation: ChannelObservation
    combat: ChannelObservation


class ObservationTracker:
    def __init__(self):
        self.channels = {}

    def observe(self, name: str, value, now: float) -> ChannelObservation:
        previous = self.channels.get(name)
        sequence = getattr(value, "sequence", None)
        changed = now if previous is None or previous.sequence != sequence else previous.changed_at
        result = ChannelObservation(value, sequence, now, changed)
        self.channels[name] = result
        return result

    def snapshot(self, now, quest, navigation, combat) -> ObservationSnapshot:
        return ObservationSnapshot(now, *(self.observe(name, value, now) for name, value in
            (("quest", quest), ("navigation", navigation), ("combat", combat))))
