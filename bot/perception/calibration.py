"""Find the main WowAgent row and verify that its heartbeat is live."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import channel_spec as spec

Rect = tuple[int, int, int, int]

# macOS display profiles can add blue to otherwise valid red/green blocks.
LEVEL_TOLERANCE = 38
BLUE_LIMIT = 64


class ChannelNotFound(ValueError):
    """No live channel was found within the scan window."""


@dataclass(frozen=True)
class Location:
    monitor: int
    x: int
    y: int

    @property
    def region(self) -> Rect:
        return self.x, self.y, spec.REGION_W, spec.REGION_H


@dataclass
class Evidence:
    frames: int = 0
    kinds: set[int] = field(default_factory=set)
    beats: set[bool] = field(default_factory=set)

    def record(self, frame: tuple[int, int]) -> None:
        kind, value = frame
        self.frames += 1
        self.kinds.add(kind)
        if kind == spec.MSG_STATUS:
            flags, _ = spec.unpack_status(value)
            self.beats.add(bool(flags & spec.StatusFlags.HEARTBEAT))

    @property
    def verified(self) -> bool:
        return self.frames >= 8 and len(self.kinds) >= 3 and len(self.beats) == 2


def read_row(img: np.ndarray, x: int = 0, y: int = 0) -> tuple[int, int] | None:
    if x < 0 or y < 0 or y + spec.REGION_H > img.shape[0] or x + spec.REGION_W > img.shape[1]:
        return None
    nibbles = spec.sample_nibbles(img, x, y)
    frame = spec.decode_frame(nibbles)
    if frame is None or frame[0] not in spec.MSG_NAMES:
        return None
    for index, nibble in enumerate(nibbles):
        mid_x = x + spec.BLOCK // 2 + index * spec.STRIDE
        mid_y = y + spec.BLOCK // 2
        patch = img[mid_y - 2:mid_y + 2, mid_x - 2:mid_x + 2]
        if (np.abs(patch.astype(np.int16) - spec.nibble_to_rgb(nibble))
                > (LEVEL_TOLERANCE, LEVEL_TOLERANCE, BLUE_LIMIT)).any():
            return None
    return frame


def find_rows(img: np.ndarray) -> list[tuple[int, int]]:
    r, g, _ = spec.nibble_to_rgb(spec.SYNC)
    mask = cv2.inRange(img, (r - LEVEL_TOLERANCE, g - LEVEL_TOLERANCE, 0),
                       (r + LEVEL_TOLERANCE, g + LEVEL_TOLERANCE, BLUE_LIMIT))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((7, 7), dtype=np.uint8))
    _, _, components, _ = cv2.connectedComponentsWithStats(mask)
    rows = []
    for x, y, width, height, area in components[1:].tolist():
        if not (spec.BLOCK - 2 <= width <= spec.BLOCK + 2
                and height >= spec.BLOCK - 2 and area >= .9 * width * height):
            continue
        x += (width - spec.BLOCK) // 2
        # Quest and combat rows can share a continuous sync column above the main row.
        extra_height = height - spec.BLOCK
        y += extra_height if extra_height > 2 else extra_height // 2
        if read_row(img, x, y) is not None:
            rows.append((x, y))
    return rows


def startup_location(capture, previous: Location, *, scan_duration: float = 5) -> Location:
    """Verify the saved row or find its new position before enabling input."""
    monitors = capture.monitor_regions()
    if 1 <= previous.monitor <= len(monitors):
        left, top, width, height = monitors[previous.monitor - 1]
        if (left <= previous.x <= left + width - spec.REGION_W
                and top <= previous.y <= top + height - spec.REGION_H):
            evidence = Evidence()
            deadline = time.monotonic() + 1.25
            while time.monotonic() < deadline:
                frame = read_row(capture.grab(*previous.region))
                if frame is None:
                    break
                evidence.record(frame)
                if evidence.verified:
                    return previous
                time.sleep(.018)
    confirmed = locate_channel(capture, monitors, scan_duration)
    if not confirmed:
        raise ChannelNotFound("找不到正在更新的 WowAgent 主色条。请确认魔兽窗口可见，且插件主色条已开启。")
    if len(confirmed) != 1:
        raise ValueError("检测到多个 WowAgent 主色条。请只保留当前任务角色的游戏窗口可见。")
    return next(iter(confirmed))


def locate_channel(capture, monitors: list[Rect], duration: float) -> dict[Location, Evidence]:
    deadline = time.monotonic() + duration
    evidence: dict[Location, Evidence] = {}
    while time.monotonic() < deadline:
        found = {}
        for monitor, region in enumerate(monitors, 1):
            left, top, _, _ = region
            for x, y in find_rows(capture.grab(*region)):
                location = Location(monitor, left + x, top + y)
                found[location] = evidence.get(location, Evidence())
        evidence = found
        poll_until = min(deadline, time.monotonic() + (1.25 if evidence else .35))
        while evidence and time.monotonic() < poll_until:
            for location in evidence:
                frame = read_row(capture.grab(*location.region))
                if frame is None:
                    evidence[location] = Evidence()
                else:
                    evidence[location].record(frame)
            time.sleep(.018)
        confirmed = {location: state for location, state in evidence.items() if state.verified}
        if confirmed:
            return confirmed
        if not evidence:
            time.sleep(max(0, poll_until - time.monotonic()))
    return {}
