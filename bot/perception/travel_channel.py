"""Precise quest destination and travel capabilities beside the main pixel strip."""
import math
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
    NPC_TALK = 5
    CHOICE = 6


class ChoiceStep(IntEnum):
    WAIT = 0
    SELECT = 1
    OPEN_PANEL = 2
    APPROACH = 3
    STAND = 4


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


class CorpseSource(IntEnum):
    UNKNOWN = 0
    CURRENT_MAP = 1
    PARENT_MAP = 2
    RECORDED_DEATH = 3
    TRACKED_WAYPOINT = 4
    MINIMAP_MAP = 5


class CameraScene(IntEnum):
    UNKNOWN = 0
    GROUND = 1
    MOUNTED = 2
    NARROW = 3
    SEARCH = 4
    INTERACT = 5
    RANGED = 6
    MELEE = 7
    FLIGHT = 8
    LANDING = 9
    GHOST = 10
    SWIMMING = 11
    GAME_CONTROL = 12
    PROBE = 13
    OVERHEAD = 14


class CameraResult(IntEnum):
    IDLE = 0
    PENDING = 1
    DISTANCE_VERIFIED = 2
    LIMITED = 3
    UNKNOWN = 4
    INTERRUPTED = 5
    TIMED_OUT = 6
    PAUSED = 7


@dataclass(frozen=True)
class CameraSample:
    zoom: float | None
    view: int
    scene: CameraScene
    applied: CameraScene
    request: int
    result: CameraResult
    paused: bool = False
    calibrated: bool = False


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
    walk_verified: bool | None = None
    facing: float | None = None
    corpse_source: CorpseSource = CorpseSource.UNKNOWN
    corpse_distance: float | None = None
    pet_health: int | None = None
    mend_verified: bool = False
    mend_active: bool = False
    mend_unusable: bool = False
    mend_epoch: int = 0
    patrol_id: int = 0
    patrol_danger: int = 0
    attacker_count: int = 0
    attacker_cursor: tuple[float, float] | None = None
    attacker_hovered: bool = False
    attacker_scan_supported: bool = False


@dataclass(frozen=True)
class MinimapGeometry:
    center: tuple[float, float]
    size: tuple[float, float]
    radius: int
    rotates: bool


@dataclass(frozen=True)
class SkyFlight:
    surge_key: str | None = None
    second_wind_key: str | None = None
    whirling_key: str | None = None
    speed: int | None = None
    charges: int | None = None
    cast: str | None = None
    cast_epoch: int = 0


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
    loot_epoch: int | None = None
    sweep_ready: bool = False
    search_area: tuple[tuple[float, float], ...] | None = None
    sky_flight: SkyFlight | None = None
    camera_restore_available: bool = False
    camera_zoom_close: bool = False
    camera_epoch: int = 0
    objective_index: int | None = None
    objectives_finished: bool = False
    objective_progress: tuple[int, int] = (0, 0)
    objective_resource: bool = False
    suspend_ready: bool = False
    suspend_epoch: int | None = None
    current_suspended: bool = False
    protocol_version: int | None = None
    camera: CameraSample | None = None
    addon_build: int | None = None
    probe_camera_available: bool = False
    overhead_camera_available: bool = False
    calibration_orbit_available: bool = False
    calibration_orbit_active: bool = False
    round_pending_count: int | None = None
    extension_schema: int | None = None
    visual_ui_hidden: int | None = None
    choice_step: ChoiceStep = ChoiceStep.WAIT


def travel_region(x: int, y: int) -> tuple[int, int, int, int]:
    height = (TRAVEL_ROWS + 26) * spec.REGION_H
    return x + TRAVEL_OFFSET_X, y - height, spec.REGION_W, height


def decode_travel(image: np.ndarray) -> TravelSample | None:
    row_height = spec.REGION_H
    if image.shape[1:] != (spec.REGION_W, 3) or image.shape[0] not in {
        (TRAVEL_ROWS + extra) * row_height for extra in range(27)
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
    if not 1 <= version <= 15:
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
    choice_step = ChoiceStep.WAIT
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
            elif version >= 8 and phase == QuestState.ACTIVE and 1 <= action_code <= 6:
                objective_pin = action_code == 4
                action = ObjectiveAction.INTERACT if objective_pin else ObjectiveAction(action_code)
                target = value if action == ObjectiveAction.COMBAT else 0
                cursor = point if action == ObjectiveAction.INTERACT else None
                if action == ObjectiveAction.CHOICE and 0 <= value <= ChoiceStep.STAND:
                    choice_step = ChoiceStep(value)
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
    if version >= 10:
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

            # V11 uses the button row for synchronized heading while no popup is visible.
            heading = ((button - 1) * math.tau / 1048575
                       if version >= 11 and life == LifeState.GHOST and not state & 1024 and button else None)
            source = (state >> 2) & 7
            dialog = bool(state & 1024)
            recovery = RecoverySample(
                life, point(corpse) if state & 512 else None,
                point(button) if state & 4096 else None, aspect / 100000 if dialog else 0,
                dialog=dialog, hovered=bool(state & 2048),
                walk_verified=bool(state & 256) if version >= 11 else None, facing=heading,
                corpse_source=CorpseSource(source) if state & 512 and source <= CorpseSource.MINIMAP_MAP
                else CorpseSource.UNKNOWN,
                corpse_distance=aspect / 10 if life == LifeState.GHOST and not dialog and 10 <= aspect <= 2000 else None,
            )
        else:
            healing = corpse if life == LifeState.ALIVE else 0
            pet_health = healing & 127
            handoff = life == LifeState.ALIVE and bool(state & 1024)
            recovery = RecoverySample(
                life, pet=PetState((state >> 2) & 3), pet_expected=bool(state & 512),
                follow=bool(state & 16), summon=bool(state & 32), revive=bool(state & 64),
                buff_missing=bool(state & 128), buff_unverified=bool(state & 256),
                pet_health=pet_health - 1 if 1 <= pet_health <= 101 else None,
                mend_verified=bool(healing & 128), mend_active=bool(healing & 256),
                mend_unusable=bool(healing & 512), mend_epoch=(healing >> 10) & 127,
                patrol_id=button if life == LifeState.ALIVE and not handoff and 1 <= aspect <= 7 else 0,
                patrol_danger=aspect if life == LifeState.ALIVE and not handoff and 1 <= aspect <= 7 else 0,
                attacker_count=aspect & 15 if handoff else 0,
                attacker_cursor=((button >> 10) / 1023, (button & 1023) / 1023) if handoff and button else None,
                attacker_hovered=bool(aspect & 16) if handoff else False,
                attacker_scan_supported=handoff,
            )
    loot_epoch, sweep_ready, search_area = None, False, None
    camera_restore_available, camera_zoom_close, camera_epoch = False, False, 0
    if version >= 12:
        if offset < 17 * row_height:
            return None
        receipt = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 12 * row_height))
        if not receipt or receipt[0] != 9 or ((receipt[1] >> 10) & 127) != sequence:
            return None
        loot_epoch, sweep_ready = receipt[1] & 255, bool(receipt[1] & 256)
        camera_restore_available = bool(receipt[1] & (1 << 17))
        camera_zoom_close = camera_restore_available and bool(receipt[1] & (1 << 18))
        camera_epoch = receipt[1] >> 19
        if receipt[1] & 512 and meta & 128 and width and depth and goal_map == map_id:
            rows = [spec.decode_frame(spec.sample_nibbles(image, 0, offset - i * row_height))
                    for i in (17, 16, 15, 14, 13)]
            if any(not row or row[0] != index + 4 or row[1] >> 16 != sequence % 16
                   for index, row in enumerate(rows)):
                return None
            points, index = [], 0
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if dx or dy:
                        if rows[index // 16][1] & (1 << (index % 16)):
                            point = gx / 1048575 + dx * 300 / width, gy / 1048575 + dy * 300 / depth
                            if all(0 <= p <= 1 for p in point):
                                points.append(point)
                        index += 1
            search_area = tuple(points)
    sky_flight = None
    if version >= 13:
        if offset < 19 * row_height:
            return None
        skills = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 19 * row_height))
        motion = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 18 * row_height))
        if (not skills or skills[0] != 10 or skills[1] >> 13 != sequence
                or not motion or motion[0] != 11 or motion[1] >> 13 != sequence):
            return None
        if flags & TravelFlags.SKYRIDING:
            keys = tuple(str(key) if 1 <= key <= 9 else None
                         for key in ((skills[1] >> shift) & 15 for shift in (0, 4, 8)))
            speed, charges = motion[1] & 31, (motion[1] >> 5) & 7
            casts = {1: "surge", 2: "second_wind", 3: "whirling", 4: "ascent", 5: "halt"}
            sky_flight = SkyFlight(*keys, speed=(speed - 1) * 4 if speed else None,
                                   charges=charges - 1 if charges else None,
                                   cast=casts.get((motion[1] >> 8) & 7), cast_epoch=(motion[1] >> 11) & 3)
    objective_index, suspend_epoch = None, None
    objectives_finished = objective_resource = suspend_ready = current_suspended = False
    objective_progress = (0, 0)
    if version >= 14:
        if offset < 21 * row_height:
            return None
        objective_row = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 20 * row_height))
        progress_row = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 21 * row_height))
        if (not objective_row or objective_row[0] != 12 or objective_row[1] >> 13 != sequence
                or not progress_row or progress_row[0] != sequence % 16):
            return None
        value = objective_row[1]
        objective_index, current_suspended = value & 63, bool(value & 64)
        objectives_finished, objective_resource = bool(value & 256), bool(value & 128)
        objective_progress = divmod(progress_row[1], 1024)
        if objective_resource and (objective_progress[1] != 1000 or objective_progress[0] > 1000):
            return None
        suspend_ready, suspend_epoch = bool(value & 512), (value >> 10) & 7
    camera = None
    if version >= 15:
        if offset < 23 * row_height:
            return None
        reading = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 22 * row_height))
        state = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 23 * row_height))
        if (not reading or reading[0] != 13 or reading[1] >> 13 != sequence
                or not state or state[0] != 14 or state[1] >> 13 != sequence):
            return None
        zoom, value = reading[1] & 127, state[1]
        try:
            camera = CameraSample((zoom - 1) / 2 if zoom else None, (reading[1] >> 10) & 7,
                                  CameraScene(value & 15), CameraScene((value >> 4) & 15),
                                  (value >> 8) & 7, CameraResult((reading[1] >> 7) & 7),
                                  bool(value & 2048), bool(value & 4096))
        except ValueError:
            return None
    addon_build = None
    probe_camera_available = False
    overhead_camera_available = False
    calibration_orbit_available = calibration_orbit_active = False
    round_pending_count = extension_schema = None
    if version >= 15 and offset >= 25 * row_height:
        schema = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 25 * row_height))
        build = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 24 * row_height))
        schema_version = schema[1] & 524287 if schema and schema[0] == 15 else None
        round_schema = schema_version is not None and (schema_version & ~511) == 357888
        automatic_schema = round_schema or schema_version is not None and (schema_version & ~3) == 357384
        schema_valid = automatic_schema or schema_version == 357380 or (
            schema_version in {357377, 357378, 357379} and schema[1] == schema_version)
        if schema_valid and build and build[0] == 14 and (build[1] >> 12) & 127 == sequence:
            addon_build = build[1] & 4095 or None
            extension_schema = 6 if round_schema else 5 if automatic_schema else schema_version - 357376
            round_pending_count = (schema_version >> 2) & 127 if round_schema else None
            # Old F9 capability cannot authorize the new F6 binding.
            probe_camera_available = (automatic_schema or schema_version in {357379, 357380}) and bool(build[1] & 524288)
            overhead_camera_available = ((automatic_schema or schema_version == 357380) and probe_camera_available
                                         and bool(schema[1] & 524288))
            calibration_orbit_available = bool(automatic_schema and overhead_camera_available and schema_version & 1)
            calibration_orbit_active = bool(calibration_orbit_available and schema_version & 2)
    visual_ui_hidden = None
    if addon_build is not None and addon_build >= 274 and offset >= 26 * row_height:
        ui = spec.decode_frame(spec.sample_nibbles(image, 0, offset - 26 * row_height))
        if (ui and ui[0] == 13 and ui[1] >> 14 == 45 and (ui[1] >> 4) & 7 == 1
                and (ui[1] >> 7) & 127 == sequence):
            visual_ui_hidden = ui[1] & 15
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
        loot_epoch, sweep_ready, search_area, sky_flight, camera_restore_available, camera_zoom_close, camera_epoch,
        objective_index, objectives_finished, objective_progress, objective_resource, suspend_ready, suspend_epoch,
        current_suspended,
        version, camera, addon_build, probe_camera_available, overhead_camera_available,
        calibration_orbit_available,calibration_orbit_active,
        round_pending_count, extension_schema, visual_ui_hidden,
        choice_step,
    )
