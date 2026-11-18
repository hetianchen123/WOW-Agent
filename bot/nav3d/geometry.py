"""Strict triangle OBJ / transformed collision-scene import, with provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .backend import NavigationError


def validate_geometry(vertices, triangles):
    v, t = np.asarray(vertices, dtype=float), np.asarray(triangles)
    if (v.ndim != 2 or v.shape[1] != 3 or len(v) < 3 or len(v) > 2000000
            or not np.isfinite(v).all() or np.abs(v).max() > 10000000):
        raise NavigationError("invalid_vertices")
    if (t.ndim != 2 or t.shape[1] != 3 or not 0 < len(t) <= 2000000
            or t.dtype.kind not in "iu" or t.min() < 0 or t.max() >= len(v)):
        raise NavigationError("invalid_triangles")
    t = t.astype(np.int32)
    normal = np.cross(v[t[:, 1]]-v[t[:, 0]], v[t[:, 2]]-v[t[:, 0]])
    if (np.linalg.norm(normal, axis=1) < 1e-10).any():
        raise NavigationError("degenerate_triangles")
    return v, t


def read_obj(path):
    """Only triangles: blindly fan-triangulating concave faces can bridge walls."""
    path = Path(path)
    if path.stat().st_size > 256 * 1024 * 1024:
        raise NavigationError("obj_size_limit")
    vertices, faces = [], []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        try:
            if fields[0] == "v":
                if len(fields) != 4:
                    raise ValueError("vertex must have three coordinates")
                vertices.append(tuple(map(float, fields[1:])))
            elif fields[0] == "f":
                if len(fields) != 4:
                    raise ValueError("triangulate faces before import")
                indices = [int(x.split("/")[0]) for x in fields[1:]]
                if 0 in indices:
                    raise ValueError("zero vertex index")
                faces.append(tuple(i-1 if i > 0 else len(vertices)+i for i in indices))
            elif fields[0] in {"l", "p", "curv", "surf"}:
                raise ValueError("non-triangle geometry")
        except ValueError as exc:
            raise NavigationError(f"obj_line_{line_no}: {exc}") from exc
        if len(vertices) > 2000000 or len(faces) > 2000000:
            raise NavigationError("geometry_size_limit")
    return validate_geometry(vertices, faces)


def load_scene(path):
    """Each instance's affine matrix maps source OBJ to Z-up world units (yards).

    No axis/scale/placement is guessed from a filename or exporter version.
    Missing referenced objects are errors, never silently skipped obstacles.
    """
    path = Path(path).resolve()
    scene = json.loads(path.read_text())
    if scene.get("schema") != 1 or scene.get("geometry_kind") != "collision":
        raise NavigationError("collision_scene_required")
    if scene.get("units") != "yards" or scene.get("up_axis") != "Z":
        raise NavigationError("explicit_z_up_yards_required")
    instances = scene.get("instances")
    if not isinstance(instances, list) or not 1 <= len(instances) <= 10000:
        raise NavigationError("invalid_scene_instances")
    vertices, triangles, records, offset = [], [], [], 0
    for entry in instances:
        source = (path.parent / entry["path"]).resolve()
        v, t = read_obj(source)
        matrix = np.asarray(entry["transform"], dtype=float)
        if (matrix.shape != (4, 4) or not np.isfinite(matrix).all()
                or not np.allclose(matrix[3], [0, 0, 0, 1])
                or abs(np.linalg.det(matrix[:3, :3])) < 1e-12):
            raise NavigationError("invalid_instance_transform")
        v = (np.c_[v, np.ones(len(v))] @ matrix.T)[:, :3]
        if np.linalg.det(matrix[:3, :3]) < 0:
            t = t[:, [0, 2, 1]]
        vertices.append(v); triangles.append(t+offset); offset += len(v)
        if offset > 2000000 or sum(len(x) for x in triangles) > 2000000:
            raise NavigationError("scene_geometry_limit")
        records.append({"path": entry["path"], "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "transform": matrix.tolist()})
    v, t = validate_geometry(np.concatenate(vertices), np.concatenate(triangles))
    return scene, v, t, records
