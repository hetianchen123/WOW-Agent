"""Independent Nav3D schema 1. Public Z and verified physical Z are distinct."""
from dataclasses import dataclass, replace
import math
import numpy as np
from . import channel_spec as spec

ROWS=26
FAST_POSE_ROWS = 11

@dataclass(frozen=True)
class Nav3DSample:
    sequence: int
    world_id: int
    client_build: int
    ui_map_id: int
    position_xy: tuple[float,float] | None
    public_z: float | None
    heading: float | None
    quest_id: int
    phase: int
    goal_world_id: int | None
    goal_xy: tuple[float,float] | None
    grounded: bool
    bindings_verified: bool
    in_combat: bool
    travel_flags: int
    encounter_profile: bool = False
    encounter_selector_verified: bool = False
    encounter_target_matches: bool = False
    encounter_target_attackable: bool = False
    encounter_target_alive: bool = False


def nav3d_region(x, y, *, raised=False):
    return x + 288, y - ROWS * 16 - (256 if raised else 0), 142, ROWS * 16


@dataclass(frozen=True)
class FastPoseSample:
    sequence: int
    base_sequence: int
    world_id: int
    ui_map_id: int
    position_xy: tuple[float, float] | None
    heading: float | None
    grounded: bool
    bindings_verified: bool
    in_combat: bool


def fast_pose_region(x, y):
    width, height = spec.REGION_W, FAST_POSE_ROWS * spec.REGION_H
    return x, y - 480 - height, width, height


def packet(image, count, magic):
    if image.shape != (count * spec.REGION_H, spec.REGION_W, 3):
        return None
    values=[];sequences=[]
    for i in range(count):
        row=spec.decode_frame(spec.sample_nibbles(image,0,i*16))
        if row is None or row[0]!=i%16:return None
        values.append(row[1]&4095);sequences.append(row[1]>>12)
    if len(set(sequences)) != 1 or values[0] != magic:
        return None
    return sequences[0], values


def coordinate(value):
    return (value - (1 << 24) if value & (1 << 23) else value) / 100


def decode_fast_pose(image):
    decoded = packet(image, FAST_POSE_ROWS, 0xD21)
    if decoded is None:
        return None
    sequence, values = decoded
    flags = values[1]
    def pair(i): return values[i] + values[i+1] * 4096
    return FastPoseSample(sequence, flags & 127, pair(2), pair(4),
        (coordinate(pair(6)), coordinate(pair(8))) if flags & 1024 else None,
        values[10] * math.tau / 4096 if flags & 2048 else None,
        bool(flags & 128), bool(flags & 256), bool(flags & 512))


def with_fast_pose(base: Nav3DSample, fast: FastPoseSample | None) -> Nav3DSample | None:
    if fast is None or (fast.base_sequence, fast.world_id, fast.ui_map_id) != (
        base.sequence, base.world_id, base.ui_map_id
    ):
        return None
    return replace(base, sequence=fast.sequence, position_xy=fast.position_xy, heading=fast.heading,
                   grounded=base.grounded and fast.grounded,
                   bindings_verified=base.bindings_verified and fast.bindings_verified,
                   in_combat=base.in_combat or fast.in_combat)


def decode_nav3d(image):
    decoded = packet(image, ROWS, 0xD01)
    if decoded is None:
        return None
    sequence, values = decoded
    def pair(i):return values[i]+values[i+1]*4096
    def signed(i):return coordinate(pair(i))
    f=values[1]
    if values[17]>7:return None
    return Nav3DSample(sequence,pair(2),pair(4),pair(6),
        (signed(8),signed(10)) if f&1 else None,signed(12) if f&2 else None,
        values[14]*math.tau/4096 if f&4 else None,pair(15),values[17],
        pair(18) if f&8 else None,(signed(20),signed(22)) if f&8 else None,
        bool(f&16),bool(f&32),bool(f&64),pair(24),
        bool(f&2048),bool(f&128),bool(f&256),bool(f&512),bool(f&1024))
