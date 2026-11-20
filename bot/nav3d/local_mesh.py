"""Select connected ground on a verified local mesh before any movement."""
from __future__ import annotations

import math

import numpy as np

from ..control.navigation import route_advances_toward
from .follower import WorldRouteFollower
from .planner import NavMesh, NavigationError, Query
from .runtime import SurfaceTracker


class LocalMesh:
    def __init__(self, config):
        self.config = config
        self.mesh = NavMesh.load(config.mesh)
        if self.mesh.mesh_id != config.supervised_trial_mesh_id:
            raise NavigationError("nav3d_trial_mesh_mismatch")
        self.tracker = SurfaceTracker(self.mesh, edge_tolerance=.23)
        xy = self.tracker.triangles[:, :, :2]
        self.lower = tuple(xy.min(axis=(0, 1)))
        self.upper = tuple(xy.max(axis=(0, 1)))

    def contains(self, xy, padding=0):
        return all(lo - padding <= value <= hi + padding for value, lo, hi in zip(xy, self.lower, self.upper))


def connected_ground(tracker, origin, goal, *, start=None, end_height=None, max_length=None, forward_goal=None):
    """Return an unambiguous pair of connected support points."""
    tolerance = tracker.edge_tolerance
    starts = [start] if start is not None else tracker.candidates(origin, edge_tolerance=tolerance)
    ends = tracker.candidates(goal, edge_tolerance=tolerance)
    if len(starts) > 4 or len(ends) > 4:
        return None
    pair = None
    for start in starts:
        for end in ends:
            if end_height is not None and abs(end[2] - end_height) > .3:
                continue
            try:
                if math.dist(start, end) > .1:
                    route = tracker.mesh.plan(Query(tracker.mesh.frame, start, end, horizontal_snap=tolerance))
                    WorldRouteFollower.validate_route(route)
                    if max_length is not None and route.length > max_length:
                        continue
                    if forward_goal is not None and not route_advances_toward(start, route.points, forward_goal):
                        continue
            except NavigationError:
                continue
            if pair is not None:
                return None
            pair = start, end
    return pair


def connected_search_point(tracker, start, heading, visited, *, avoid=()):
    """Choose a nearby observation point on the current walkable surface."""
    centers = tracker.triangles.mean(axis=1)
    deltas = centers[:, :2] - start[:2]
    distances = np.linalg.norm(deltas, axis=1)
    valid = (distances >= 12) & (distances <= 30) & (abs(centers[:, 2] - start[2]) <= 7)
    for point in (*visited, *avoid):
        valid &= np.linalg.norm(centers[:, :2] - point, axis=1) >= 10
    indices = np.flatnonzero(valid)
    if not len(indices):
        return None
    east, north = -math.sin(heading), math.cos(heading)
    costs = abs(distances[indices] - 20) - 7 * (deltas[indices] @ (east, north)) / distances[indices]
    # Check at most two candidates per direction. All candidate paths are
    # validated before the caller can select a point.
    tried = {}
    for index in indices[np.argsort(costs)]:
        dx, dy = deltas[index]
        sector = int((math.atan2(dy, dx) + math.pi) / (math.pi / 4)) % 8
        if tried.get(sector, 0) >= 2:
            continue
        tried[sector] = tried.get(sector, 0) + 1
        end = tuple(float(value) for value in centers[index])
        try:
            route = tracker.mesh.plan(Query(tracker.mesh.frame, start, end, horizontal_snap=tracker.edge_tolerance))
            WorldRouteFollower.validate_route(route)
        except NavigationError:
            continue
        if route.length <= 48:
            return end
    return None
