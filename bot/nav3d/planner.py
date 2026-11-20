"""3D navigation contracts, versioned artifacts, and complete-path queries."""
from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import platform
import tempfile
import weakref

import numpy as np

from .backend import (Backend, MAX_PATH_POLYGONS, NavigationError, REVISION,
                      artifact_source_matches, source_id)
from .geometry import validate_geometry


@dataclass(frozen=True)
class Frame:
    client_build: str
    world_id: int  # World/instance map ID, explicitly NOT uiMapID.
    phase_id: str  # Caller-supplied scene variant; unknown is not a wildcard.
    coordinate_system: str

    def __post_init__(self):
        if (not isinstance(self.client_build, str) or not self.client_build.strip()
                or type(self.world_id) is not int or self.world_id < 0
                or not isinstance(self.phase_id, str) or not self.phase_id.strip()
                or not isinstance(self.coordinate_system, str) or not self.coordinate_system.strip()):
            raise NavigationError("invalid_world_frame")


@dataclass(frozen=True)
class Agent:
    cell_size: float = .2
    cell_height: float = .1
    height: float = 2.0
    radius: float = .4
    max_climb: float = .4
    max_slope: float = 45.0

    def __post_init__(self):
        values = tuple(asdict(self).values())
        if (not all(isinstance(x, (float, int)) and math.isfinite(x) for x in values)
                or not .025 <= self.cell_size <= 2 or not .01 <= self.cell_height <= 1
                or not .1 <= self.height <= 10 or not 0 < self.radius <= 5
                or not 0 <= self.max_climb < self.height or not 0 < self.max_slope < 85
                or self.height / self.cell_height < 3):
            raise NavigationError("invalid_agent_dimensions")


def point3(value):
    try:
        result = tuple(float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise NavigationError("finite_xyz_required") from exc
    if len(result) != 3 or not all(math.isfinite(v) and abs(v) <= 10000000 for v in result):
        raise NavigationError("finite_xyz_required")
    return result


@dataclass(frozen=True)
class Query:
    frame: Frame
    start: tuple[float, float, float]
    goal: tuple[float, float, float]
    horizontal_snap: float = .5
    vertical_snap: float = .3
    spacing: float = .25
    blocked_boxes: tuple = ()  # ((min_x,min_y,min_z),(max_x,max_y,max_z))

    def __post_init__(self):
        point3(self.start); point3(self.goal)
        if (not all(math.isfinite(x) and .001 <= x <= 2 for x in
                    (self.horizontal_snap, self.vertical_snap))
                or not math.isfinite(self.spacing) or not .01 <= self.spacing <= 2):
            raise NavigationError("invalid_query_tolerance")
        if len(self.blocked_boxes) > 1024:
            raise NavigationError("obstacle_limit")
        for box in self.blocked_boxes:
            if len(box) != 2 or any(a >= b for a, b in zip(point3(box[0]), point3(box[1]))):
                raise NavigationError("invalid_blocked_box")


@dataclass(frozen=True)
class Path3D:
    frame: Frame
    mesh_id: str
    points: tuple[tuple[float, float, float], ...]
    requested_start: tuple
    requested_goal: tuple
    length: float
    obstacle_revision: str
    evidence: str = "offline_collision_geometry"
    reached_in_game: bool = False

    def to_dict(self):
        return {"schema": 1, "status": "complete", **asdict(self)}


class NavMesh:
    def __init__(self, frame, agent, blob, *, origin, geometry_hash, sources=(),
                 evidence="unverified_collision_export", tile_cells=0):
        if type(tile_cells) is not int or (tile_cells and not 32 <= tile_cells <= 512):
            raise NavigationError("tile_cell_limit")
        self.frame, self.agent, self.blob = frame, agent, blob
        self.tile_cells = tile_cells
        self.origin = point3(origin)
        self.geometry_hash, self.sources, self.evidence = geometry_hash, tuple(sources), evidence
        self.mesh_id = hashlib.sha256(blob).hexdigest()
        self.backend = Backend()
        self._native_mesh = self.backend.prepare(blob)
        weakref.finalize(self, self.backend.lib.wa_close_mesh, self._native_mesh)

    @classmethod
    def build(cls, vertices, triangles, *, frame, agent=Agent(), sources=(),
              evidence="unverified_collision_export", tile_cells=0):
        v, t = validate_geometry(vertices, triangles)
        # Rebase large WoW world positions to preserve local float32 precision.
        origin = np.floor(v.min(axis=0))
        geometry_hash = hashlib.sha256(v.astype("<f8").tobytes()+t.astype("<i4").tobytes()).hexdigest()
        blob, _ = Backend().build(v-origin, t, tuple(asdict(agent).values()), tile_cells=tile_cells)
        return cls(frame, agent, blob, origin=origin, geometry_hash=geometry_hash,
                   sources=sources, evidence=evidence, tile_cells=tile_cells)

    def plan(self, query: Query) -> Path3D:
        return self._plan(query)[0]

    def plan_corridor(self, query: Query):
        if query.blocked_boxes:
            raise NavigationError("corridor_requires_static_mesh")
        refs = (ctypes.c_uint64 * MAX_PATH_POLYGONS)()
        route, count = self._plan(query, refs)
        return route, RouteCorridor(self, route, query.spacing, refs, count)

    def _plan(self, query: Query, refs=None):
        if query.frame != self.frame:
            raise NavigationError("world_build_phase_or_coordinate_mismatch")
        start, goal = np.array(query.start)-self.origin, np.array(query.goal)-self.origin
        boxes = np.array(query.blocked_boxes, dtype=float).reshape(-1, 2, 3).copy()
        if len(boxes):
            boxes -= self.origin
            boxes[:, 0, :2] -= self.agent.radius
            boxes[:, 1, :2] += self.agent.radius
        points = self.backend.query(self.blob, start, goal, query.horizontal_snap,
                                    query.vertical_snap, query.spacing, boxes, prepared=self._native_mesh,
                                    corridor=refs)
        count = 0
        if refs is not None:
            points, count = points
        points += self.origin
        if not len(points) or not np.isfinite(points).all():
            raise NavigationError("invalid_backend_path")
        # Recheck endpoints independently of native status bits.
        for actual, expected in ((points[0], query.start), (points[-1], query.goal)):
            if np.linalg.norm(actual[:2]-np.array(expected[:2])) > query.horizontal_snap+1e-4 or abs(actual[2]-expected[2]) > query.vertical_snap+1e-4:
                raise NavigationError("endpoint_snap_exceeded")
        length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
        route = Path3D(self.frame, self.mesh_id, tuple(map(tuple, points.tolist())),
                      point3(query.start), point3(query.goal), length,
                      hashlib.sha256(json.dumps(query.blocked_boxes).encode()).hexdigest(), self.evidence)
        return route, count

    def debug_triangles(self):
        return self.backend.debug(self.blob)+self.origin

    @property
    def clearance_agent(self):
        return replace(self.agent, radius=round(self.agent.radius + self.agent.cell_size, 9))

    def save(self, folder, *, clearance=None):
        folder = Path(folder)
        if folder.exists():
            raise NavigationError("artifact_already_exists")
        folder.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"schema": 1, "frame": asdict(self.frame), "agent": asdict(self.agent),
                    "origin": self.origin, "geometry_sha256": self.geometry_hash,
                    "mesh_sha256": self.mesh_id, "backend_source_id": source_id(),
                    "recast_revision": REVISION, "machine": platform.machine(),
                    "sources": self.sources, "evidence": self.evidence, "reached_in_game": False}
        if self.tile_cells:
            metadata["tile_cells"] = self.tile_cells
        with tempfile.TemporaryDirectory(dir=folder.parent) as temporary:
            staging = Path(temporary)/"artifact"
            staging.mkdir()
            (staging/"mesh.bin").write_bytes(self.blob)
            (staging/"manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
            if clearance is not None:
                clearance.save(staging / "clearance")
            staging.rename(folder)

    @classmethod
    def load(cls, folder):
        folder = Path(folder)
        meta = json.loads((folder/"manifest.json").read_text())
        if not isinstance(meta, dict):
            raise NavigationError("invalid_navmesh_manifest")
        tile_cells = meta.get("tile_cells", 0)
        if (meta.get("schema") != 1 or not artifact_source_matches(meta.get("backend_source_id"), tiled=bool(tile_cells))
                or meta.get("recast_revision") != REVISION or meta.get("machine") != platform.machine()):
            raise NavigationError("artifact_backend_mismatch_rebuild_required")
        if not 100 < (folder/"mesh.bin").stat().st_size < 128*1024*1024:
            raise NavigationError("artifact_size_invalid")
        blob = (folder/"mesh.bin").read_bytes()
        if hashlib.sha256(blob).hexdigest() != meta["mesh_sha256"]:
            raise NavigationError("artifact_hash_mismatch")
        return cls(Frame(**meta["frame"]), Agent(**meta["agent"]), blob, origin=meta["origin"],
                   geometry_hash=meta["geometry_sha256"], sources=meta["sources"], evidence=meta["evidence"],
                   tile_cells=tile_cells)


class RouteCorridor:
    """A static Detour polygon path anchored to the latest verified support."""

    def __init__(self, mesh, route, spacing, refs, count):
        self.mesh, self.route, self.spacing = mesh, route, spacing
        self.refs, self.count = refs, count
        self.anchor = np.array(route.points[0]) - mesh.origin
        self.goal = np.array(route.points[-1]) - mesh.origin
        self.observed = route.requested_start

    def advance(self, position, *, edge_tolerance):
        position = point3(position)
        if position != self.observed:
            observed = np.array(position) - self.mesh.origin
            self.anchor, self.count = self.mesh.backend.advance_corridor(self.mesh._native_mesh,
                self.anchor, observed, edge_tolerance, self.refs, self.count)
            self.observed = position

    def remaining_route(self):
        points = self.mesh.backend.render_corridor(self.mesh._native_mesh, self.anchor, self.goal,
                                                   self.spacing, self.refs, self.count)
        if len(points) == 0 or not np.isfinite(points).all() or not np.allclose(points[-1], self.goal, rtol=0, atol=.003):
            raise NavigationError("invalid_corridor_path")
        points += self.mesh.origin
        return replace(self.route, points=tuple(map(tuple, points.tolist())), requested_start=self.observed,
                       length=float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum()))

    def forward_clearance(self, heading, limit):
        end = self.anchor + (-math.sin(heading) * limit, math.cos(heading) * limit, 0.)
        fraction = self.mesh.backend.corridor_raycast(self.mesh._native_mesh, self.refs[0], self.anchor, end)
        return fraction * limit
