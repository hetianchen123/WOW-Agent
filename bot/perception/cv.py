"""Small OpenCV helpers: template matching, frame diff, snapshots."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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


def save_snapshot(img_rgb: np.ndarray, tag: str, directory: Path = SCREENSHOT_DIR) -> Optional[Path]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png"
        cv2.imwrite(str(path), img_rgb[:, :, ::-1])  # RGB -> BGR
        return path
    except Exception:  # noqa: BLE001
        log.exception("snapshot save failed")
        return None
