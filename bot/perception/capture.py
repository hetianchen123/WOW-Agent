"""Screen capture backends: real screen (mss), image directory replay, and
synthetic generator (tests / selftest / dry-run)."""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

from ..core.config import CaptureConfig
from ..core.paths import DATA_DIR

log = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    pass


class MacWindowStream:
    """Latest complete sRGB frame from the same window and coordinate space."""

    def __init__(self, window_id: int, width: int, height: int, *, library: Path | None = None):
        self.lib = ctypes.CDLL(str(library or (DATA_DIR / "apps/libwowagent_capture.dylib")))
        size = ctypes.c_size_t
        self.lib.wa_capture_start.argtypes = (ctypes.c_uint32, size, size)
        self.lib.wa_capture_start.restype = ctypes.c_void_p
        self.lib.wa_capture_copy.argtypes = (ctypes.c_void_p, size, size, size, size, ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_uint64))
        self.lib.wa_capture_copy.restype = ctypes.c_int
        self.lib.wa_capture_stop.argtypes = (ctypes.c_void_p,)
        self.lib.wa_capture_stop.restype = None
        self.handle = self.lib.wa_capture_start(window_id, width, height)
        if self.handle is None:
            raise CaptureError("ScreenCaptureKit 无法启动已有权限的窗口流")
        self.sequence = None
        self.age_s = None

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray | None:
        if not self.handle:
            raise CaptureError("ScreenCaptureKit 窗口流已关闭")
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        age, sequence = ctypes.c_double(), ctypes.c_uint64()
        result = self.lib.wa_capture_copy(self.handle, x, y, w, h, bgra.ctypes.data,
                                          ctypes.byref(age), ctypes.byref(sequence))
        if result != 1:
            self.sequence = self.age_s = None
        if result < 0:
            raise CaptureError("ScreenCaptureKit 窗口流已停止")
        if result == 0:
            return None
        self.sequence, self.age_s = sequence.value, age.value
        return bgra[:, :, 2::-1].copy()

    def close(self) -> None:
        if self.handle:
            self.lib.wa_capture_stop(self.handle)
            self.handle = None


def _game_window(*, require_focus):
    import Quartz as q
    from AppKit import NSWorkspace

    workspace = NSWorkspace.sharedWorkspace()
    if require_focus:
        app = workspace.frontmostApplication()
        if not app or app.bundleIdentifier() != "com.blizzard.worldofwarcraft":
            raise ValueError("魔兽未在前台")
        apps = (app,)
    else:
        apps = workspace.runningApplications()
    pids = {app.processIdentifier() for app in apps if app.bundleIdentifier() == "com.blizzard.worldofwarcraft"}
    windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or ()
    matches = []
    for window in windows:
        bounds = window.get("kCGWindowBounds", {})
        if (window.get("kCGWindowOwnerPID") in pids and window.get("kCGWindowLayer") == 0
                and bounds.get("Width", 0) >= 800 and bounds.get("Height", 0) >= 500):
            if require_focus:
                return window
            matches.append(window)
    if len(matches) != 1:
        raise ValueError("未找到唯一可用的魔兽窗口")
    return matches[0]


def frontmost_game_bounds() -> tuple[float, float, float, float]:
    """Return the foremost WoW window in desktop coordinates."""
    bounds = _game_window(require_focus=True)["kCGWindowBounds"]
    return bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"]


def game_window_pixels(*, require_focus: bool = True) -> np.ndarray:
    """Read the game's own image, independently of desktop overlays."""
    import Quartz as cg

    window = _game_window(require_focus=require_focus)
    image = cg.CGWindowListCreateImage(
        cg.CGRectNull, cg.kCGWindowListOptionIncludingWindow, window["kCGWindowNumber"],
        cg.kCGWindowImageBoundsIgnoreFraming
        | cg.kCGWindowImageShouldBeOpaque
        | cg.kCGWindowImageNominalResolution,
    )
    return _image_srgb(cg, image)


def _image_srgb(cg, image) -> np.ndarray:
    if image is None:
        raise CaptureError("CoreGraphics could not capture the image")
    width, height = cg.CGImageGetWidth(image), cg.CGImageGetHeight(image)
    srgb = cg.CGColorSpaceCreateWithName(cg.kCGColorSpaceSRGB)
    bitmap = cg.CGBitmapContextCreate(None, width, height, 8, width * 4, srgb, cg.kCGImageAlphaNoneSkipLast)
    if bitmap is None:
        raise CaptureError("CoreGraphics could not create an sRGB bitmap")
    cg.CGContextDrawImage(bitmap, cg.CGRectMake(0, 0, width, height), image)
    pixels = cg.CGBitmapContextGetData(bitmap).as_buffer(width * height * 4)
    return np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()


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
        self._pinned_window = None
        self._window_stream = None
        self._using_stream = False
        self._next_bounds_check = 0.
        self._grab_count = 0
        self._t0 = 0.0

    def close(self) -> None:
        self.stop_window_stream()
        if self._mss is not None:
            self._mss.close()
            self._mss = None
        self._pinned_window = None

    def bind_game_window(self, *, stream: bool = True) -> None:
        """Capture the frontmost game's own pixels in desktop coordinates."""
        self.stop_window_stream()
        window = _game_window(require_focus=True)
        bounds = window["kCGWindowBounds"]
        region = tuple(round(bounds[key]) for key in ("X", "Y", "Width", "Height"))
        self._pinned_window = int(window["kCGWindowNumber"]), region
        if stream and sys.platform == "darwin":
            try:
                self._window_stream = MacWindowStream(self._pinned_window[0], *region[2:])
            except (OSError, AttributeError, CaptureError) as exc:
                log.debug("ScreenCaptureKit unavailable: %s", exc)
        self._next_bounds_check = time.monotonic() + .5
        self._t0 = time.time()
        log.info("capture game window: %s", region)

    def stop_window_stream(self) -> bool:
        if self._window_stream is None:
            return False
        self._window_stream.close()
        self._window_stream = None
        self._using_stream = False
        return True

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
        if self._pinned_window:
            return [self._pinned_window[1]]
        self._ensure()
        return [(m["left"], m["top"], m["width"], m["height"]) for m in self._mss.monitors[1:]]

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        x, y, w, h = int(x), int(y), int(w), int(h)
        if self._pinned_window:
            window_id, (left, top, width, height) = self._pinned_window
            now = time.monotonic()
            if now >= self._next_bounds_check:
                try:
                    current = _game_window(require_focus=True)
                except ValueError as exc:
                    raise CaptureError(str(exc)) from exc
                bounds = current["kCGWindowBounds"]
                region = tuple(round(bounds[key]) for key in ("X", "Y", "Width", "Height"))
                if (int(current["kCGWindowNumber"]), region) != self._pinned_window:
                    raise CaptureError("游戏窗口位置或尺寸已变化")
                self._next_bounds_check = now + .5
            if not (0 < w <= width and 0 < h <= height
                    and left <= x <= left + width - w and top <= y <= top + height - h):
                raise CaptureError("游戏窗口截图区域越界")
            rgb = None
            if self._window_stream:
                try:
                    rgb = self._window_stream.grab(x - left, y - top, w, h)
                except CaptureError as exc:
                    log.info("ScreenCaptureKit: %s", exc)
                    self.stop_window_stream()
            if rgb is None:
                rgb = self._grab_mac_srgb(x, y, w, h, window_id=window_id)
            elif not self._using_stream:
                self._using_stream = True
                log.info("capture active backend: ScreenCaptureKit latest window frame")
            if rgb.shape != (h, w, 3):
                raise CaptureError("游戏窗口截图坐标尺度已变化")
        elif sys.platform == "darwin":
            self._ensure()
            rgb = self._grab_mac_srgb(x, y, w, h)
        else:
            self._ensure()
            assert self._mss is not None
            shot = self._mss.grab({"left": x, "top": y, "width": w, "height": h})
            arr = np.asarray(shot, dtype=np.uint8)  # BGRA
            rgb = arr[:, :, :3][:, :, ::-1].copy()
        self._grab_count += 1
        return rgb

    @staticmethod
    def _grab_mac_srgb(x: int, y: int, w: int, h: int, *, window_id: int | None = None) -> np.ndarray:
        import Quartz as cg

        # Convert the CGImage before extracting bytes. Raw display colors can
        # shift across the pixel channel's nibble boundaries on other monitors.
        image = cg.CGWindowListCreateImage(
            cg.CGRectMake(x, y, w, h),
            cg.kCGWindowListOptionOnScreenOnly if window_id is None else cg.kCGWindowListOptionIncludingWindow,
            cg.kCGNullWindowID if window_id is None else window_id,
            cg.kCGWindowImageBoundsIgnoreFraming
            | cg.kCGWindowImageShouldBeOpaque
            | cg.kCGWindowImageNominalResolution,
        )
        return _image_srgb(cg, image)

    @property
    def size(self) -> tuple[int, int]:
        if self._pinned_window:
            return self._pinned_window[1][2:]
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
