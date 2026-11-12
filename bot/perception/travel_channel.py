"""Precise quest destination and travel capabilities beside the main pixel strip."""
from dataclasses import dataclass
from enum import IntEnum, IntFlag

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
    NPC_WITHIN_5_YARDS = 128
    NPC_RANGE_KNOWN = 256
    MOUNT_TUTORIAL = 512


class ObjectiveAction(IntEnum):
    NONE = 0
    COMBAT = 1
    INTERACT = 2
    ARRIVE = 3


class RoundStage(IntEnum):
    NONE = 0
    PICKUP = 1
    OBJECTIVES = 2
    TURN_IN = 3
    DONE = 4


class LifeState(IntEnum):
    UNKNOWN = 0
    ALIVE = 1
    DEAD = 2
    GHOST = 3


class PetState(IntEnum):
    UNKNOWN = 0
    ABSENT = 1
    ALIVE = 2
    DEAD = 3


@dataclass(frozen=True)
class RecoverySample:
    life: LifeState
    corpse: tuple[float, float] | None = None
    button: tuple[float, float] | None = None
    ui_aspect: float = 0.0  # UI height / width
    dialog: bool = False
    hovered: bool = False
    pet: PetState = PetState.UNKNOWN
    pet_expected: bool = False
    follow: bool = False
    summon: bool = False
    revive: bool = False
    buff_missing: bool = False
    buff_unverified: bool = False


@dataclass(frozen=True)
class MinimapGeometry:
    center: tuple[float, float]
    size: tuple[float, float]
    radius: int
    rotates: bool


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
    npc_range_supported: bool = False
    mount_button: tuple[float, float] | None = None
    mount_button_key: str | None = None
    objective_action: ObjectiveAction = ObjectiveAction.NONE
    objective_target: int = 0
    objective_cursor: tuple[float, float] | None = None
    round_stage: RoundStage = RoundStage.NONE
    round_advance: bool = False
    round_blocked: bool = False
    round_immediate: bool = False
    objective_pin: bool = False
    minimap: MinimapGeometry | None = None
    corpse_lootable: bool | None = None
    fetch_ready: bool = False
    precise_object: bool = False
    apr_waypoint: bool = False
    strafe_verified: bool = False
    last_target_verified: bool = False
    recovery: RecoverySample | None = None


def travel_region(x: int, y: int) -> tuple[int, int, int, int]:
    height = (TRAVEL_ROWS + 11) * spec.REGION_H
    return x + TRAVEL_OFFSET_X, y - height, spec.REGION_W, height


def decode_travel(image: np.ndarray) -> TravelSample | None:
    row_height = spec.REGION_H
    if image.shape[1:] != (spec.REGION_W, 3) or image.shape[0] not in {
        (TRAVEL_ROWS + extra) * row_height for extra in range(12)
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
    if not 1 <= version <= 10:
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
    range_supported = False
    mount_button = None
    mount_button_key = None
    action_code = 0
    if offset >= 2 * row_height:
        extra = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 2 * row_height))
        task_bits = 10 if version >= 6 else (9 if version == 5 else 7)
        packed_sequence = extra[1] >> task_bits if extra else -1
        if version >= 7:
            code, packed_sequence = divmod(packed_sequence, 128)
        if extra and extra[0] == 1 and packed_sequence == sequence:
            task = TaskFlags(extra[1] & ((1 << task_bits) - 1))
            range_supported = version >= 5
            action_code = code if version >= 7 else 0
    action = ObjectiveAction.NONE
    objective_pin = False
    target = 0
    cursor = None
    if version >= 7 and offset >= 3 * row_height:
        row = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 3 * row_height))
        if row and row[0] == 2:
            value = row[1]
            point = ((value >> 10) / 1023, (value & 1023) / 1023) if value else None
            if task & TaskFlags.MOUNT_TUTORIAL:
                mount_button = point
                mount_button_key = str(action_code) if point and 2 <= action_code <= 4 else None
            elif version >= 8 and phase == QuestState.ACTIVE and 1 <= action_code <= 4:
                objective_pin = action_code == 4
                action = ObjectiveAction.INTERACT if objective_pin else ObjectiveAction(action_code)
                target = value if action == ObjectiveAction.COMBAT else 0
                cursor = point if action == ObjectiveAction.INTERACT else None
    round_stage, round_advance, round_blocked, round_immediate = RoundStage.NONE, False, False, False
    corpse_lootable, fetch_ready, precise_object, apr_waypoint, strafe_verified = None, False, False, False, False
    last_target_verified = False
    if version >= 9:
        if offset < 4 * row_height:
            return None
        row = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 4 * row_height))
        if row is None or row[0] != 3 or ((row[1] >> 6) & 127) != sequence or (row[1] & 7) > RoundStage.DONE:
            return None
        round_stage = RoundStage(row[1] & 7)
        round_advance, round_blocked = bool(row[1] & 8), bool(row[1] & 16)
        round_immediate = bool(row[1] & 32)
        if row[1] & 8192:
            corpse_lootable = bool(row[1] & 16384)
        fetch_ready = bool(row[1] & 32768)
        precise_object = bool(row[1] & 65536) and action == ObjectiveAction.INTERACT
        apr_waypoint = precise_object and not objective_pin and bool(row[1] & 131072)
        strafe_verified = bool(row[1] & 262144)
        last_target_verified = bool(row[1] & 524288)
    minimap = None
    if version >= 9 and offset >= 7 * row_height:
        minimap_rows = [spec.decode_frame(spec.sample_nibbles(image, 0, offset - i * row_height))
                        for i in (7, 6, 5)]
        if all(frame and frame[0] == index for index, frame in enumerate(minimap_rows)):
            center, size, info = [frame[1] for frame in minimap_rows]
            radius = info & 1023
            if info >> 11 == sequence and radius and (size >> 10) and (size & 1023):
                minimap = MinimapGeometry(((center >> 10) / 1023, (center & 1023) / 1023),
                                          ((size >> 10) / 1023, (size & 1023) / 1023),
                                          radius, bool(info & 1024))
    recovery = None
    if version == 10:
        if offset < 11 * row_height:
            return None
        rows = [spec.decode_frame(spec.sample_nibbles(image, 0, offset - i * row_height))
                for i in (11, 10, 9, 8)]
        if any(row is None or row[0] != kind for kind, row in enumerate(rows, 12)):
            return None
        state, corpse, button, aspect = (row[1] for row in rows)
        if state >> 13 != sequence:
            return None
        life = LifeState(state & 3)
        if life in {LifeState.DEAD, LifeState.GHOST}:
            def point(value):
                return (value >> 10) / 1023, (value & 1023) / 1023

            recovery = RecoverySample(
                life, point(corpse) if state & 512 else None,
                point(button) if state & 4096 else None, aspect / 100000,
                dialog=bool(state & 1024), hovered=bool(state & 2048),
            )
        else:
            recovery = RecoverySample(
                life, pet=PetState((state >> 2) & 3), pet_expected=bool(state & 512),
                follow=bool(state & 16), summon=bool(state & 32), revive=bool(state & 64),
                buff_missing=bool(state & 128), buff_unverified=bool(state & 256),
            )
    return TravelSample(
        quest, map_id, goal_map,
        (gx / 1048575, gy / 1048575) if meta & 128 and goal_map else None,
        (px / 1048575, py / 1048575) if meta & 1024 else None,
        (width / 10, depth / 10) if meta & 512 and width and depth else None,
        (height - 524288) / 10 if meta & 256 else None,
        phase, flags, sequence, bool(meta & 2048), bool(meta & 4096), descent_key,
        pitch, dismount, ascent, halt, task, range_supported, mount_button, mount_button_key,
        action, target, cursor, round_stage, round_advance, round_blocked, round_immediate, objective_pin, minimap,
        corpse_lootable, fetch_ready, precise_object, apr_waypoint, strafe_verified, last_target_verified, recovery,
    )
