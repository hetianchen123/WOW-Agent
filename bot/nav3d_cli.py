"""Offline 3D navigation: backend, import/build, query, demo, and HTML preview."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .nav3d import Agent, Frame, NavigationError, NavMesh, Query
from .nav3d.backend import build_backend
from .nav3d.demo import write_scene
from .nav3d.geometry import load_scene


def build_scene(scene_path, output):
    scene, vertices, triangles, sources = load_scene(scene_path)
    mesh = NavMesh.build(vertices, triangles, frame=Frame(**scene["frame"]),
                         agent=Agent(**scene.get("agent", {})), sources=sources,
                         evidence=scene.get("evidence", "unverified_collision_export"))
    mesh.save(output)
    return mesh, vertices, triangles


def run_query(mesh, query_path):
    data = json.loads(Path(query_path).read_text())
    return mesh.plan(Query(**{**data, "frame": Frame(**data["frame"])}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backend", help="compile bundled Recast/Detour locally")
    build = sub.add_parser("build", help="build a bounded collision scene")
    build.add_argument("scene", type=Path); build.add_argument("output", type=Path)
    query = sub.add_parser("query", help="query a complete three-dimensional ground path")
    query.add_argument("mesh", type=Path); query.add_argument("request", type=Path)
    query.add_argument("--output", type=Path, required=True)
    view = sub.add_parser("preview", help="standalone HTML navigation preview")
    view.add_argument("scene", type=Path); view.add_argument("mesh", type=Path)
    view.add_argument("--path", type=Path); view.add_argument("--output", type=Path, required=True)
    demo = sub.add_parser("demo", help="build/query synthetic wall, doorway and stacked-floor fixtures")
    demo.add_argument("--output", type=Path, default=Path("data/nav3d/demo-v1"))
    extract = sub.add_parser("extract-client", help="extract local CASC collision ROI; no game input")
    extract.add_argument("--client", type=Path, required=True)
    extract.add_argument("--product", choices=("wow", "wowf"), default="wow")
    extract.add_argument("--library", type=Path, required=True)
    extract.add_argument("--readers", type=Path, default=Path("data/nav3d/deps/wow.export"))
    extract.add_argument("--map-dbd", type=Path, default=Path("data/nav3d/deps/Map.dbd"))
    extract.add_argument("--listfile", type=Path)
    extract.add_argument("--world-id", type=int, required=True)
    extract.add_argument("--wdt-file", type=int, help="explicit WDT FileDataID when Map.db2 is unavailable")
    extract.add_argument("--phase", required=True)
    extract.add_argument("--roi", nargs=4, type=float, required=True, metavar=("E_MIN", "N_MIN", "E_MAX", "N_MAX"))
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--max-climb", type=float, default=.4,
        help="explicit agent step height in yards; defaults to .4; live capability needs validation")
    probe = sub.add_parser("probe-live", help="record coherent Nav3D telemetry and optionally shadow-plan; never sends input")
    probe.add_argument("--seconds", type=float, default=5)
    probe.add_argument("--mesh", type=Path)
    probe.add_argument("--background-read-only", action="store_true", help="read visible telemetry without foreground requirement; no input executor exists in this probe")
    probe.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract-client":
            from .nav3d.extract import LocalCASC, ClientDecoder, SceneBuilder
            agent=Agent(max_climb=args.max_climb)
            names = {}
            if args.listfile:
                for line in args.listfile.read_text().splitlines():
                    key, name = line.split(";", 1); names[name.lower()] = int(key)
            with LocalCASC(args.client, args.library, product=args.product, cache="data/nav3d/casc-cache") as casc:
                decoder = ClientDecoder(args.readers, "data/nav3d/decoded-input")
                try:
                    builder = SceneBuilder(casc, decoder, [args.roi[:2], args.roi[2:]], names)
                    scene_path = builder.extract(args.world_id, args.map_dbd, args.output, args.phase,
                                                 wdt_file=args.wdt_file)
                    manifest=json.loads(scene_path.read_text());manifest['agent']=asdict(agent)
                    scene_path.write_text(json.dumps(manifest,indent=2))
                    mesh, v, t = build_scene(scene_path, args.output/"mesh")
                    print(json.dumps(dict(scene=str(scene_path), mesh_id=mesh.mesh_id, vertices=len(v), triangles=len(t), reached_in_game=False)))
                finally:
                    decoder.close()
        elif args.command == "probe-live":
            from .nav3d.live_probe import probe_live
            print(json.dumps(probe_live(args.output, seconds=args.seconds, mesh=args.mesh, require_focus=not args.background_read_only), ensure_ascii=False))
        elif args.command == "backend":
            print(json.dumps({"backend": str(build_backend())}))
        elif args.command == "build":
            mesh, v, t = build_scene(args.scene, args.output)
            print(json.dumps({"mesh":str(args.output), "mesh_id":mesh.mesh_id,
                              "vertices":len(v), "triangles":len(t), "frame":asdict(mesh.frame)}))
        elif args.command == "query":
            if args.output.exists():
                raise NavigationError("query_output_already_exists")
            route = run_query(NavMesh.load(args.mesh), args.request)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(route.to_dict(), ensure_ascii=False, indent=2))
            print(json.dumps({"status":"complete", "length_yards":route.length, "points":len(route.points),
                              "output":str(args.output), "reached_in_game":False}))
        elif args.command == "preview":
            from .nav3d.preview import write_preview
            mesh = NavMesh.load(args.mesh)
            scene, v, t, _ = load_scene(args.scene)
            if Frame(**scene["frame"]) != mesh.frame:
                raise NavigationError("preview_scene_frame_mismatch")
            import hashlib
            if hashlib.sha256(v.astype("<f8").tobytes()+t.astype("<i4").tobytes()).hexdigest() != mesh.geometry_hash:
                raise NavigationError("preview_scene_geometry_mismatch")
            path = json.loads(args.path.read_text()) if args.path else None
            if path and (path.get("mesh_id") != mesh.mesh_id or path.get("frame") != asdict(mesh.frame)):
                raise NavigationError("preview_route_mismatch")
            write_preview(args.output, [{"name":args.scene.stem, "vertices":v.tolist(),
                          "triangles":t.tolist(), "navmesh":mesh.debug_triangles().tolist(),
                          "route":path, "frame":asdict(mesh.frame)}])
            print(json.dumps({"preview":str(args.output)}))
        else:
            from .nav3d.preview import write_preview
            if args.output.exists():
                raise NavigationError("demo_output_already_exists")
            scenes, results = [], []
            for name in ("wall", "door", "ramp", "stairs", "stacked", "steep", "ceiling", "narrow"):
                folder = args.output/name
                scene_file, query_file = write_scene(folder, name)
                mesh, v, t = build_scene(scene_file, folder/"mesh")
                try:
                    route = run_query(mesh, query_file).to_dict()
                except NavigationError as exc:
                    route = {"status":"rejected", "reason":str(exc), "reached_in_game":False}
                (folder/"path.json").write_text(json.dumps(route, ensure_ascii=False, indent=2))
                results.append({"scene":name, "status":route["status"], "reason":route.get("reason"),
                                "length_yards":route.get("length")})
                scenes.append({"name":name, "vertices":v.tolist(), "triangles":t.tolist(),
                               "navmesh":mesh.debug_triangles().tolist(), "route":route, "frame":asdict(mesh.frame)})
            write_preview(args.output/"preview.html", scenes)
            (args.output/"results.json").write_text(json.dumps(results, indent=2))
            print(json.dumps({"results":results, "preview":str(args.output/"preview.html"), "reached_in_game":False}))
        return 0
    except (NavigationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status":"rejected", "reason":str(exc), "reached_in_game":False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
