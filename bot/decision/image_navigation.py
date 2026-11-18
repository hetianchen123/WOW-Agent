"""Bounded image-space navigation over observed ground, solids and unknown regions.

This produces geometry candidates, NEVER world coordinates or input permission.
Unlabelled pixels are unknown. A valid route is conditional on its input map.
"""
from dataclasses import dataclass
import heapq
import math
import time
from typing import Callable

import cv2
import numpy as np

from .route_geometry import simple_polygon, segment_distance


class ImageRouteError(ValueError):
    pass


@dataclass(frozen=True)
class SceneMap:
    frame_id: str
    size: tuple[int, int]
    ground: tuple
    obstacles: tuple = ()
    unknown: tuple = ()
    source: str = "unknown"


@dataclass(frozen=True)
class ImageRoute:
    frame_id: str
    points: tuple
    length_px: float
    clearance_px: float
    expanded_nodes: int
    elapsed_ms: float
    map_source: str
    executable: bool = False


def _point(p, size):
    return (isinstance(p, (list, tuple)) and len(p) == 2 and
            all(type(v) in (int, float) and math.isfinite(v) and 0 <= v < n
                for v, n in zip(p, size)))


class ImageNavigator:
    def __init__(self, scene: SceneMap, *, body_radius_px=8, uncertainty_px=4, cell_px=3):
        self._configure(scene, body_radius_px, uncertainty_px, cell_px)
        w, h = scene.size
        if not scene.ground:
            raise ImageRouteError('image_ground_unknown')
        groups = (scene.ground, scene.obstacles, scene.unknown)
        if sum(len(group) for group in groups) > 64:
            raise ImageRouteError('image_map_budget')
        for group in groups:
            for poly in group:
                if (not 3 <= len(poly) <= 256 or not all(_point(p, scene.size) for p in poly)
                        or not simple_polygon(list(poly))):
                    raise ImageRouteError('image_polygon_invalid')
        mask = np.zeros((h, w), dtype=np.uint8)
        for group, value in ((scene.ground, 255), (scene.obstacles, 0), (scene.unknown, 0)):
            for poly in group:
                cv2.fillPoly(mask, [np.rint(poly).astype(np.int32)], value)
        self._set_mask(mask)

    @classmethod
    def from_masks(cls, frame_id, ground, obstacles, unknown, *, source,
                   body_radius_px=8, uncertainty_px=4, cell_px=3):
        """Keep holes/unknown pixels intact; no polygon approximation or hole fill."""
        arrays = (ground, obstacles, unknown)
        if any(not isinstance(a, np.ndarray) or a.dtype != bool or a.ndim != 2 for a in arrays):
            raise ImageRouteError('image_mask_invalid')
        if any(a.shape != ground.shape for a in arrays):
            raise ImageRouteError('image_mask_size_mismatch')
        h, w = ground.shape
        self = cls.__new__(cls)
        self._configure(SceneMap(frame_id, (w, h), (), source=source), body_radius_px, uncertainty_px, cell_px)
        self._set_mask((ground & ~obstacles & ~unknown).astype(np.uint8)*255)
        return self

    def _configure(self, scene, body_radius_px, uncertainty_px, cell_px):
        self.scene = scene
        w, h = scene.size
        if (not scene.frame_id or not scene.source or type(w) is not int or type(h) is not int
                or min(w, h) < 8 or w*h > 4_000_000):
            raise ImageRouteError('image_map_invalid')
        if (any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                for v in (body_radius_px, uncertainty_px)) or
                body_radius_px+uncertainty_px > 64 or type(cell_px) is not int or not 1 <= cell_px <= 8):
            raise ImageRouteError('image_clearance_invalid')
        self.radius = body_radius_px + uncertainty_px
        self.cell = cell_px

    def _set_mask(self, mask):
        w, h = self.scene.size
        cell_px = self.cell
        self.observed_ground = mask
        # Extra raster margin covers rounding and pixel-sized boundary ambiguity.
        radius = math.ceil(self.radius) + 2
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*radius+1, 2*radius+1))
        self.clear = cv2.erode(mask, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
        self.xs = np.arange(cell_px//2, w, cell_px)
        self.ys = np.arange(cell_px//2, h, cell_px)
        self.nodes = self.clear[np.ix_(self.ys, self.xs)]

    def line_clear(self, a, b):
        """Check the complete segment, including sub-cell gaps and unknown areas."""
        if not _point(a, self.scene.size) or not _point(b, self.scene.size):
            return False
        count = max(2, math.ceil(math.dist(a, b)*2)+1)
        points = np.rint(np.linspace(a, b, count)).astype(np.int32)
        if (points[:, 0].max() >= self.scene.size[0] or
                points[:, 1].max() >= self.scene.size[1]):
            return False
        if not self.clear[points[:, 1], points[:, 0]].all():
            return False
        return True

    def _xy(self, node):
        return (int(self.xs[node[0]]), int(self.ys[node[1]]))

    def _links(self, point):
        center = (round((point[0]-self.cell//2)/self.cell),
                  round((point[1]-self.cell//2)/self.cell))
        links = {}
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                x, y = center[0]+dx, center[1]+dy
                if 0 <= x < len(self.xs) and 0 <= y < len(self.ys) and self.nodes[y, x]:
                    p = self._xy((x, y))
                    if self.line_clear(point, p):
                        links[x, y] = math.dist(point, p)
        return links

    def validate(self, points):
        if len(points) < 2:
            raise ImageRouteError('image_route_empty')
        for a, b in zip(points, points[1:]):
            if not self.line_clear(a, b):
                raise ImageRouteError('image_route_crosses_blocked_or_unknown')
            # Independent continuous check after raster search and simplification.
            for poly in (*self.scene.obstacles, *self.scene.unknown):
                if min(segment_distance(a, b, c, d) for c, d in
                       zip(poly, (*poly[1:], poly[0]))) < self.radius - 1e-6:
                    raise ImageRouteError('image_route_clearance_insufficient')

    def plan(self, start, goal, *, max_nodes=80_000, timeout_s=2,
             cancelled: Callable[[], bool] = lambda: False):
        began = time.monotonic()
        def guard():
            if cancelled():
                raise ImageRouteError('image_route_cancelled')
            if time.monotonic()-began >= timeout_s:
                raise ImageRouteError('image_route_time_budget')
        guard()
        for label, p in (('start', start), ('goal', goal)):
            if not _point(p, self.scene.size):
                raise ImageRouteError(f'image_{label}_outside_frame')
            if not self.line_clear(p, p):
                raise ImageRouteError(f'image_{label}_blocked_or_uncertain')
        start, goal = tuple(start), tuple(goal)
        if self.line_clear(start, goal):
            path, expanded = [start, goal], 0
        else:
            costs, ends = self._links(start), self._links(goal)
            if not costs or not ends:
                raise ImageRouteError('image_endpoint_not_connected')
            previous = {node: None for node in costs}
            queue = [(g+math.dist(self._xy(node), goal), g, node) for node, g in costs.items()]
            heapq.heapify(queue)
            expanded, reached = 0, None
            while queue:
                _, g, node = heapq.heappop(queue)
                if g != costs[node]:
                    continue
                expanded += 1
                if expanded > max_nodes:
                    raise ImageRouteError('image_route_node_budget')
                if expanded % 32 == 1:
                    guard()
                if node in ends:
                    reached = node
                    break
                x, y = node
                for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                    nx, ny = x+dx, y+dy
                    if not (0 <= nx < len(self.xs) and 0 <= ny < len(self.ys)) or not self.nodes[ny, nx]:
                        continue
                    if dx and dy and (not self.nodes[y, nx] or not self.nodes[ny, x]):
                        continue
                    neighbor = nx, ny
                    ng = g + self.cell*math.hypot(dx, dy)
                    if ng >= costs.get(neighbor, math.inf):
                        continue
                    if not self.line_clear(self._xy(node), self._xy(neighbor)):
                        continue
                    costs[neighbor], previous[neighbor] = ng, node
                    heapq.heappush(queue, (ng+math.dist(self._xy(neighbor), goal), ng, neighbor))
            if reached is None:
                raise ImageRouteError('image_no_observed_route')
            path = [goal]
            while reached is not None:
                path.append(self._xy(reached))
                reached = previous[reached]
            path.append(start)
            path.reverse()
            # Remove only line-of-sight redundant points. Never trim to a waypoint
            # limit by drawing a shortcut through the obstacle.
            simplified, i = [start], 0
            while i < len(path)-1:
                guard()
                j = len(path)-1
                while j > i+1 and not self.line_clear(path[i], path[j]):
                    j -= 1
                simplified.append(path[j]); i = j
            path = simplified
        guard()
        self.validate(path)
        return ImageRoute(self.scene.frame_id, tuple(path),
                          sum(math.dist(a,b) for a,b in zip(path,path[1:])),
                          self.radius, expanded, (time.monotonic()-began)*1000,
                          self.scene.source)
