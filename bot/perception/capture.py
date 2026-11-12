"""Screen capture backends: real screen (mss), image directory replay, and
synthetic generator (tests / selftest / dry-run)."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

from ..core.config import CaptureConfig

log = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    pass


def frontmost_game_bounds() -> tuple[float, float, float, float]:
    """Return the foremost WoW window in desktop coordinates."""
    import Quartz as q
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if not app or app.bundleIdentifier() != "com.blizzard.worldofwarcraft":
        raise ValueError("魔兽未在前台")
    pid = app.processIdentifier()
    windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID)
    for window in windows or ():
        bounds = window.get("kCGWindowBounds", {})
        if (window.get("kCGWindowOwnerPID") == pid and window.get("kCGWindowLayer") == 0
                and bounds.get("Width", 0) >= 800 and bounds.get("Height", 0) >= 500):
            return bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"]
    raise ValueError("未找到可用的魔兽窗口")


def game_ui_bounds(bounds, minimap):
    """Remove a top title bar using the stock square minimap as a scale reference."""
    if minimap is None:
        return bounds
    x, y, width, height = bounds
    map_width, map_height = minimap.size
    if min(map_width, map_height) <= 0:
        return bounds
    content_height = round(width * map_width / map_height)
    top_inset = height - content_height
    if 4 <= top_inset <= 64:
        return x, y + top_inset, width, content_height
    return bounds


class ScreenCapture:
    """Grabs screen regions as RGB numpy arrays via mss.

    Note: mss instances are not thread safe; use from the main control loop
    only. Coordinates live in the same space our calibrate tool reports.
    """

    def __init__(self, cfg: CaptureConfig) -> None:
        self.cfg = cfg
        self._mss = None
        self._monitor = None
        self._grab_count = 0
        self._t0 = 0.0

    def _ensure(self):
        if self._mss is None:
            import mss  # deferred: keeps import cheap for tests

            source = mss.mss()
            monitors: List[dict] = source.monitors
            if len(monitors) < 2:
                source.close()
                raise CaptureError("没有可截取的显示器")
            self._mss = source
            idx = self.cfg.monitor if 0 <= self.cfg.monitor < len(monitors) else 0
            self._monitor = monitors[idx]
            log.info("capture monitor: %s", self._monitor)
            self._t0 = time.time()

    def monitor_regions(self) -> list[tuple[int, int, int, int]]:
        """Desktop-coordinate regions in the same order as the configured monitor index."""
        self._ensure()
        return [(m["left"], m["top"], m["width"], m["height"]) for m in self._mss.monitors[1:]]

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        self._ensure()
        assert self._mss is not None
        x, y, w, h = int(x), int(y), int(w), int(h)
        if sys.platform == "darwin":
            rgb = self._grab_mac_srgb(x, y, w, h)
        else:
            shot = self._mss.grab({"left": x, "top": y, "width": w, "height": h})
            arr = np.asarray(shot, dtype=np.uint8)  # BGRA
            rgb = arr[:, :, :3][:, :, ::-1].copy()
        self._grab_count += 1
        return rgb

    @staticmethod
    def _grab_mac_srgb(x: int, y: int, w: int, h: int) -> np.ndarray:
        import Quartz as cg

        # Convert the CGImage before extracting bytes. Raw display colors can
        # shift across the pixel channel's nibble boundaries on other monitors.
        image = cg.CGWindowListCreateImage(
            cg.CGRectMake(x, y, w, h),
            cg.kCGWindowListOptionOnScreenOnly,
            cg.kCGNullWindowID,
            cg.kCGWindowImageBoundsIgnoreFraming
            | cg.kCGWindowImageShouldBeOpaque
            | cg.kCGWindowImageNominalResolution,
        )
        if image is None:
            raise CaptureError("CoreGraphics could not capture the screen region")

        width, height = cg.CGImageGetWidth(image), cg.CGImageGetHeight(image)
        srgb = cg.CGColorSpaceCreateWithName(cg.kCGColorSpaceSRGB)
        bitmap = cg.CGBitmapContextCreate(
            None, width, height, 8, width * 4, srgb, cg.kCGImageAlphaNoneSkipLast,
        )
        if bitmap is None:
            raise CaptureError("CoreGraphics could not create an sRGB bitmap")
        cg.CGContextDrawImage(bitmap, cg.CGRectMake(0, 0, width, height), image)
        pixels = cg.CGBitmapContextGetData(bitmap).as_buffer(width * height * 4)
        return np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()

    @property
    def size(self) -> tuple[int, int]:
        self._ensure()
        assert self._monitor is not None
        return int(self._monitor["width"]), int(self._monitor["height"])

    @property
    def fps(self) -> float:
        if self._grab_count < 2:
            return 0.0
        return (self._grab_count - 1) / max(1e-6, time.time() - self._t0)


class FileCapture:
    """Replays sorted PNG files — offline regression of recorded sessions."""

    def __init__(self, directory: Path) -> None:
        self.files = sorted(Path(directory).glob("*.png"))
        if not self.files:
            raise FileNotFoundError(f"no png files in {directory}")
        self._i = -1
        import cv2

        self._cv2 = cv2

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        self._i = (self._i + 1) % len(self.files)
        img = self._cv2.imread(str(self.files[self._i]), self._cv2.IMREAD_COLOR)
        if img is None:
            raise CaptureError(f"failed to read {self.files[self._i]}")
        img = img[:, :, ::-1]  # BGR -> RGB
        hh, ww = img.shape[:2]
        x, y = max(0, x), max(0, y)
        return img[y : min(hh, y + h), x : min(ww, x + w)].copy()


class SyntheticCapture:
    """Calls frame_fn(frame_index, t, rect) -> full RGB canvas each grab.
    The callback may use ``rect`` to advance only channel-region ticks."""

    def __init__(self, frame_fn: Callable[..., np.ndarray], t_fn: Optional[Callable[[], float]] = None) -> None:
        self.frame_fn = frame_fn
        self.t_fn = t_fn or time.time
        self._i = -1

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        self._i += 1
        full = self.frame_fn(self._i, self.t_fn(), (x, y, w, h))
        hh, ww = full.shape[:2]
        x, y = max(0, x), max(0, y)
        return full[y : min(hh, y + h), x : min(ww, x + w)].copy()
