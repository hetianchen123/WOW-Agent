"""ChannelReader: decodes WowAgent pixel-block frames from screenshots and
exposes the latest values with freshness checks."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import channel_spec as spec
from ..core.config import ChannelConfig

log = logging.getLogger(__name__)


@dataclass
class FastEvent:
    kind: int  # spec.FastEvKind
    seq: int
    payload: int
    t: float


class ChannelReader:
    def __init__(self, cfg: ChannelConfig) -> None:
        self.cfg = cfg
        self._latest: Dict[int, Tuple[int, float]] = {}
        self.frames_ok = 0
        self.frames_bad = 0
        self.last_frame_t = 0.0
        self.events: List[FastEvent] = []  # fastev ring, newest last
        self._last_seq: Optional[int] = None
        self._last_status_flags = 0

    # ------------------------------------------------------------------ core
    def update(self, img_rgb: np.ndarray) -> List[Tuple[int, int]]:
        """Decode one frame from a cropped 142x16 channel-region image."""
        accepted: List[Tuple[int, int]] = []
        nibbles = spec.sample_nibbles(img_rgb, 0, 0)
        decoded = spec.decode_frame(nibbles)
        if decoded is None:
            self.frames_bad += 1
            return accepted
        mtype, value = decoded
        self.frames_ok += 1
        self.last_frame_t = time.time()
        self._latest[mtype] = (value, self.last_frame_t)
        accepted.append((mtype, value))
        if mtype == spec.MSG_FASTEV:
            self._consume_fastev(value)
        return accepted

    def _consume_fastev(self, value: int) -> None:
        kind, seq, payload = spec.unpack_fastev(value)
        if kind == spec.FastEvKind.NONE:
            self._last_seq = seq  # keep sequence tracking even on empty slots
            return
        if self._last_seq is not None:
            gap = (seq - self._last_seq) % 32
            if 0 < gap <= 30:  # 31/0 wraps or retransmits treated as dup
                if gap > 1:
                    log.debug("fastev gap: expected seq+%d", gap)
        self._last_seq = seq
        self.events.append(FastEvent(kind, seq, payload, time.time()))
        if len(self.events) > 64:
            del self.events[: len(self.events) - 64]

    # ------------------------------------------------------------ freshness
    def _value(self, mtype: int, max_age_s: float = 2.5) -> Optional[int]:
        got = self._latest.get(mtype)
        if not got:
            return None
        value, t = got
        if time.time() - t > max_age_s:
            return None
        return value

    def is_alive(self, max_age_s: float = 2.0) -> bool:
        return self.last_frame_t > 0 and (time.time() - self.last_frame_t) <= max_age_s

    def error_rate(self) -> float:
        total = self.frames_ok + self.frames_bad
        return self.frames_bad / total if total else 1.0

    # ------------------------------------------------------------- getters
    def pos(self, max_age_s: float = 2.5) -> Optional[Tuple[float, float]]:
        v = self._value(spec.MSG_POS, max_age_s)
        return spec.unpack_pos(v) if v is not None else None

    def heading(self) -> Optional[float]:
        v = self._value(spec.MSG_HDG)
        return spec.unpack_hdg(v) if v is not None else None

    def status(self, max_age_s: float = 4.0) -> Optional[Tuple[spec.StatusFlags, int]]:
        v = self._value(spec.MSG_STATUS, max_age_s)
        if v is None:
            return None
        flags, hp_bucket = spec.unpack_status(v)
        # heartbeat must toggle between consecutive STATUS frames
        beat_on = bool(flags & spec.StatusFlags.HEARTBEAT)
        prev = bool(self._last_status_flags & spec.StatusFlags.HEARTBEAT)
        if beat_on != prev:
            self._hb_ok_t = time.time()
        self._last_status_flags = int(flags)
        return flags, hp_bucket

    def bagsum(self, max_age_s: float = 3.0) -> Optional[Tuple[int, int]]:
        v = self._value(spec.MSG_BAGSUM, max_age_s)
        return spec.unpack_bagsum(v) if v is not None else None

    def gold(self, max_age_s: float = 60.0) -> Optional[int]:
        v = self._value(spec.MSG_GOLD, max_age_s)
        return v

    def zone(self, max_age_s: float = 120.0) -> Optional[int]:
        return self._value(spec.MSG_ZONE, max_age_s)

    def bag_item(self, idx: int, max_age_s: float = 120.0) -> Optional[int]:
        v = self._value(spec.MSG_BAG, max_age_s)
        if v is None:
            return None
        got_idx, count = (v >> 16) & 0xF, v & 0xFFFF
        return count if got_idx == idx else None

    def pop_events(self, since_idx: int = 0) -> List[FastEvent]:
        """Return events past the given index (caller keeps an index cursor)."""
        return self.events[since_idx:]
