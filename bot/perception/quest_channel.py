"""Decode the optional five-row quest strip above the existing pixel channel."""
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from . import channel_spec as spec

QUEST_ROWS = 5


class QuestKind(IntEnum):
    UNKNOWN = 0
    CAMPAIGN = 1
    MAINLINE = 2
    SIDE = 3
    WORLD = 4
    REPEATABLE = 5
    IMPORTANT = 6
    SPECIAL = 7


class QuestState(IntEnum):
    UNKNOWN = 0
    PENDING = 1
    ACTIVE = 2
    READY = 3
    TURNED_IN = 4


class Dialogue(IntEnum):
    CLOSED = 0
    OFFER = 1
    TURN_IN = 2
    DETAIL = 3
    PROGRESS = 4
    REWARD = 5
    BLOCKED = 6


class Objective(IntEnum):
    UNKNOWN = 0
    NONE = 1
    COUNTED = 2
    NON_COUNTED = 3


@dataclass(frozen=True)
class QuestSample:
    quest_id: int
    kind: QuestKind
    state: QuestState
    objective: Objective
    progress: tuple[int, int]
    map_id: int
    dialogue_id: int
    dialogue: Dialogue
    sequence: int
    tracking: bool


def quest_region(x: int, y: int) -> tuple[int, int, int, int]:
    height = QUEST_ROWS * spec.REGION_H
    return x, y - height, spec.REGION_W, height


def decode_quest(img: np.ndarray) -> QuestSample | None:
    if img.shape != (QUEST_ROWS * spec.REGION_H, spec.REGION_W, 3):
        return None
    values = []
    for row in range(QUEST_ROWS):
        frame = spec.decode_frame(spec.sample_nibbles(img, 0, row * spec.REGION_H))
        if frame is None or frame[0] != 0xA + row:
            return None
        values.append(frame[1])
    quest_id, info, progress, map_id, dialogue_id = values
    try:
        return QuestSample(
            quest_id, QuestKind(info & 0xF), QuestState((info >> 4) & 7),
            Objective((info >> 10) & 3), divmod(progress, 1024), map_id,
            dialogue_id, Dialogue((info >> 7) & 7), (info >> 12) & 127,
            bool(info & (1 << 19)),
        )
    except ValueError:
        return None
