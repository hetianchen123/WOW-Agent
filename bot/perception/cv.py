"""Small OpenCV helpers: template matching, frame diff, snapshots."""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Tuple

import cv2
import numpy as np

from ..core.paths import SCREENSHOT_DIR

log = logging.getLogger(__name__)


@dataclass
class Match:
    x: int  # top-left of match in the searched ROI
    y: int
    w: int
    h: int
    score: float
    template: str


def load_template(name: str, template_dir: Path) -> Optional[np.ndarray]:
    for ext in (".png", ".jpg"):
        p = Path(template_dir) / f"{name}{ext}"
        if p.exists():
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
            return None if img is None else img[:, :, ::-1]  # RGB
    log.debug("template missing: %s in %s", name, template_dir)
    return None


def to_gray(img_rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)


def match_best(
    frame_rgb: np.ndarray,
    template_rgb: np.ndarray,
    threshold: float = 0.8,
    scales: Iterable[float] = (1.0,),
    template_name: str = "",
) -> Optional[Match]:
    """Multi-scale normalized cross-correlation; returns best match above threshold."""
    if frame_rgb.size == 0 or template_rgb.size == 0:
        return None
    frame = to_gray(frame_rgb)
    best: Optional[Match] = None
    th, tw = template_rgb.shape[:2]
    for s in scales:
        t = template_rgb if s == 1.0 else cv2.resize(template_rgb, (int(tw * s), int(th * s)), interpolation=cv2.INTER_AREA)
        tg = to_gray(t)
        ths, tws = tg.shape
        if ths >= frame.shape[0] or tws >= frame.shape[1]:
            continue
        res = cv2.matchTemplate(frame, tg, cv2.TM_CCOEFF_NORMED)
        _, max_v, _, max_loc = cv2.minMaxLoc(res)
        if max_v >= threshold and (best is None or max_v > best.score):
            best = Match(max_loc[0], max_loc[1], tws, ths, float(max_v), template_name)
    return best


def absdiff_score(a_rgb: np.ndarray, b_rgb: np.ndarray) -> float:
    """Mean absolute difference of grayscale images (0..255)."""
    if a_rgb.shape != b_rgb.shape or a_rgb.size == 0:
        return 255.0
    return float(
        np.mean(cv2.absdiff(to_gray(a_rgb), to_gray(b_rgb)))
    )


def player_bar_percent(rgb: np.ndarray, *, mana: bool = False) -> int | None:
    """Read the contiguous fill of an already identified native player bar."""
    if rgb.ndim != 3 or rgb.shape[0] < 3 or rgb.shape[1] < 30 or rgb.shape[2] != 3:
        return None
    pixels = rgb[:, 2:-2].astype(np.int16)
    red, green, blue = np.moveaxis(pixels, 2, 0)
    fill, other = (blue, green) if mana else (green, blue)
    colored = np.mean((fill >= 70) & (fill - red >= 25) & (fill - other >= 20), axis=0) >= .66
    empty = np.mean(np.max(pixels, axis=2) < 95, axis=0) >= .66
    indices = np.flatnonzero(colored)
    if len(indices) < 4 or indices[0] > 4:
        return None
    start = int(indices[0])
    end = int(indices[-1]) + 1
    if np.count_nonzero(~colored[start:end]) > 3 or np.count_nonzero(~empty[end + 2:]) > 3:
        return None
    return 100 if end == colored.size else min(100, math.ceil(100 * (end + 3) / rgb.shape[1]))


def hostile_nameplate_points(frame_rgb: np.ndarray) -> tuple[tuple[int, int], ...]:
    """Centers of red health-bar shapes in the world view, in capture pixels."""
    if frame_rgb.size == 0:
        return ()
    height, width = frame_rgb.shape[:2]
    x0, x1 = round(.10 * width), round(.78 * width)
    y0, y1 = round(.13 * height), round(.61 * height)
    hsv = cv2.cvtColor(frame_rgb[y0:y1, x0:x1], cv2.COLOR_RGB2HSV)
    red = cv2.inRange(hsv, (0, 145, 85), (9, 255, 255))
    red |= cv2.inRange(hsv, (174, 145, 85), (179, 255, 255))
    red[round(.38 * height) - y0:, :round(.29 * width) - x0] = 0
    kernel_width = max(3, round(width * .004)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1))
    closed = cv2.morphologyEx(red, cv2.MORPH_CLOSE, kernel)
    _, _, stats, _ = cv2.connectedComponentsWithStats(closed)
    points = []
    for x, y, w, h, area in stats[1:]:
        if (.020 * width <= w <= .145 * width and 3 <= h <= .025 * height
                and 5 <= w / h <= 36 and area / (w * h) >= .68):
            points.append((int(x0 + x + w / 2), int(y0 + y + h / 2)))
    return tuple(sorted(points, key=lambda p: math.hypot(p[0] - width * .5, p[1] - height * .43)))


def pumpkin_probe_points(
    frame_rgb: np.ndarray, excluded: Iterable[tuple[float, float, float, float]] = (),
) -> tuple[tuple[float, float], ...]:
    """Find red-orange shapes to hover over. The game still identifies the object."""
    if frame_rgb.size == 0:
        return ()
    height, width = frame_rgb.shape[:2]
    width = round(width * 720 / height)
    height = 720
    frame = cv2.resize(frame_rgb, (width, height), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    red = cv2.inRange(hsv, (0, 95, 56), (9, 255, 240))
    red |= cv2.inRange(hsv, (172, 95, 56), (179, 255, 240))
    orange = cv2.inRange(hsv, (0, 65, 48), (19, 235, 240))
    orange |= cv2.inRange(hsv, (165, 65, 48), (179, 235, 240))
    for left, top, right, bottom in excluded:
        x0, x1 = max(0, round(left * width)), min(width, round(right * width))
        y0, y1 = max(0, round(top * height)), min(height, round(bottom * height))
        if x0 < x1 and y0 < y1:
            red[y0:y1, x0:x1] = orange[y0:y1, x0:x1] = 0

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 5))
    points = []
    # Separate red objects from warm soil before trying the broader orange mask.
    for mask in (red, orange):
        components = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(components)
        distances = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
        candidates = []
        for index, (x, y, w, h, area) in enumerate(stats[1:], 1):
            if not (9 <= w <= 90 and 7 <= h <= 75 and .55 <= w / h <= 2.8
                    and area >= max(70, .33 * w * h)):
                continue
            region = np.s_[y:y + h, x:x + w]
            core = np.where(labels[region] == index, distances[region], 0)
            py, px = np.unravel_index(np.argmax(core), core.shape)
            compactness = area / max(w, h) ** 2
            candidates.append((area * compactness, (int(x + px), int(y + py))))
        for _, point in sorted(candidates, reverse=True)[:6]:
            if all(abs(point[0] - px) > 6 or abs(point[1] - py) > 6 for px, py in points):
                points.append(point)
    return tuple((x / width, y / height) for x, y in points)


def minimap_quest_dots(frame_rgb: np.ndarray) -> tuple[tuple[float, float], ...]:
    """Gold dots inside the minimap, expressed as fractions of its radius."""
    if frame_rgb.size == 0:
        return ()
    size = 192
    frame = cv2.resize(frame_rgb, (size, size), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    gold = cv2.inRange(hsv, (20, 145, 182), (35, 255, 255))
    _, _, stats, centers = cv2.connectedComponentsWithStats(gold)
    radius = size / 2
    dots = []
    for (_, _, width, height, area), (x, y) in zip(stats[1:], centers[1:]):
        dx, dy = float(x) / radius - 1, float(y) / radius - 1
        if (3 <= width <= 12 and 3 <= height <= 12 and 8 <= area <= 100
                and .5 <= width / height <= 2 and dx * dx + dy * dy <= .80 ** 2):
            dots.append((dx, dy))
    return tuple(dots)


@lru_cache(maxsize=1)
def _corpse_minimap_template():
    path = Path(__file__).with_name("assets") / "corpse_minimap.png"
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    return cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA) if image is not None else None


def minimap_corpse_pin(frame_rgb: np.ndarray) -> tuple[float, float] | None:
    """Return the tombstone offset in minimap-radius units for a unique match."""
    if frame_rgb.size == 0:
        return None
    height, width = frame_rgb.shape[:2]
    if min(height, width) < 70 or not .85 <= width / height <= 1.15:
        return None
    template = _corpse_minimap_template()
    if template is None:
        return None
    th, tw = template.shape[:2]
    scale = min(height, width) / 194
    peaks = []
    for size in sorted({(round(tw * scale * factor), round(th * scale * factor))
                        for factor in (.75, .85, .925, 1, 1.075, 1.16, 1.26, 1.4)}):
        w, h = size
        icon = cv2.resize(template, size, interpolation=cv2.INTER_NEAREST)
        errors = cv2.matchTemplate(frame_rgb, icon[:, :, :3], cv2.TM_SQDIFF_NORMED, mask=icon[:, :, 3])
        ys, xs = np.ogrid[:errors.shape[0], :errors.shape[1]]
        dx, dy = (xs + w / 2) / (width / 2) - 1, (ys + h / 2) / (height / 2) - 1
        errors[~np.isfinite(errors) | (dx * dx + dy * dy > .86 ** 2)] = 1
        for _ in range(2):
            error, _, (x, y), _ = cv2.minMaxLoc(errors)
            if error > .12:
                break
            peaks.append((error, float(dx[0, x]), float(dy[y, 0])))
            errors[(dx - dx[0, x]) ** 2 + (dy - dy[y, 0]) ** 2 <= .13 ** 2] = 1
    if not peaks:
        return None
    error, x, y = min(peaks)
    if error > .085 or any(other - error < .035 and (dx - x) ** 2 + (dy - y) ** 2 > .13 ** 2
                           for other, dx, dy in peaks):
        return None
    return x, y


@lru_cache(maxsize=1)
def _corpse_arrow_templates():
    image = cv2.imread(str(Path(__file__).with_name("assets") / "corpse_arrow.png"), cv2.IMREAD_UNCHANGED)
    if image is None:
        return ()
    image = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)
    canvas = np.zeros((34, 34, 4), np.uint8)
    h, w = image.shape[:2]
    canvas[(34-h)//2:(34-h)//2+h, (34-w)//2:(34-w)//2+w] = image
    return tuple((math.radians(135 - angle), cv2.warpAffine(
        canvas, cv2.getRotationMatrix2D((17, 17), angle, 1), (34, 34)))
        for angle in range(0, 360, 15))


def minimap_corpse_bearing(frame_rgb: np.ndarray) -> tuple[float, float] | None:
    """Recognize the red edge arrow; return direction only, never a distance."""
    if frame_rgb.size == 0:
        return None
    height, width = frame_rgb.shape[:2]
    if min(height, width) < 70 or not .85 <= width / height <= 1.15:
        return None
    red, green, blue = (frame_rgb[:, :, i].astype(float) for i in range(3))
    mask = np.uint8((red > 110) & (red > green * 1.5) & (red > blue * 1.5))
    _, _, stats, centers = cv2.connectedComponentsWithStats(mask)
    directions = []
    for (_, _, w, h, area), (x, y) in zip(stats[1:], centers[1:]):
        dx, dy = x / (width / 2) - 1, y / (height / 2) - 1
        scale = min(height, width) / 198
        if .72 <= math.hypot(dx, dy) <= .96 and 25*scale**2 <= area <= 300*scale**2 and min(w, h) >= 6*scale:
            directions.append(math.atan2(dy, dx))
    if not directions:
        return None
    peaks = []
    for angle, template in _corpse_arrow_templates():
        if not any(math.cos(angle - direction) >= .94 for direction in directions):
            continue
        for factor in (.85, 1., 1.15):
            size = round(34 * min(height, width) / 198 * factor)
            icon = cv2.resize(template, (size, size))
            mask = np.uint8(icon[:, :, 3] > 180) * 255
            errors = cv2.matchTemplate(frame_rgb, icon[:, :, :3], cv2.TM_SQDIFF_NORMED, mask=mask)
            ys, xs = np.ogrid[:errors.shape[0], :errors.shape[1]]
            dx, dy = (xs + size / 2) / (width / 2) - 1, (ys + size / 2) / (height / 2) - 1
            radius = np.sqrt(dx * dx + dy * dy)
            alignment = (dx * math.cos(angle) + dy * math.sin(angle)) / np.maximum(radius, .01)
            errors[~np.isfinite(errors) | (radius < .72) | (radius > .96) | (alignment < .94)] = 1
            error, _, (x, y), _ = cv2.minMaxLoc(errors)
            if error < .10:
                peaks.append((error, float(dx[0, x]), float(dy[y, 0])))
    if not peaks:
        return None
    error, x, y = min(peaks)
    if any(other - error < .025 and (dx-x)**2 + (dy-y)**2 > .18**2 for other, dx, dy in peaks):
        return None
    radius = math.hypot(x, y)
    return x / radius, y / radius


def save_snapshot(img_rgb: np.ndarray, tag: str, directory: Path = SCREENSHOT_DIR) -> Optional[Path]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png"
        cv2.imwrite(str(path), img_rgb[:, :, ::-1])  # RGB -> BGR
        return path
    except Exception:  # noqa: BLE001
        log.exception("snapshot save failed")
        return None
