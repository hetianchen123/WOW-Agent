"""WowAgent pixel-channel protocol — single source of truth (mirrored in Lua).

Layout
------
The addon renders 8 color blocks of 16x16 px with 2 px gaps (total 142x16 px)
at a fixed screen position. Each block encodes one nibble (4 bits):

    block 0: SYNC constant 0xA
    block 1: message TYPE (see MSG_* below)
    block 2..6: 20-bit payload, big-endian nibble order
    block 7: checksum = (type + data nibbles) & 0xF

Nibble -> color: high 2 bits -> RED channel level (0/85/170/255),
low 2 bits -> GREEN channel level, BLUE = 0. Decoding quantizes each channel
to the nearest of the four levels, tolerating +-42 per channel.

Schedule (tick counter at 20 Hz; SCHEDULE[tick % 20] -> message kind)
----------------------------------------------------------------------
    POS      player map position, 7 Hz   (steering loop)
    HDG      player facing,     6 Hz
    BAGSUM   free slots + total tracked item count, 2 Hz (loot/post verify)
    FASTEV   event queue (cast start/done, ui error, ah post), 2 Hz
    STATUS   combat/dead/mounted/... flags + hp, 2 Hz
    SLOWROT  rotating: zone, gold, per-item counts, ~1 Hz step

Payload packing per type
------------------------
POS      qx(10b)<<10 | qy(10b)              qx = round(x*1023), x = map fraction
HDG      round(facing / 2pi * 1048575)
STATUS   flags(16b) | hp_bucket(4b)         see StatusFlags
BAGSUM   free_slots(8b)<<12 | tracked_total(12b)
FASTEV   kind(3b)<<17 | seq(5b)<<12 | payload(12b)
SLOWROT  zone: uiMapID (20b)
         gold: gold coins (20b)
         bag_i: item_index(4b)<<16 | count(16b)
"""
from __future__ import annotations

import enum
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

# ------------------------------------------------------------- geometry ----
BLOCK = 16
GAP = 2
N_BLOCKS = 8
REGION_W = N_BLOCKS * BLOCK + (N_BLOCKS - 1) * GAP  # 142
REGION_H = BLOCK
STRIDE = BLOCK + GAP  # 18

SYNC = 0xA

# ------------------------------------------------------------ message ids ----
MSG_POS = 0x0
MSG_HDG = 0x1
MSG_STATUS = 0x2
MSG_BAG = 0x3
MSG_GOLD = 0x4
MSG_ERR = 0x5
MSG_CAST = 0x6
MSG_ZONE = 0x7
MSG_BAGSUM = 0x8
MSG_FASTEV = 0x9

MSG_NAMES = {
    MSG_POS: "POS", MSG_HDG: "HDG", MSG_STATUS: "STATUS", MSG_BAG: "BAG",
    MSG_GOLD: "GOLD", MSG_ERR: "ERR", MSG_CAST: "CAST", MSG_ZONE: "ZONE",
    MSG_BAGSUM: "BAGSUM", MSG_FASTEV: "FASTEV",
}

CHANNEL_FPS = 20
SLOWROT_LEN = 2 + 16  # ZONE, GOLD, BAG_0..BAG_15


def schedule_kind(tick: int) -> Optional[int]:
    """Message type to send on the given tick (mirrors addon Lua).

    Returns None for tick%20 == 19: that slot belongs to the slow rotation
    (ZONE / GOLD / per-item counts) — see slowrot_index().
    """
    t = tick % 20
    if t in (7, 13):
        return MSG_FASTEV
    if t in (10, 17):
        return MSG_STATUS
    if t in (4, 14):
        return MSG_BAGSUM
    if t == 19:
        return None
    return MSG_POS if t % 2 == 0 else MSG_HDG


# Slot 19 belongs to the slow rotation; helper returns the rotation index.
def slowrot_index(tick: int) -> int:
    return (tick // 20) % SLOWROT_LEN


# --------------------------------------------------------------- colors ----
_LEVELS = (0, 85, 170, 255)


def nibble_to_rgb(v: int) -> Tuple[int, int, int]:
    hi, lo = (v >> 2) & 3, v & 3
    return (_LEVELS[hi], _LEVELS[lo], 0)


def rgb_to_nibble(r: float, g: float) -> int:
    hi = min(3, max(0, int(round(r / 85.0))))
    lo = min(3, max(0, int(round(g / 85.0))))
    return (hi << 2) | lo


# -------------------------------------------------------------- framing ----
def encode_frame(mtype: int, value: int) -> List[int]:
    """Return 8 nibbles composing one frame."""
    value = int(value) & 0xFFFFF
    nibbles = [SYNC, mtype & 0xF]
    for shift in (16, 12, 8, 4, 0):
        nibbles.append((value >> shift) & 0xF)
    nibbles.append(sum(nibbles[1:7]) & 0xF)
    return nibbles


def decode_frame(nibbles: List[int]) -> Optional[Tuple[int, int]]:
    """Validate sync+checksum; return (type, value) or None."""
    if len(nibbles) != N_BLOCKS:
        return None
    if nibbles[0] != SYNC:
        return None
    if sum(nibbles[1:7]) & 0xF != nibbles[7]:
        return None
    value = 0
    for n in nibbles[2:7]:
        value = (value << 4) | (n & 0xF)
    return nibbles[1], value


# ----------------------------------------------------------- render/sample ----
def render_frame(img: "np.ndarray", x: int, y: int, nibbles: List[int]) -> None:
    """Paint the 8 blocks into an RGB uint8 image (synthetic/testing)."""
    for i, v in enumerate(nibbles):
        r, g, b = nibble_to_rgb(v)
        bx = x + i * STRIDE
        img[y : y + BLOCK, bx : bx + BLOCK] = (r, g, b)


def sample_nibbles(img: "np.ndarray", x: int, y: int) -> List[int]:
    """Average the central 4x4 px of each block and quantize back to nibbles."""
    nibbles: List[int] = []
    half = BLOCK // 2
    for i in range(N_BLOCKS):
        bx = x + i * STRIDE + half - 2
        by = y + half - 2
        patch = img[by : by + 4, bx : bx + 4]
        mean = patch.reshape(-1, 3).mean(axis=0) if patch.size else np.zeros(3)
        nibbles.append(rgb_to_nibble(mean[0], mean[1]))
    return nibbles


# ------------------------------------------------------------ status bits ----
class StatusFlags(enum.IntFlag):
    IN_COMBAT = 1 << 0
    DEAD = 1 << 1
    GHOST = 1 << 2
    MOUNTED = 1 << 3
    FLYING = 1 << 4
    SWIMMING = 1 << 5
    FALLING = 1 << 6
    CASTING = 1 << 7
    LOOT_OPEN = 1 << 8
    HAS_TARGET = 1 << 9
    MOVING = 1 << 10
    HEARTBEAT = 1 << 11  # toggles every STATUS send — addon liveness
    HP_UNKNOWN = 1 << 12  # hp bucket is unavailable when this bit is set


def pack_status(flags: int, hp_pct: float) -> int:
    bucket = max(0, min(15, int(round(hp_pct * 15.0 / 100.0))))
    return ((flags & 0xFFFF) << 4) | bucket


def unpack_status(value: int) -> Tuple[StatusFlags, int]:
    return StatusFlags(value >> 4), value & 0xF


# ------------------------------------------------------------- pack helpers ----
def pack_pos(x: float, y: float) -> int:
    qx = max(0, min(1023, int(round(x * 1023.0))))
    qy = max(0, min(1023, int(round(y * 1023.0))))
    return (qx << 10) | qy


def unpack_pos(value: int) -> Tuple[float, float]:
    return ((value >> 10) & 0x3FF) / 1023.0, (value & 0x3FF) / 1023.0


def pack_hdg(radians: float) -> int:
    two_pi = 2.0 * math.pi
    r = radians % two_pi
    return int(round(r / two_pi * 1048575.0)) & 0xFFFFF


def unpack_hdg(value: int) -> float:
    return value / 1048575.0 * 2.0 * math.pi


def pack_bagsum(free_slots: int, tracked_total: int) -> int:
    return (min(255, free_slots) << 12) | min(4095, tracked_total)


def unpack_bagsum(value: int) -> Tuple[int, int]:
    return (value >> 12) & 0xFF, value & 0xFFF


# ------------------------------------------------------------- fast events ----
class FastEvKind(enum.IntEnum):
    NONE = 0
    CAST_START = 1
    CAST_DONE = 2
    UI_ERROR = 3
    AH_POST = 4


def pack_fastev(kind: int, seq: int, payload: int) -> int:
    return ((kind & 0x7) << 17) | ((seq & 0x1F) << 12) | (payload & 0xFFF)


def unpack_fastev(value: int) -> Tuple[int, int, int]:
    return (value >> 17) & 0x7, (value >> 12) & 0x1F, value & 0xFFF
