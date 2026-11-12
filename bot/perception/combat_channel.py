"""Combat rows and optional facing feedback above the quest strip."""
import math
from dataclasses import dataclass

import numpy as np

from . import channel_spec as spec
from .quest_channel import QUEST_ROWS

COMBAT_ROWS = 6
COMBAT_CAPTURE_ROWS = COMBAT_ROWS + 2


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
    target_engaged: bool = False
    facing: float | None = None
    facing_error: int | None = None
    turn_verified: bool = False
    in_range: bool | None = None
    previous_target_epoch: int | None = None

    @property
    def identity(self) -> tuple[int, ...]:
        return (self.build, self.project_id, self.class_id, self.race_id,
                self.spec_id, self.config_id, self.level)


def combat_region(x: int, y: int) -> tuple[int, int, int, int]:
    return (x, y - (QUEST_ROWS + COMBAT_CAPTURE_ROWS) * spec.REGION_H,
            spec.REGION_W, COMBAT_CAPTURE_ROWS * spec.REGION_H)


def decode_combat(img: np.ndarray) -> CombatSample | None:
    if img.shape not in {(rows * spec.REGION_H, spec.REGION_W, 3)
                         for rows in (COMBAT_ROWS, COMBAT_ROWS + 1, COMBAT_CAPTURE_ROWS)}:
        return None
    offset = img.shape[0] - COMBAT_ROWS * spec.REGION_H
    values = []
    for row in range(COMBAT_ROWS):
        frame = spec.decode_frame(spec.sample_nibbles(img, 0, offset + row * spec.REGION_H))
        if frame is None or frame[0] != 0xA + row:
            return None
        values.append(frame[1])
    identity, build, spec_level, config, target, status = values
    sequence = status >> 13
    facing, facing_error, turn_verified = None, None, False
    if offset:
        frame = spec.decode_frame(spec.sample_nibbles(img, 0, offset - spec.REGION_H))
        if frame and frame[0] == 9 and (frame[1] >> 9) & 127 == sequence:
            angle = frame[1] & 511
            facing = (angle - 1) * math.tau / 511 if angle else None
            facing_error = (frame[1] >> 16) & 7
            turn_verified = bool(frame[1] & 524288)
    in_range, previous_target_epoch = None, None
    if offset == 2 * spec.REGION_H:
        frame = spec.decode_frame(spec.sample_nibbles(img, 0, 0))
        if frame and frame[0] == 8 and frame[1] >> 10 == sequence:
            in_range = {1: True, 2: False}.get(frame[1] & 3)
            epoch = (frame[1] >> 2) & 127
            previous_target_epoch = epoch - 1 if 1 <= epoch <= 64 else None
    return CombatSample(
        identity & 31, (identity >> 5) & 255, identity >> 13,
        build, spec_level & 4095, (spec_level >> 12) & 127, config, target,
        bool(status & 1), bool(status & 2), bool(status & 4),
        (status >> 3) & 15, (status >> 7) & 63, sequence, bool(spec_level & 524288),
        facing, facing_error, turn_verified, in_range, previous_target_epoch,
    )
