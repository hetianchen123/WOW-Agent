"""Verified world map crop and local model road observations."""
from collections import Counter
from dataclasses import dataclass
import math

import cv2
import numpy as np

from . import channel_spec as spec

ROWS = 10
ROAD_STRIP_RAISED_BUILD = 339
COLORS = ((255, 0, 255), (0, 255, 255), (255, 255, 0), (0, 0, 255))


@dataclass(frozen=True)
class MapIdentity:
    profile: int
    build: int
    map_id: int
    art: int

    @property
    def key(self):
        return f"{self.profile}-{self.build}-{self.map_id}-{self.art}"


@dataclass(frozen=True)
class RoadMapStatus:
    sequence: int
    identity: MapIdentity
    armed: bool
    state: int
    raised: bool = False


def road_region(x, y, *, raised=False):
    height = ROWS * spec.REGION_H
    return x + 432, y - height - (256 if raised else 0), spec.REGION_W, height


def road_capture_button(x, y, *, raised=False):
    left, top, width, _ = road_region(x, y, raised=raised)
    return left + width // 2, top - 14


def decode_road_status(image):
    if image.shape != (ROWS * spec.REGION_H, spec.REGION_W, 3):
        return None
    values, sequences = [], set()
    for index in range(ROWS):
        row = spec.decode_frame(spec.sample_nibbles(image, 0, index * spec.REGION_H))
        if not row or row[0] != index:
            return None
        values.append(row[1] % 4096)
        sequences.add(row[1] // 4096)
    if len(sequences) != 1 or values[0] != 0xB38:
        return None
    armed, state = values[1] % 16, values[1] // 16
    if armed > 1 or state > 2 or armed and state:
        return None

    def pair(index):
        return values[index] + values[index + 1] * 4096

    identity = MapIdentity(pair(6), pair(8), pair(2), pair(4))
    if min(identity.build, identity.map_id, identity.art) > 0:
        return RoadMapStatus(sequences.pop(), identity, bool(armed), state)
    return None


def map_crop(rgb):
    """Recover the complete canvas from four independently located corner marks."""
    corners = []
    for color in COLORS:
        # Window capture converts saturated game colors into sRGB. On the
        # Forever client the blue and magenta markers can carry 46 green.
        lower = tuple(218 if channel else 0 for channel in color)
        upper = tuple(255 if channel else 62 for channel in color)
        mask = cv2.inRange(rgb, lower, upper)
        blobs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for blob in blobs:
            x, y, w, h = cv2.boundingRect(blob)
            if 5 <= min(w, h) <= max(w, h) <= 34 and abs(w - h) <= 2 and cv2.contourArea(blob) >= .58 * w * h:
                candidates.append((x, y, w, h))
        if not candidates:
            return None
        corners.append(candidates)

    rectangles = set()
    for x, y, w, h in corners[0]:
        for rx, ry, rw, rh in corners[1]:
            if abs(ry - y) > 2 or abs(w - rw) > 3 or rx - x < 300:
                continue
            for lx, ly, lw, lh in corners[2]:
                if abs(lx - x) > 2 or abs(w - lw) > 3 or ly - y < 180:
                    continue
                for bx, by, bw, bh in corners[3]:
                    if abs(bx - rx) <= 2 and abs(by - ly) <= 2 and abs(w - bw) <= 3:
                        rectangles.add((x, y, rx + rw, ly + lh))
    if len(rectangles) != 1:
        return None
    x, y, right, bottom = rectangles.pop()
    canvas = rgb[y:bottom, x:right].copy()
    # Paint out the coordinate markers without shifting the normalized bounds.
    pad = 36
    for row in (slice(0, pad), slice(-pad, None)):
        for col in (slice(0, pad), slice(-pad, None)):
            canvas[row, col] = canvas[row, pad:-pad].mean(axis=1, keepdims=True)
    if float(canvas[pad:-pad, pad:-pad].std(axis=(0, 1)).mean()) < 8:
        return None
    return canvas


def parse_traces(payload, *, model=True):
    """Coordinates in the model protocol are integers on the 0..1000 canvas."""
    if not isinstance(payload, dict) or not isinstance(payload.get("roads"), list):
        raise ValueError("invalid road map model response")
    traces = []
    for trace in payload["roads"][:16]:
        if not isinstance(trace, list) or not 2 <= len(trace) <= 32:
            continue
        if not all(isinstance(point, list) and len(point) == 2
                   and all(type(v) is int and 0 <= v <= 1000 for v in point) for point in trace):
            continue
        # A straight observed stroke needs only two endpoints. The local model
        # has emitted both long arithmetic series and short variations of them.
        if model and len(trace) >= 3:
            steps = [(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(trace, trace[1:])]
            mean = tuple(sum(axis) / len(steps) for axis in zip(*steps))
            repeated = max(Counter(steps).values()) >= len(steps) * .8
            if repeated or all(math.dist(step, mean) <= max(3, .17 * math.hypot(*mean)) for step in steps):
                continue
        traces.append(tuple((x / 1000, y / 1000) for x, y in trace))
    return traces


def visible_map_art(rgb):
    """Limit parchment-heavy maps to areas around visible colored terrain."""
    height, width = rgb.shape[:2]
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    warm, green = (lab[:, :, 1] >= 132) & (lab[:, :, 2] >= 151), lab[:, :, 1] <= 122
    if warm.mean() < .74 or green.mean() < .005:
        return None, (0, 0, width, height)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(green.astype(np.uint8))
    regions = stats[:, cv2.CC_STAT_AREA] >= max(20, rgb.shape[0] * rgb.shape[1] * .00035)
    regions[0] = False
    core = regions[labels]
    if not core.any():
        return None, (0, 0, width, height)
    distance = cv2.distanceTransform((~core).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    visible = distance <= max(10, width * .033)
    left, top, w, h = cv2.boundingRect(visible.astype(np.uint8))
    return visible, (left, top, left + w, top + h)


def supported_traces(rgb, traces, visible=None):
    """Refine rough model polylines against contiguous road-colored strokes."""
    height, width = rgb.shape[:2]
    size = np.array((width - 1, height - 1))
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab = cv2.GaussianBlur(lab, (3, 3), .65)
    scale = max(.65, min(1.6, width / 900.))
    radius = min(60, max(18, round(width * .045)))
    offsets = np.linspace(-radius, radius, radius + 1)
    columns = np.arange(len(offsets))
    neighbors = np.clip(columns[:, None] + np.arange(-3, 4), 0, len(offsets) - 1)

    def colors(coordinates):
        x, y = np.moveaxis(np.clip(np.rint(coordinates).astype(int), 0, size), -1, 0)
        return lab[y, x]

    def measure(trace):
        samples = []
        for first, second in zip(trace, trace[1:]):
            start, stop = np.array(first) * size, np.array(second) * size
            length = math.dist(start, stop)
            if length >= 2:
                count = math.ceil(length / (4 * scale))
                samples.extend(np.linspace(start, stop, count + 1)[bool(samples):])
        if len(samples) < 8:
            return []
        centers = np.array(samples)
        vectors = np.gradient(centers, axis=0)
        lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
        normals = np.column_stack((-vectors[:, 1], vectors[:, 0])) / np.maximum(lengths, 1e-6)
        candidates = centers[:, None, :] + normals[:, None, :] * offsets[None, :, None]
        on = colors(candidates)
        evidence = np.zeros(on.shape[:2])
        for flank in (3, 6, 10):
            shift = flank * scale * normals[:, None, :]
            left, right = colors(candidates - shift), colors(candidates + shift)
            contrast = np.minimum(on[:, :, 0] - left[:, :, 0], on[:, :, 0] - right[:, :, 0])
            distinct = np.minimum(np.linalg.norm(on - left, axis=2), np.linalg.norm(on - right, axis=2))
            evidence = np.maximum(evidence, np.where((contrast >= 4) & (distinct >= 8), contrast, 0))
        valid = ((evidence >= 5) & (on[:, :, 2] >= 131) &
                 np.all((candidates >= 2) & (candidates <= size - 2), axis=2))
        if visible is not None:
            x, y = np.moveaxis(np.clip(np.rint(candidates).astype(int), 0, size), -1, 0)
            valid &= visible[y, x]
        weights = np.where(valid, np.minimum(24, evidence) - .08 * np.abs(offsets), -np.inf)
        return strongest_stroke(candidates, weights)

    def strongest_stroke(candidates, weights):
        active = weights[0]
        parents = np.full(weights.shape, -1, dtype=int)
        best = (float(active.max()), 0, int(active.argmax()))
        for row in range(1, len(weights)):
            links = candidates[row - 1, neighbors]
            distance = np.linalg.norm(candidates[row, :, None, :] - links, axis=2)
            previous = np.where(distance <= 8 * scale, active[neighbors] - .22 * distance, -np.inf)
            predecessor = previous.argmax(axis=1)
            progress = previous[columns, predecessor]
            continued = (progress > 0) & np.isfinite(weights[row])
            parents[row, continued] = neighbors[columns[continued], predecessor[continued]]
            active = weights[row] + np.maximum(0., progress)
            column = int(active.argmax())
            if active[column] > best[0]:
                best = float(active[column]), row, column
        _, row, column = best
        points = []
        while column >= 0:
            points.append(candidates[row, column])
            column, row = parents[row, column], row - 1
        if len(points) < 8 or sum(math.dist(a, b) for a, b in zip(points, points[1:])) < width * .04:
            return []
        line = np.array(points[::-1], dtype=np.float32)
        simplified = cv2.approxPolyDP(line, 1.4 * scale, False).reshape(-1, 2) / size
        return [tuple((float(x), float(y)) for x, y in simplified[start:start + 32])
                for start in range(0, len(simplified) - 1, 31)]

    confirmed = []
    for trace in traces:
        confirmed.extend(measure(trace))
    return confirmed[:16]
