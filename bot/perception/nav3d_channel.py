"""Independent Nav3D schema 1. Public Z and verified physical Z are distinct."""
from dataclasses import dataclass
import math
import numpy as np
from . import channel_spec as spec

ROWS=26

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


def nav3d_region(x,y):return x+288,y-ROWS*16,142,ROWS*16


def decode_nav3d(image):
    if image.shape!=(ROWS*16,142,3):return None
    values=[];sequences=[]
    for i in range(ROWS):
        row=spec.decode_frame(spec.sample_nibbles(image,0,i*16))
        if row is None or row[0]!=i%16:return None
        values.append(row[1]&4095);sequences.append(row[1]>>12)
    if len(set(sequences))!=1 or values[0]!=0xD01:return None
    def pair(i):return values[i]+values[i+1]*4096
    def signed(i):
        v=pair(i);return (v-(1<<24) if v&(1<<23) else v)/100
    f=values[1]
    if values[17]>7:return None
    return Nav3DSample(sequences[0],pair(2),pair(4),pair(6),
        (signed(8),signed(10)) if f&1 else None,signed(12) if f&2 else None,
        values[14]*math.tau/4096 if f&4 else None,pair(15),values[17],
        pair(18) if f&8 else None,(signed(20),signed(22)) if f&8 else None,
        bool(f&16),bool(f&32),bool(f&64),pair(24),
        bool(f&2048),bool(f&128),bool(f&256),bool(f&512),bool(f&1024))
