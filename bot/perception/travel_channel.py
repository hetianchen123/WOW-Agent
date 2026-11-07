"""Precise quest destination and travel capabilities beside the main pixel strip."""
from dataclasses import dataclass
from enum import IntFlag

import numpy as np

from . import channel_spec as spec
from .quest_channel import QuestState

TRAVEL_ROWS = 12
TRAVEL_OFFSET_X = 144


class TravelFlags(IntFlag):
    OUTDOORS = 1
    FLYABLE = 2
    MOUNTED = 4
    FLYING = 8
    FALLING = 16
    SWIMMING = 32
    IN_COMBAT = 64
    CASTING = 128
    CAN_FLY = 256
    CAN_MOUNT = 512
    SKYRIDING = 1024
    STYLE_SWITCH = 2048
    KEYS_VERIFIED = 4096


@dataclass(frozen=True)
class TravelSample:
    quest_id: int
    map_id: int
    destination_map: int
    destination: tuple[float, float] | None
    position: tuple[float, float] | None
    world_size: tuple[float, float] | None
    height: float | None
    phase: QuestState
    flags: TravelFlags
    sequence: int


def travel_region(x: int, y: int) -> tuple[int, int, int, int]:
    return x + TRAVEL_OFFSET_X, y - TRAVEL_ROWS * spec.REGION_H, spec.REGION_W, TRAVEL_ROWS * spec.REGION_H


def decode_travel(image: np.ndarray) -> TravelSample | None:
    if image.shape != (TRAVEL_ROWS * spec.REGION_H, spec.REGION_W, 3):
        return None
    values = []
    for row in range(TRAVEL_ROWS):
        frame = spec.decode_frame(spec.sample_nibbles(image, 0, row * spec.REGION_H))
        if frame is None or frame[0] != row + 4:
            return None
        values.append(frame[1])
    quest, goal_map, gx, gy, status, height, width, depth, meta, px, py, map_id = values
    if meta & 15 != 1:
        return None
    try:
        phase = QuestState((meta >> 4) & 7)
    except ValueError:
        return None
    return TravelSample(
        quest, map_id, goal_map,
        (gx / 1048575, gy / 1048575) if meta & 128 else None,
        (px / 1048575, py / 1048575) if meta & 1024 else None,
        (width / 10, depth / 10) if meta & 512 and width and depth else None,
        (height - 524288) / 10 if meta & 256 else None,
        phase, TravelFlags(status & 8191), status >> 13,
    )
