"""Six atomic combat rows located above the optional quest strip."""
from dataclasses import dataclass

import numpy as np

from . import channel_spec as spec
from .quest_channel import QUEST_ROWS

COMBAT_ROWS = 6


@dataclass(frozen=True)
class CombatSample:
    class_id: int
    race_id: int
    project_id: int
    build: int
    spec_id: int
    level: int
    config_id: int
    target_id: int
    target_dead: bool
    key_verified: bool
    unsafe: bool
    key: int
    state_epoch: int
    sequence: int

    @property
    def identity(self) -> tuple[int, ...]:
        return (self.build, self.project_id, self.class_id, self.race_id,
                self.spec_id, self.config_id, self.level)


def combat_region(x: int, y: int) -> tuple[int, int, int, int]:
    return (x, y - (QUEST_ROWS + COMBAT_ROWS) * spec.REGION_H,
            spec.REGION_W, COMBAT_ROWS * spec.REGION_H)


def decode_combat(img: np.ndarray) -> CombatSample | None:
    if img.shape != (COMBAT_ROWS * spec.REGION_H, spec.REGION_W, 3):
        return None
    values = []
    for row in range(COMBAT_ROWS):
        frame = spec.decode_frame(spec.sample_nibbles(img, 0, row * spec.REGION_H))
        if frame is None or frame[0] != 0xA + row:
            return None
        values.append(frame[1])
    identity, build, spec_level, config, target, status = values
    return CombatSample(
        identity & 31, (identity >> 5) & 255, identity >> 13,
        build, spec_level & 4095, spec_level >> 12, config, target,
        bool(status & 1), bool(status & 2), bool(status & 4),
        (status >> 3) & 15, (status >> 7) & 63, status >> 13,
    )
