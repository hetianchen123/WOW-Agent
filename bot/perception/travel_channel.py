"""Precise quest destination and travel capabilities beside the main pixel strip."""
from dataclasses import dataclass
from enum import IntFlag

import numpy as np

from . import channel_spec as spec
from .quest_channel import QuestState

TRAVEL_ROWS = 12
TRAVEL_OFFSET_X = 144
# Navigation metadata v2 uses the same codes as Quest.lua's DescentBinding.
DESCENT_KEYS = {1: "up", 2: "down"}
DESCENT_KEYS.update({number + 2: f"f{number}" for number in range(1, 13)})
DESCENT_KEYS.update({ord(key): key.lower() for key in "ABCDEGHIJKLMNOPQRTUVXYZ"})
SKY_KEYS = (None, *(f"f{number}" for number in range(1, 13)), *"abcdeghijklmnopqrtuvxyz0")


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


class TaskFlags(IntFlag):
    PREPARED = 1
    NPC_CANDIDATE = 2  # Soft-target detection can extend beyond interaction range.
    NPC_SELECTED = 4
    LOOT_IN_RANGE = 8
    ITEM_BOUND = 16
    NPC_CONFIGURED = 32
    USE_ITEM = 64


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
    flight_mount_active: bool = False
    interact_verified: bool = False
    descent_key: str | None = None
    sky_pitch_key: str | None = None
    sky_dismount_key: str | None = None
    sky_ascent_key: str | None = None
    sky_halt_key: str | None = None
    task: TaskFlags = TaskFlags(0)


def travel_region(x: int, y: int) -> tuple[int, int, int, int]:
    height = (TRAVEL_ROWS + 2) * spec.REGION_H
    return x + TRAVEL_OFFSET_X, y - height, spec.REGION_W, height


def decode_travel(image: np.ndarray) -> TravelSample | None:
    row_height = spec.REGION_H
    if image.shape[1:] != (spec.REGION_W, 3) or image.shape[0] not in {
        (TRAVEL_ROWS + extra) * row_height for extra in range(3)
    }:
        return None
    offset = image.shape[0] - TRAVEL_ROWS * row_height
    values = []
    for row in range(TRAVEL_ROWS):
        frame = spec.decode_frame(spec.sample_nibbles(image, 0, offset + row * row_height))
        if frame is None or frame[0] != row + 4:
            return None
        values.append(frame[1])
    quest, goal_map, gx, gy, status, height, width, depth, meta, px, py, map_id = values
    version = meta & 15
    if version not in (1, 2, 3, 4):
        return None
    flags = TravelFlags(status & 8191)
    if version == 1:
        descent_key = "x" if flags & TravelFlags.KEYS_VERIFIED else None
    else:
        descent_key = DESCENT_KEYS.get(meta >> 13)
    pitch = dismount = ascent = halt = None
    if version >= 3:
        sky = spec.decode_frame(spec.sample_nibbles(image, 0, offset - row_height)) if offset else None
        if sky is None or sky[0] != 0 or (version == 3 and sky[1] >> 16):
            return None
        ascent_num, halt_num = sky[1] & 15, (sky[1] >> 4) & 15
        key_bits = 4 if version == 3 else 6
        pitch_num = (sky[1] >> 8) & ((1 << key_bits) - 1)
        dismount_num = sky[1] >> (8 + key_bits)
        keys = SKY_KEYS[:13] if version == 3 else SKY_KEYS
        pitch = keys[pitch_num] if pitch_num < len(keys) else None
        dismount = keys[dismount_num] if dismount_num < len(keys) and dismount_num != pitch_num else None
        ascent = str(ascent_num) if 1 <= ascent_num <= 9 else None
        halt = str(halt_num) if 1 <= halt_num <= 9 and halt_num != ascent_num else None
    if descent_key is None and not (pitch and dismount):
        flags &= ~TravelFlags.CAN_FLY
    try:
        phase = QuestState((meta >> 4) & 7)
    except ValueError:
        return None
    task = TaskFlags(0)
    sequence = status >> 13
    if offset == 2 * row_height:
        extra = spec.decode_frame(spec.sample_nibbles(image, 0, 0))
        if extra and extra[0] == 1 and extra[1] >> 7 == sequence:
            task = TaskFlags(extra[1] & 127)
    return TravelSample(
        quest, map_id, goal_map,
        (gx / 1048575, gy / 1048575) if meta & 128 and goal_map else None,
        (px / 1048575, py / 1048575) if meta & 1024 else None,
        (width / 10, depth / 10) if meta & 512 and width and depth else None,
        (height - 524288) / 10 if meta & 256 else None,
        phase, flags, sequence, bool(meta & 2048), bool(meta & 4096), descent_key,
        pitch, dismount, ascent, halt, task,
    )
