"""Mesh exit candidates for releasing ground travel to the legacy stack.

The 2026-11-18 split policy (user decision): Nav3D owns mesh-covered scenes
(buildings, cities); open-field travel reuses the legacy straight-line stack.
When the active goal lies outside mesh coverage, Nav3D first walks to a mesh
boundary point toward that goal. Hull vertices are only geometric candidates;
a complete planned route decides which one is a real exit.
"""
from __future__ import annotations

import math

import numpy as np


def _hull_xy(points):
    """Monotone-chain convex hull of 2D points; collinear points dropped."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1]+upper[:-1]


def mesh_exit_candidates(mesh, goal_xy, *, limit=8):
    """Hull vertices (x,y,z) sorted by XY distance to goal, ground Z first.

    Vertices sharing an XY footprint (ground edge vs rooftop) keep ascending Z
    order so ground exits are tried before elevated hull points; planning
    rejects disconnected candidates either way.
    """
    goal = (float(goal_xy[0]), float(goal_xy[1]))
    triangles = np.asarray(mesh.debug_triangles(), dtype=float)
    footprints = np.round(triangles[:, :, :2].reshape(-1, 2), 2)
    heights = triangles[:, :, 2].reshape(-1)
    grouped = {}
    for (x, y), z in zip(footprints, heights):
        grouped.setdefault((float(x), float(y)), []).append(float(z))
    candidates = []
    for xy in sorted(_hull_xy(grouped), key=lambda p: math.dist(p, goal)):
        for z in sorted(set(grouped[xy])):
            candidates.append((*xy, z))
    return candidates[:limit]
