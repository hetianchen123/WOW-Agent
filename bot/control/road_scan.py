"""Acquire one world-map screenshot and retain its local-model road candidates."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from threading import Event
import time

import cv2
import numpy as np

from ..core.paths import DATA_DIR
from ..data.savedvars import parse_lua_table
from ..perception.capture import CaptureError, game_window_pixels
from ..perception.road_map import (MapIdentity, map_crop, parse_traces, road_capture_button, supported_traces,
                                   visible_map_art)
from ..perception.vision_worker import get_worker
from .quest_terrain import local_vision_available
from .road_route import RoadTrace

log = logging.getLogger(__name__)
DATA_HEADER = "-- WowAgent world map scan data. The local controller updates the installed copy.\n"
CACHE_HOURS = 12
RESCAN_DISTANCE_YARDS = 220
RESCAN_DELAY_S = 60


def profile_token(key):
    token = 0
    for byte in key.encode():
        token = (token * 131 + byte) % (1 << 24)
    return token


def installed_forever():
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app and app.bundleIdentifier() == "com.blizzard.worldofwarcraft":
        url = app.bundleURL()
        if url and url.path():
            folder = Path(url.path()).resolve().parent
            if folder.name == "_forever_":
                return folder
    return None


def manual_traces(installation, identity):
    routes = []
    if installation is None:
        return ()
    for path in sorted(installation.glob("WTF/Account/*/SavedVariables/WowAgent.lua")):
        try:
            if path.stat().st_size > 16_000_000:
                continue
            db = parse_lua_table(path.read_text(encoding="utf-8")).get("WowAgentDB", {})
            roads = db.get("roads", {})
            if roads.get("schema") != 1:
                continue
            for key, profile in roads.get("profiles", {}).items():
                if isinstance(key, str) and profile_token(key) == identity.profile:
                    routes.extend(lua_array(profile.get("routes")))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.debug("World map annotations could not be read: %s", type(exc).__name__)
    traces = set()
    for route in routes:
        if not isinstance(route, dict) or route.get("mode") not in {"walk", "ground"}:
            continue
        current = []
        direction = route.get("bidirectional")
        if type(direction) is not bool:
            continue
        points = lua_array(route.get("points"))
        for point in (*points, None):
            xy = (point.get("x"), point.get("y")) if isinstance(point, dict) else ()
            if (xy and point.get("mapID") == identity.map_id and point.get("artID") == identity.art
                    and all(type(v) in (float, int) and 0 <= v <= 1 for v in xy)):
                current.append(xy)
            else:
                if len(current) >= 2:
                    traces.add(RoadTrace(tuple(current), route["mode"], direction))
                current = []
    return tuple(sorted(traces, key=lambda trace: (trace.mode, trace.points, trace.bidirectional)))


def lua_array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value[key] for key in sorted(key for key in value if type(key) is int and key > 0)]
    return ()


def atomic_text(path, content):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def load_observation(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != 1:
        raise ValueError("unsupported road cache")
    identity = MapIdentity(*value["identity"])
    paths = parse_traces({"roads": value["roads"]}, model=False)
    return identity, tuple(paths), float(value["created"])


def publish_overlay(root, installation):
    if installation is None:
        return False
    target = installation / "Interface/AddOns/WowAgent/RoadScanData.lua"
    if not target.is_file() or not target.read_text(encoding="utf-8").startswith(DATA_HEADER):
        return False
    profiles = {}
    for path in sorted(root.glob("*/roads.json")):
        try:
            identity, traces, _ = load_observation(path)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
        if identity.key != path.parent.name or not traces:
            continue
        if not all(type(value) is int and 0 <= value < 1 << 24 for value in (
                identity.profile, identity.build, identity.map_id, identity.art)):
            continue
        maps = profiles.setdefault(identity.profile, {}).setdefault(identity.build, {})
        maps.setdefault(identity.map_id, {})[identity.art] = traces

    def lua(table):
        if isinstance(table, dict):
            return "{" + ",\n".join(f"[{key}]={lua(value)}" for key, value in sorted(table.items())) + "}"
        if isinstance(table, tuple):
            return "{" + ",".join(map(lua, table)) + "}"
        return f"{table:.3f}"

    atomic_text(target, DATA_HEADER + "WowAgent.RoadScanData = " + lua(profiles) + "\n")
    return True


def infer_roads(image, folder, cancelled):
    def guard():
        if cancelled.is_set():
            raise ValueError("road map scan cancelled")

    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "map.png"
    if not cv2.imwrite(str(source), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
        raise OSError("could not save world map screenshot")
    visible, (left, top, right, bottom) = visible_map_art(image)
    model_source = source
    if visible is not None:
        view = image[top:bottom, left:right].copy()
        view[~visible[top:bottom, left:right]] = (110, 110, 110)
        model_source = folder / "model-input.png"
        if not cv2.imwrite(str(model_source), cv2.cvtColor(view, cv2.COLOR_RGB2BGR)):
            raise OSError("could not save explored map crop")
    cases = get_worker().inspect([model_source], road_map=True, guard=guard, timeout=48)
    guard()
    case, = cases
    atomic_text(folder / "model.json", json.dumps(case, ensure_ascii=False, indent=2))
    traces = parse_traces(case.get("observation"))
    if visible is not None:
        height, width = image.shape[:2]
        traces = [tuple(((left + x * (right - left - 1)) / (width - 1),
                         (top + y * (bottom - top - 1)) / (height - 1)) for x, y in trace) for trace in traces]
    return supported_traces(image, traces, visible)


class RoadScanSession:
    def __init__(self, root=None):
        self.root = root or DATA_DIR / "roads/forever"
        self.identity = None
        self.installation = None
        self.last_sequence = None
        self.settle_at = None
        self.request_at = None
        self.screenshot = None
        self.capture_state = 0
        self.capture_error = None
        self.inference = None
        self.inference_at = None
        self.manual = self.detected = ()
        self.cached_at = None
        self.checked = set()
        self.first_capture = {}
        self.rescanned = set()

    def close(self):
        if self.inference:
            future, cancelled = self.inference
            self.inference = None
            self.inference_at = None
            cancelled.set()
            future.cancel()

    def reset_identity(self, identity):
        if self.inference or self.request_at is not None:
            self.checked.discard(self.identity)
        self.close()
        self.identity = identity
        self.request_at = self.screenshot = None
        try:
            self.installation = installed_forever()
        except (ImportError, OSError, ValueError):
            self.installation = None
        self.manual, self.detected = manual_traces(self.installation, identity), ()
        self.cached_at = None
        self.last_sequence = self.settle_at = None
        try:
            stored, traces, created = load_observation(self.root / identity.key / "roads.json")
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return
        if stored == identity and math.isfinite(created) and 0 <= time.time() - created:
            self.detected = tuple(RoadTrace(points) for points in traces)
            if time.time() - created <= (3600 * CACHE_HOURS if traces else 900):
                self.checked.add(identity)
                if traces:
                    self.cached_at = created

    def finish_inference(self, ctx):
        if not self.inference or not self.inference[0].done():
            return
        future, _ = self.inference
        self.inference = None
        self.inference_at = None
        identity = self.identity
        folder = self.root / identity.key
        try:
            traces = future.result()
            if not traces and self.detected:
                ctx.log_event("quest_main_road_scan_unconfirmed", {"map": identity.map_id, "reason": "no_new_strokes"})
                return
            rows = [[[round(x * 1000), round(y * 1000)] for x, y in trace] for trace in traces]
            value = {"schema": 1, "identity": [identity.profile, identity.build, identity.map_id, identity.art],
                     "created": time.time(), "roads": rows}
            atomic_text(folder / "roads.json", json.dumps(value, indent=2))
            self.detected = tuple(RoadTrace(points) for points in traces)
            if traces:
                image = cv2.imread(str(folder / "map.png"))
                if image is None:
                    raise OSError("world map screenshot unavailable")
                height, width = image.shape[:2]
                for trace in traces:
                    points = np.array([(round(x * (width - 1)), round(y * (height - 1))) for x, y in trace], np.int32)
                    cv2.polylines(image, [points], False, (255, 216, 45), 2, cv2.LINE_AA)
                    for point in points:
                        cv2.circle(image, point, 3, (255, 216, 45), -1, cv2.LINE_AA)
                cv2.imwrite(str(folder / "marked.png"), image)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError, cv2.error) as exc:
            ctx.log_event("quest_main_road_scan_unconfirmed", {"map": identity.map_id, "reason": str(exc)})
            return
        installed = False
        if traces:
            try:
                installed = publish_overlay(self.root, self.installation)
            except OSError as exc:
                log.info("Could not update the installed map overlay: %s", exc)
        ctx.log_event("quest_main_road_scan_result", {"map": identity.map_id, "art": identity.art,
            "paths": len(traces), "overlay_for_reload": installed, "evidence": str(folder)})

    def tick(self, ctx, now, status, *, allow, release, position=None, world_size=None, needs_coverage=False):
        if status is not None and status.identity != self.identity:
            self.reset_identity(status.identity)
            ctx.log_event("quest_main_road_loaded", {"map": self.identity.map_id, "art": self.identity.art,
                "manual": len(self.manual), "detected": len(self.detected)})
        if status is not None and status.identity == self.identity:
            self.finish_inference(ctx)
        if self.request_at is not None:
            release()
            if status:
                self.capture_state = max(self.capture_state, status.state)
            if status and status.state == 2 and self.screenshot is None:
                try:
                    frame = game_window_pixels()
                    self.screenshot = map_crop(frame)
                    if self.screenshot is not None:
                        height, width = self.screenshot.shape[:2]
                        ctx.log_event("quest_main_road_canvas_captured", {"map": self.identity.map_id,
                            "art": self.identity.art, "width": width, "height": height, "sequence": status.sequence})
                    if self.screenshot is None and self.capture_error is None:
                        self.capture_error = "canvas_markers_unresolved"
                        folder = self.root / self.identity.key
                        folder.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(folder / "capture-unverified.png"), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                except (ValueError, CaptureError, OSError, cv2.error) as exc:
                    self.capture_error = str(exc)
            if status and status.state == 0 and self.screenshot is not None:
                cancelled = Event()
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="road-map")
                future = pool.submit(infer_roads, self.screenshot, self.root / self.identity.key, cancelled)
                pool.shutdown(wait=False)
                self.inference = future, cancelled
                self.inference_at = now
                self.request_at = self.screenshot = None
            elif now - self.request_at >= 3:
                self.request_at = self.screenshot = None
                ctx.log_event("quest_main_road_scan_unconfirmed", {"map": self.identity.map_id,
                    "reason": self.capture_error or "map_capture_unverified", "map_state": self.capture_state})
            return True
        if status and status.state:
            release()
            return True
        if (allow and self.inference_at is not None and self.identity not in self.rescanned
                and not self.manual and not self.detected):
            if now - self.inference_at < 12:
                release()
                return True
        if allow and position is not None and self.identity in self.checked:
            self.first_capture.setdefault(self.identity, (now, position))
        previous = self.first_capture.get(self.identity)
        rescan = False
        if (previous and position is not None and world_size and self.inference is None
                and (needs_coverage or not (self.manual or self.detected)) and self.identity not in self.rescanned):
            captured_at, origin = previous
            distance = math.hypot((position[0] - origin[0]) * world_size[0],
                                  (position[1] - origin[1]) * world_size[1])
            older_cache = (needs_coverage and self.cached_at is not None
                           and time.time() - self.cached_at >= RESCAN_DELAY_S)
            rescan = older_cache or (now - captured_at >= RESCAN_DELAY_S and distance >= RESCAN_DISTANCE_YARDS)
        if not (allow and status and status.armed and (self.identity not in self.checked or rescan)
                and local_vision_available()):
            self.last_sequence = self.settle_at = None
            return False
        release()
        if self.settle_at is None:
            self.settle_at, self.last_sequence = now, status.sequence
        if now - self.settle_at < .25 or status.sequence == self.last_sequence:
            return True
        self.checked.add(self.identity)
        if rescan:
            self.rescanned.add(self.identity)
        elif position is not None:
            self.first_capture.setdefault(self.identity, (now, position))
        self.settle_at, self.last_sequence = None, None
        self.request_at = now
        self.cached_at = None
        self.capture_state = 0
        self.capture_error = None
        channel = ctx.cfg.channel
        ctx.input.move_to(*road_capture_button(channel.x, channel.y, raised=status.raised), jitter_px=0)
        ctx.input.click_ui()
        ctx.log_event("quest_main_road_scan_requested", {"map": self.identity.map_id, "art": self.identity.art,
            "rescan_after_travel": rescan})
        return True

    def traces(self, status):
        if status is not None and status.identity == self.identity:
            return self.manual + self.detected
        return ()
