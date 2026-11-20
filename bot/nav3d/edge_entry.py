"""Verify a supervised short move from source terrain into an eroded mesh."""
from dataclasses import dataclass
import hashlib
import json
import math

import cv2
import numpy as np

from .geometry import load_scene
from .planner import Frame, NavigationError, Path3D


def nearest_boundary_origin(polygon):
    edges = np.roll(polygon, -1, axis=0) - polygon
    fraction = (-np.sum(polygon * edges, axis=1) / np.sum(edges * edges, axis=1)).clip(0, 1)
    nearest = polygon + fraction[:, None] * edges
    return min(nearest, key=np.linalg.norm)


@dataclass(frozen=True)
class TerrainEntry:
    route: Path3D
    triangles: np.ndarray
    maximum_slope: float
    direction: np.ndarray
    length: float
    receding_slopes: tuple[float, ...]
    steep_projections: tuple[np.ndarray, ...]

    def position(self, xy):
        offset = np.asarray(xy, dtype=float) - self.route.requested_start[:2]
        along, sideways = offset @ self.direction
        if not -.12 <= along <= self.length + .18 or abs(sideways) > .15:
            raise NavigationError("entry_lateral_or_longitudinal_deviation")
        if any(cv2.pointPolygonTest(poly, (float(along), float(sideways)), True)
               > cv2.pointPolygonTest(poly, (0., 0.), True) + .07 for poly in self.steep_projections):
            raise NavigationError("entry_motion_toward_steep_ground")
        return support(self.triangles, xy, self.maximum_slope)


def support(triangles, xy, maximum_slope):
    heights = []
    for a, b, c in triangles:
        u, v = b - a, c - a
        normal = np.cross(u, v)
        if normal[2] <= 1e-8:
            continue
        edge = np.asarray(xy) - a[:2]
        weights = np.linalg.solve(np.stack((u[:2], v[:2]), axis=1), edge)
        if min(weights) < -1e-6 or sum(weights) > 1.000001:
            continue
        slope = math.degrees(math.atan2(math.hypot(*normal[:2]), normal[2]))
        if slope > maximum_slope:
            raise NavigationError("entry_center_on_steep_ground")
        heights.append(float(a[2] + np.dot(weights, (u[2], v[2]))))
    if not heights or max(heights) - min(heights) > .015:
        raise NavigationError("entry_source_support_ambiguous")
    return float(xy[0]), float(xy[1]), min(heights)


def plan_edge_entry(mesh, tracker, scene_file, start, goal):
    length = math.dist(start, goal)
    if not .50 <= length <= 1.20 or tracker.candidates(start):
        raise NavigationError("entry_requires_nearby_off_mesh_start")
    start_support = tracker.candidates(start, edge_tolerance=.22)
    goal_support = tracker.candidates(goal)
    tolerance = .14
    if (len(start_support) != 1 or len(goal_support) != 1
            or any(len(tracker.candidates((goal[0] + dx, goal[1] + dy))) != 1
                   for dx, dy in ((tolerance, 0), (-tolerance, 0), (0, tolerance), (0, -tolerance)))):
        raise NavigationError("entry_mesh_support_not_confirmed")

    scene, vertices, indices, sources = load_scene(scene_file)
    digest = hashlib.sha256(vertices.astype("<f8").tobytes() + indices.astype("<i4").tobytes()).hexdigest()
    if Frame(**scene["frame"]) != mesh.frame or tuple(sources) != mesh.sources or digest != mesh.geometry_hash:
        raise NavigationError("entry_source_does_not_match_mesh")

    forward = (np.asarray(goal) - start) / length
    direction = np.column_stack((forward, (-forward[1], forward[0])))
    # Cover the character radius plus the allowed observed lateral deviation.
    radius = mesh.agent.radius + .15
    padding = mesh.agent.radius + .18
    box = np.float32([[-padding, -radius], [length+padding, -radius],
                      [length+padding, radius], [-padding, radius]])
    triangles = vertices[indices]
    extent = length + max(radius, padding) + .05
    nearby = triangles[((triangles[:, :, :2].min(1) <= np.asarray(start) + extent).all(1)
                        & (triangles[:, :, :2].max(1) >= np.asarray(start) - extent).all(1))]
    start_height = support(nearby, start, mesh.agent.max_slope)[2]
    projections, surface, receding, steep = [], [], [], []
    coverage = 0.
    for tri in nearby:
        local = np.float32((tri[:, :2] - start) @ direction)
        if np.any(local.max(0) < box.min(0)) or np.any(local.min(0) > box.max(0)):
            continue
        if cv2.contourArea(local) <= 1e-6:
            if tri[:, 2].max() >= start_height - mesh.agent.max_climb:
                raise NavigationError("entry_source_vertical_obstacle")
            continue
        area, clipped = cv2.intersectConvexConvex(box, local)
        if not math.isfinite(area):
            raise NavigationError("entry_source_projection_invalid")
        if area <= 1e-6:
            continue
        normal = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        slope = math.degrees(math.atan2(math.hypot(*normal[:2]), normal[2]))
        if slope >= 65:
            raise NavigationError("entry_source_obstacle_or_cliff")
        if slope > mesh.agent.max_slope:
            nearest = nearest_boundary_origin(local)
            if (cv2.pointPolygonTest(local, (0., 0.), True) >= -.10
                    or -nearest[0] < .15 * np.linalg.norm(nearest)):
                raise NavigationError("entry_does_not_leave_steep_ground")
            receding.append(slope)
            steep.append(local)
        if any(cv2.intersectConvexConvex(previous, clipped)[0] > 1e-5 for previous in projections):
            raise NavigationError("entry_overlapping_collision_surfaces")
        coverage += area
        projections.append(clipped)
        surface.append(tri)
    if not math.isclose(coverage, cv2.contourArea(box), abs_tol=1e-4):
        raise NavigationError("entry_source_ground_gap")

    # Every intersected surface must share a physical edge with this patch.
    vertices_by_face = [set(map(tuple, tri.round(4))) for tri in surface]
    connected, frontier = {0}, [0]
    while frontier:
        current = frontier.pop()
        for index, vertices_on_face in enumerate(vertices_by_face):
            if index not in connected and len(vertices_on_face & vertices_by_face[current]) >= 2:
                connected.add(index)
                frontier.append(index)
    if len(connected) != len(surface):
        raise NavigationError("entry_disconnected_source_ground")

    points = tuple(support(surface, (np.asarray(start) + forward * distance), mesh.agent.max_slope)
                   for distance in np.linspace(0, length, 1 + math.ceil(length / .10)))
    if (abs(points[0][2]-start_support[0][2]) > mesh.agent.max_climb
            or abs(points[-1][2]-goal_support[0][2]) > mesh.agent.max_climb):
        raise NavigationError("entry_source_height_mismatch")
    revision = hashlib.sha256(json.dumps(()).encode()).hexdigest()
    route = Path3D(mesh.frame, mesh.mesh_id, points, points[0], points[-1],
                   sum(math.dist(a, b) for a, b in zip(points, points[1:])), revision,
                   "continuous_source_collision_surface")
    return TerrainEntry(route, np.array(surface), mesh.agent.max_slope, direction, length, tuple(receding), tuple(steep))
