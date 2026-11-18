"""Small synthetic collision scenes; explicitly not extracted WoW assets."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


class Geometry:
    def __init__(self):
        self.vertices, self.triangles = [], []

    def quad(self, a, b, c, d):
        n = len(self.vertices)
        self.vertices.extend((a, b, c, d))
        self.triangles.extend(((n, n+1, n+2), (n, n+2, n+3)))

    def floor(self, x0, y0, x1, y1, z=0):
        self.quad((x0,y0,z), (x1,y0,z), (x1,y1,z), (x0,y1,z))

    def box(self, lo, hi):
        x,y,z = lo; X,Y,Z = hi
        self.floor(x,y,X,Y,Z)
        self.quad((x,y,z),(x,Y,z),(X,Y,z),(X,y,z))
        self.quad((x,y,z),(X,y,z),(X,y,Z),(x,y,Z))
        self.quad((X,Y,z),(x,Y,z),(x,Y,Z),(X,Y,Z))
        self.quad((x,Y,z),(x,y,z),(x,y,Z),(x,Y,Z))
        self.quad((X,y,z),(X,Y,z),(X,Y,Z),(X,y,Z))

    def arrays(self):
        return np.array(self.vertices, dtype=float), np.array(self.triangles, dtype=np.int32)

    def obj(self):
        return "# Synthetic navigation test geometry; Z-up, yards.\n" + "".join(
            "v %g %g %g\n" % tuple(v) for v in self.vertices) + "".join(
            "f %d %d %d\n" % tuple(i+1 for i in t) for t in self.triangles)


def scene(name):
    g = Geometry()
    if name == "wall":
        g.floor(0,0,24,20)
        g.box((11,0,0),(12,14,4))
        start, goal = (3,8,0), (21,8,0)
    elif name == "door":
        g.floor(0,0,24,18)
        g.box((11,0,0),(12,7,4)); g.box((11,10,0),(12,18,4))
        g.box((11,7,3),(12,10,4))
        start, goal = (3,8.5,0), (21,8.5,0)
    elif name in {"ramp", "stairs", "stacked", "steep"}:
        g.floor(0,0,24,16)
        g.box((16,0,3.8),(24,16,4))
        if name in {"ramp", "steep"}:
            begin = 8 if name == "ramp" else 14
            g.quad((begin,0,0),(16,0,4),(16,5,4),(begin,5,0))
        elif name == "stairs":
            for i in range(16):
                g.box((8+i*.5,0,0),(8+(i+1)*.5,5,(i+1)*.25))
        start, goal = (3,2.5,0), (21,10,4)
    elif name == "ceiling":
        g.floor(0,0,24,8)
        g.box((10,0,1.4),(14,8,1.7))
        start, goal = (3,4,0), (21,4,0)
    elif name == "narrow":
        g.floor(0,0,24,8)
        g.box((10,0,0),(14,3.75,4)); g.box((10,4.25,0),(14,8,4))
        start, goal = (3,4,0), (21,4,0)
    else:
        raise ValueError(name)
    return g, start, goal


def write_scene(folder, name):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    g, start, goal = scene(name)
    (folder/"collision.obj").write_text(g.obj())
    frame = {"client_build":"synthetic-v1", "world_id":0,
             "phase_id":name, "coordinate_system":"east_north_up"}
    data = {"schema":1, "geometry_kind":"collision", "units":"yards", "up_axis":"Z",
            "frame":frame, "evidence":"synthetic_fixture",
            "instances":[{"path":"collision.obj", "transform":np.eye(4).tolist()}]}
    (folder/"scene.json").write_text(json.dumps(data, indent=2))
    (folder/"query.json").write_text(json.dumps({"frame":frame, "start":start, "goal":goal}, indent=2))
    return folder/"scene.json", folder/"query.json"
