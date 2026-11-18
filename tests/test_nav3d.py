"""Real Recast/Detour integration on synthetic geometry, NOT in-game tests."""
from dataclasses import asdict, replace
import json
import math

import numpy as np
import pytest

from bot.nav3d import Agent, Frame, NavigationError, NavMesh, Query
from bot.nav3d.backend import from_native, to_native
from bot.nav3d.demo import Geometry, scene, write_scene
from bot.nav3d.follower import WorldPose, WorldRouteFollower
from bot.nav3d.geometry import load_scene, read_obj
from bot.nav3d_cli import build_scene, main


FRAME = Frame("synthetic-v1", 0, "test", "east_north_up")


@pytest.fixture(scope="module")
def meshes():
    result = {}
    for name in ("wall", "door", "ramp", "stairs", "stacked", "steep", "ceiling", "narrow"):
        g, start, goal = scene(name)
        result[name] = NavMesh.build(*g.arrays(), frame=FRAME, evidence="synthetic_fixture"), start, goal
    return result


def route(meshes, name, **kwargs):
    mesh, start, goal = meshes[name]
    return mesh.plan(Query(FRAME, start, goal, **kwargs))


def test_wall_is_avoided_by_every_segment(meshes):
    path = route(meshes, "wall")
    assert path.length > math.dist(path.requested_start, path.requested_goal)+3
    assert max(p[1] for p in path.points) > 14.3
    for a, b in zip(path.points, path.points[1:]):
        for weight in np.linspace(0, 1, 20):
            p = np.array(a)*(1-weight)+np.array(b)*weight
            nearest = np.array([np.clip(p[0], 11, 12), np.clip(p[1], 0, 14)])
            assert np.linalg.norm(p[:2]-nearest) >= .39
    assert path.reached_in_game is False


def test_doorway_keeps_body_clear_of_jambs(meshes):
    path = route(meshes, "door")
    crossing = [p for p in path.points if 10.6 < p[0] < 12.4]
    assert crossing and all(7.4 <= p[1] <= 9.6 for p in crossing)
    assert path.length < 19


@pytest.mark.parametrize("name", ["ramp", "stairs"])
def test_real_height_changes_reach_upper_floor(meshes, name):
    path = route(meshes, name)
    assert abs(path.points[0][2]) <= .2 and abs(path.points[-1][2]-4) <= .2
    middle = [p for p in path.points if .5 < p[2] < 3.5]
    assert middle and all(7.5 < p[0] < 16.5 and 0 < p[1] < 5 for p in middle)
    assert max(math.dist(a,b) for a,b in zip(path.points,path.points[1:])) < .6


@pytest.mark.parametrize("name", ["stacked", "steep", "ceiling", "narrow"])
def test_unreachable_is_not_returned_as_partial_success(meshes, name):
    with pytest.raises(NavigationError, match="no_complete_path"):
        route(meshes, name)


def test_stacked_identical_xy_does_not_teleport_floors(meshes):
    mesh, _, _ = meshes["stacked"]
    with pytest.raises(NavigationError, match="no_complete_path"):
        mesh.plan(Query(FRAME, (20,8,0), (20,8,4)))
    lower = mesh.plan(Query(FRAME, (18,8,0), (22,8,0)))
    upper = mesh.plan(Query(FRAME, (18,8,4), (22,8,4)))
    assert max(p[2] for p in lower.points) < .2
    assert min(p[2] for p in upper.points) > 3.9


def test_closed_door_disables_passage_without_mutating_cache(meshes):
    with pytest.raises(NavigationError, match="no_complete_path|off_mesh"):
        route(meshes,"door", blocked_boxes=(((10.9,7,0),(12.1,10,3)),))
    assert route(meshes,"door").length < 19


def test_obstacle_height_does_not_disable_other_floor(meshes):
    mesh, _, _ = meshes["stacked"]
    result = mesh.plan(Query(FRAME,(18,8,0),(22,8,0),blocked_boxes=(((16,0,4),(24,16,6)),)))
    assert result.length < 5
    with pytest.raises(NavigationError, match="off_mesh"):
        mesh.plan(Query(FRAME,(18,8,4),(22,8,4),blocked_boxes=(((16,0,4),(24,16,6)),)))


@pytest.mark.parametrize("change", [{"client_build":"old"}, {"world_id":2451},
                                    {"phase_id":"different"}, {"coordinate_system":"wrong"}])
def test_world_identity_is_not_interchangeable(meshes, change):
    mesh, start, goal = meshes["door"]
    with pytest.raises(NavigationError, match="mismatch"):
        mesh.plan(Query(replace(FRAME, **change),start,goal))


@pytest.mark.parametrize("point", [(21,8,2), (500,500,0), (11.5,5,0)])
def test_missing_surface_and_wrong_height_not_snapped_to_success(meshes, point):
    mesh, start, _ = meshes["door"]
    with pytest.raises(NavigationError, match="off_mesh|snap"):
        mesh.plan(Query(FRAME,start,point))


def test_large_world_coordinates_keep_precision():
    g, a, b = scene("ramp"); v, t = g.arrays()
    offset = np.array([143000.,-215000.,1300.])
    mesh = NavMesh.build(v+offset,t,frame=FRAME)
    path = mesh.plan(Query(FRAME,tuple(np.array(a)+offset),tuple(np.array(b)+offset)))
    assert math.dist(path.points[-1],np.array(b)+offset) < .2
    assert min(p[2] for p in path.points) >= 1300


def test_coordinate_handedness_roundtrip():
    points = [[1,2,3],[-6,8,4]]
    np.testing.assert_allclose(from_native(to_native(points)), points)


@pytest.mark.parametrize("change", [{"spacing":1e-300}, {"horizontal_snap":float("inf")},
                                    {"goal":(1e300,0,0)}, {"goal":(1,2,None)}])
def test_invalid_query_never_reaches_native(change):
    with pytest.raises(NavigationError):
        Query(**{"frame":FRAME,"start":(2,2,0),"goal":(8,8,0),**change})


@pytest.mark.parametrize("change", [{"radius":0}, {"cell_size":0}, {"height":float("nan")},
                                    {"max_climb":2}, {"max_slope":90}])
def test_bad_agent_rejected_before_native(change):
    with pytest.raises(NavigationError):
        Agent(**change)


def test_mesh_size_bound_rejects_continent_before_allocation():
    g=Geometry(); g.floor(0,0,10000,10000)
    with pytest.raises(NavigationError, match="region_.*limit"):
        NavMesh.build(*g.arrays(),frame=FRAME)


def test_persisted_mesh_reloads_and_checks_hash(meshes, tmp_path):
    mesh, a, b = meshes["ramp"]
    folder=tmp_path/"cache"; mesh.save(folder)
    loaded=NavMesh.load(folder)
    assert loaded.plan(Query(FRAME,a,b)).points == mesh.plan(Query(FRAME,a,b)).points
    assert np.isfinite(loaded.debug_triangles()).all()
    with pytest.raises(NavigationError,match="already_exists"):
        mesh.save(folder)
    blob=bytearray((folder/"mesh.bin").read_bytes()); blob[-1]^=1
    (folder/"mesh.bin").write_bytes(blob)
    with pytest.raises(NavigationError,match="hash_mismatch"):
        NavMesh.load(folder)


def test_backend_version_mismatch_requires_rebuild(meshes,tmp_path):
    mesh,_,_=meshes["door"]; mesh.save(tmp_path/"mesh")
    p=tmp_path/"mesh/manifest.json"; meta=json.loads(p.read_text())
    meta["backend_source_id"]="different"; p.write_text(json.dumps(meta))
    with pytest.raises(NavigationError,match="rebuild_required"):
        NavMesh.load(p.parent)


def test_scene_instances_have_explicit_placement_and_handedness(tmp_path):
    source,_=write_scene(tmp_path,"door")
    raw=json.loads(source.read_text())
    raw["instances"][0]["transform"]=[[-1,0,0,100],[0,1,0,20],[0,0,1,0],[0,0,0,1]]
    source.write_text(json.dumps(raw))
    mesh,v,t=build_scene(source,tmp_path/"built")
    path=mesh.plan(Query(mesh.frame,(97,28.5,0),(79,28.5,0)))
    assert path.length < 19 and v[:,0].min() == 76
    assert len(mesh.sources) == 1 and len(mesh.sources[0]["sha256"]) == 64


def test_obj_negative_indices_and_concave_rejection(tmp_path):
    p=tmp_path/"a.obj"
    p.write_text("v 0 0 0\nv 2 0 0\nv 0 2 0\nf -3/1 -2/2 -1/3\n")
    _,t=read_obj(p); assert t.tolist()==[[0,1,2]]
    p.write_text(p.read_text()+"f 1 2 3 1\n")
    with pytest.raises(NavigationError,match="triangulate"):
        read_obj(p)


def test_missing_asset_and_render_only_geometry_not_accepted(tmp_path):
    source,_=write_scene(tmp_path,"wall")
    data=json.loads(source.read_text()); data["geometry_kind"]="render"
    source.write_text(json.dumps(data))
    with pytest.raises(NavigationError,match="collision_scene_required"):
        load_scene(source)
    data["geometry_kind"]="collision"; data["instances"][0]["path"]="missing.obj"
    source.write_text(json.dumps(data))
    with pytest.raises(FileNotFoundError):
        load_scene(source)


def test_cli_end_to_end_and_machine_readable_failure(tmp_path,capsys):
    scene_file,query_file=write_scene(tmp_path/"input","door")
    mesh=tmp_path/"mesh"; result=tmp_path/"route.json"
    assert main(["build",str(scene_file),str(mesh)])==0
    assert main(["query",str(mesh),str(query_file),"--output",str(result)])==0
    data=json.loads(result.read_text()); assert data["status"]=="complete" and not data["reached_in_game"]
    assert main(["query",str(mesh),str(query_file),"--output",str(result)])==2
    assert json.loads(capsys.readouterr().err)["reason"]=="query_output_already_exists"
    assert json.loads(result.read_text())==data
    html=tmp_path/"preview.html"
    assert main(["preview",str(scene_file),str(mesh),"--path",str(result),"--output",str(html)])==0
    assert "cdn" not in html.read_text() and "__SCENES__" not in html.read_text()
    q=json.loads(query_file.read_text());q["goal"]=[90,90,0];query_file.write_text(json.dumps(q))
    assert main(["query",str(mesh),str(query_file),"--output",str(tmp_path/"bad.json")])==2
    assert json.loads(capsys.readouterr().err)["status"]=="rejected"
    assert not (tmp_path/"bad.json").exists()


def follower_and_pose(meshes):
    path=route(meshes,"door")
    f=WorldRouteFollower(path,now=0,mesh_id=path.mesh_id,obstacle_revision=path.obstacle_revision,alignment_verified=True)
    p=WorldPose(FRAME,path.points[0],3*math.pi/2,1,0,True,True)
    return f,p,{"mesh_id":path.mesh_id,"obstacle_revision":path.obstacle_revision}


def test_follower_consumes_full_3d_path_without_claiming_quest_success(meshes):
    f,p,kwargs=follower_and_pose(meshes)
    command=f.step(0,p,**kwargs); assert command.tap=="w"
    assert f.step(.01,p,**kwargs).tap is None
    for i,point in enumerate(f.route.points[1:],2):
        command=f.step(i*.05,replace(p,position=point,sequence=i,captured_at=i*.05),**kwargs)
    assert command.outcome=="nav3d_route_arrived"
    assert not f.route.reached_in_game


@pytest.mark.parametrize("change,reason", [({"grounded":False},"unsafe"),({"in_combat":True},"unsafe"),
    ({"bindings_verified":False},"unsafe"),({"position":(3,8.5,4)},"start_mismatch"),
    ({"position":(float('nan'),0,0)},"position_unavailable"),({"captured_at":-1},"stale"),
    ({"frame":replace(FRAME,phase_id="other")},"frame_changed")])
def test_follower_releases_on_unusable_pose(meshes,change,reason):
    f,p,kwargs=follower_and_pose(meshes)
    command=f.step(0,replace(p,**change),**kwargs)
    assert reason in command.outcome and not command.keys and command.tap is None


def test_follower_dynamic_change_requires_replan(meshes):
    f,p,kwargs=follower_and_pose(meshes)
    kwargs["obstacle_revision"]="changed"
    assert f.step(0,p,**kwargs).outcome=="nav3d_replan_required"


def test_follower_unverified_alignment_never_sends_input(meshes):
    f,p,kwargs=follower_and_pose(meshes);f.alignment_verified=False
    assert f.step(0,p,**kwargs).outcome=="nav3d_world_alignment_unverified"


def test_follower_stuck_feedback_is_bounded(meshes):
    f,p,kwargs=follower_and_pose(meshes)
    for i in range(26):
        command=f.step(i*.1,replace(p,sequence=i,captured_at=i*.1),**kwargs)
    assert command.outcome=="nav3d_stuck_replan_required"


def test_follower_turns_have_feedback_and_rhythm(meshes):
    f,p,kwargs=follower_and_pose(meshes);p=replace(p,heading=0)
    first=f.step(0,p,**kwargs);assert first.tap=="right" and first.hold_s<=.12
    assert f.step(.1,replace(p,sequence=2,captured_at=.1),**kwargs).tap is None
    assert f.step(.5,replace(p,sequence=3,captured_at=.5),**kwargs).tap=="right"


def test_follower_cancel_and_timeout_release_keys(meshes):
    f,p,kwargs=follower_and_pose(meshes)
    assert f.step(0,p,cancelled=True,**kwargs).outcome=="nav3d_cancelled"
    assert f.step(.1,p,**kwargs).tap is None
    f,p,kwargs=follower_and_pose(meshes)
    assert f.step(91,p,**kwargs).outcome=="nav3d_budget_exhausted"
