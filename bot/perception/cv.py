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


def save_snapshot(img_rgb: np.ndarray, tag: str, directory: Path = SCREENSHOT_DIR) -> Optional[Path]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png"
        cv2.imwrite(str(path), img_rgb[:, :, ::-1])  # RGB -> BGR
        return path
    except Exception:  # noqa: BLE001
        log.exception("snapshot save failed")
        return None
