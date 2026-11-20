"""Behavior/geometry checks. These fixtures are not live game success evidence."""
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import json
import math
import subprocess
import numpy as np
import pytest
from bot.core.config import Nav3DConfig
from bot.nav3d import Frame, NavMesh, NavigationError
from bot.nav3d.planner import Agent
from bot.nav3d.demo import Geometry, scene, write_scene
from bot.nav3d.edge_entry import plan_edge_entry
from bot.nav3d.extract import ALL_TERRAIN_CELLS, liquid_cells, terrain, placement_matrix, quaternion_matrix
from bot.nav3d import ground_trial
from bot.nav3d.runtime import SurfaceTracker, Nav3DCoordinator
from bot.nav3d_cli import build_scene
from bot.perception.nav3d_channel import FastPoseSample, Nav3DSample, decode_fast_pose, decode_nav3d, ROWS, with_fast_pose
from bot.perception import channel_spec as spec


def test_terrain_winding_holes_and_coordinates():
    ch=dict(position=[-900,-2100,50],vertices=[0]*145,flags=0,holesLowRes=1,holesHighRes=[0]*8)
    v,t=terrain(ch)
    assert len(t)==240  # one legacy hole removes 2x2 cells
    assert np.all(np.cross(v[t[:,1]]-v[t[:,0]],v[t[:,2]]-v[t[:,0]])[:,2]>0)
    assert tuple(v[0])==(2100,-900,50)
    ch.update(flags=0x10000,holesHighRes=[1]+[0]*7)
    assert len(terrain(ch)[1])==252


def test_liquid_mask_preserves_dry_cells_in_a_partly_wet_chunk():
    partial = dict(xOffset=2, yOffset=4, width=5, height=2, offsetExistsBitmap=40, bitmap=[1, 2])
    rectangle = dict(xOffset=0, yOffset=0, width=2, height=1, offsetExistsBitmap=0)
    wet = liquid_cells([partial, rectangle])
    expected_cells = ((2, 4), (6, 5), (0, 0), (1, 0))
    assert wet == sum(1 << (y * 8 + x) for x, y in expected_cells)
    chunk = dict(position=[0, 0, 0], vertices=[0]*145, flags=0, holesLowRes=0, holesHighRes=[0]*8)
    _, triangles = terrain(chunk, wet)
    assert len(triangles) == (64 - len(expected_cells)) * 4
    for x, y in expected_cells:
        assert y * 17 + 9 + x not in triangles
    assert liquid_cells([{**partial, "bitmap": [1]}]) == ALL_TERRAIN_CELLS
    assert liquid_cells([{**partial, "xOffset": 4}]) == ALL_TERRAIN_CELLS


def test_prepared_ground_trial_request_is_consumed_once_and_rejects_expiry(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    monkeypatch.setattr(ground_trial, "REQUEST", request)
    monkeypatch.setattr(ground_trial.time, "time", lambda: 500.)
    payload = dict(issued=450., mesh="mesh", mesh_id="digest", start=[2., 2.], goal=[2., 4.],
                   capture="stream", pose_source="legacy", output=str(tmp_path / "receipt.json"))
    request.write_text(json.dumps(payload))
    old_request = ground_trial.take_prepared_trial()
    assert old_request.goal == [2., 4.] and old_request.dispatch == "auto" and old_request.profile == "short"
    assert ground_trial.take_prepared_trial() is None
    request.write_text(json.dumps({**payload, "dispatch": "native", "profile": "continuous"}))
    request_data = ground_trial.take_prepared_trial()
    assert request_data.dispatch == "native" and request_data.profile == "continuous"
    request.write_text(json.dumps({**payload, "issued": 370.}))
    with pytest.raises(NavigationError, match="expired"):
        ground_trial.take_prepared_trial()
    assert ground_trial.take_prepared_trial() is None


def test_required_native_trial_never_uses_synchronous_fallback():
    calls = []
    backend = SimpleNamespace(start_navigation_pulse=lambda *args: False, tap_precise=lambda *args: calls.append(args))
    command = SimpleNamespace(tap="w", hold_s=.05)
    with pytest.raises(NavigationError, match="native_timer_unavailable"):
        ground_trial.pulse(backend, command, "native")
    assert not calls
    assert ground_trial.pulse(backend, command, "controller") == "controller"
    assert calls == [("w", .05)]


def test_prepared_trial_launch_timeout_retains_bounded_request(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    monkeypatch.setattr(ground_trial, "REQUEST", request)

    def no_ack(command, *, check):
        raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(ground_trial.subprocess, "run", no_ack)
    args = SimpleNamespace(mesh=tmp_path / "mesh", mesh_id="digest", start=(2., 2.), goal=(2., 4.),
                           capture="stream", pose_source="fast", dispatch="native", profile="short", entry_scene=None,
                           output=tmp_path / "receipt.json")
    assert ground_trial.queue_trial(args) == 2
    queued = ground_trial.take_prepared_trial()
    assert queued.goal == [2., 4.] and queued.dispatch == "native"
    assert ground_trial.take_prepared_trial() is None


def test_mesh_entry_uses_same_source_and_checks_entire_ground_contact(tmp_path):
    source = tmp_path / "floor.obj"
    floor = "v 0 0 0\nv 10 0 0\nv 10 10 0\nv 0 10 0\nf 1 2 3\nf 1 3 4\n"
    source.write_text(floor)
    scene_file = tmp_path / "scene.json"
    frame = Frame("fixture", 0, "quest:1:2", "east_north_up")
    scene_file.write_text(json.dumps(dict(schema=1, geometry_kind="collision", units="yards", up_axis="Z",
        frame=asdict(frame), instances=[dict(path=source.name, transform=np.eye(4).tolist())])))
    mesh, _, _ = build_scene(scene_file, tmp_path / "mesh")
    tracker = SurfaceTracker(mesh)

    entry = plan_edge_entry(mesh, tracker, scene_file, (.58, 5.), (1.48, 5.))
    assert entry.route.length == pytest.approx(.9)
    assert entry.position((1., 5.)) == (1., 5., 0.)
    with pytest.raises(NavigationError, match="deviation"):
        entry.position((1., 5.2))
    with pytest.raises(NavigationError, match="ground_gap"):
        plan_edge_entry(mesh, tracker, scene_file, (.56, 5.), (1.46, 5.))

    source.write_text(floor.replace("v 10 0 0", "v 10 0 .1"))
    with pytest.raises(NavigationError, match="does_not_match_mesh"):
        plan_edge_entry(mesh, tracker, scene_file, (.58, 5.), (1.48, 5.))


def test_placement_matches_independent_zero_rotation_anchor():
    from bot.nav3d.extract import CENTER
    m=placement_matrix(dict(position=[CENTER+20,70,CENTER+30],rotation=[0,0,0],scale=1024,flags=0))
    assert np.allclose(m@[1,0,0,1],[20,-31,70,1])
    assert np.allclose(quaternion_matrix([0,0,0,1],[1,2,3],2)@[1,1,1,1],[3,4,5,1])


def render(values,seq=3):
    image=np.zeros((ROWS*16,142,3),np.uint8)
    for i,v in enumerate(values):spec.render_frame(image,0,i*16,spec.encode_frame(i%16,v+seq*4096))
    return image


def test_protocol_rejects_torn_rows_and_preserves_zero_unknown():
    values=[0]*26;values[0]=0xD01;values[1]=7;values[17]=2
    values[8]=(-12345)%4096;values[9]=((-12345)%(1<<24))//4096
    image=render(values);s=decode_nav3d(image)
    assert s.position_xy==(-123.45,0) and s.public_z==0 and s.goal_xy is None
    values[1]=0;assert decode_nav3d(render(values)).position_xy is None
    image[16:32]=render(values,seq=4)[16:32]
    assert decode_nav3d(image) is None


@pytest.fixture
def live_mesh(tmp_path):
    g,start,goal=scene('wall');frame=Frame('12.1.5.70961',2927,'quest:90896:2','east_north_up')
    m=NavMesh.build(*g.arrays(),frame=frame);m.save(tmp_path/'mesh');return m,start,goal


def samples(start,goal):
    s=Nav3DSample(1,2927,70961,2451,start[:2],0,0,90896,2,2927,goal[:2],True,True,False,4096)
    n=SimpleNamespace(sequence=1,quest_id=90896,phase=2,map_id=2451,destination=(.2,.3))
    return s,n


def test_shadow_is_no_input_and_native_zero_is_not_height(live_mesh,tmp_path):
    m,start,goal=live_mesh;s,n=samples(start,goal);events=[]
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d-shadow',mesh=str(tmp_path/'mesh')),now=0,log_event=lambda *e:events.append(e))
    cmd=rt.step(0,s,n)
    assert cmd.outcome=='nav3d_shadow_complete' and not cmd.tap and not cmd.keys
    assert events[-1][1]['z_source']=='inferred_mesh_support'
    assert rt.route.length>math.dist(start,goal)


def test_live_requires_independent_alignment_receipt(live_mesh,tmp_path):
    m,start,goal=live_mesh;s,n=samples(start,goal)
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d',mesh=str(tmp_path/'mesh')),now=0)
    assert rt.step(0,s,n).outcome=='nav3d_world_alignment_unverified'


@pytest.mark.parametrize('change,reason',[
    ({'world_id':0},'nav3d_world_frame_changed'),
    ({'client_build':70783},'nav3d_world_frame_changed'),
    ({'sequence':2},'nav3d_incoherent_channels'),
    ({'goal_world_id':0},'nav3d_goal_world_mismatch'),
    ({'grounded':False},'nav3d_state_unsafe'),
    ({'position_xy':None},'nav3d_position_unavailable'),
])
def test_live_invalid_inputs_release_and_stop(live_mesh,tmp_path,change,reason):
    m,start,goal=live_mesh;s,n=samples(start,goal)
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d',mesh=str(tmp_path/'mesh')),now=0)
    c=rt.step(0,replace(s,**change),n)
    assert c.outcome==reason and not c.tap and not c.keys


def test_stacked_surface_cannot_initialize_from_xy(tmp_path):
    g,start,goal=scene('stacked');m=NavMesh.build(*g.arrays(),frame=Frame('test',0,'test','east_north_up'))
    tracker=SurfaceTracker(m)
    with pytest.raises(NavigationError,match='ambiguous'):tracker.update(goal[:2])


def test_surface_tracker_rejects_wall_crossing(live_mesh):
    m,_,_=live_mesh;tracker=SurfaceTracker(m)
    tracker.update((10.5,5))
    with pytest.raises(NavigationError):tracker.update((12.5,5))


def test_tiled_coordinator_repairs_existing_corridor_without_replanning(tmp_path, monkeypatch):
    ground = Geometry()
    ground.floor(0, 0, 60, 20)
    frame = Frame('12.1.5.70961', 2927, 'quest:90896:2', 'east_north_up')
    mesh = NavMesh.build(*ground.arrays(), frame=frame, tile_cells=128)
    folder = tmp_path / 'mesh'
    mesh.save(folder)
    events = []
    coordinator = Nav3DCoordinator(Nav3DConfig(mode='nav3d', mesh=str(folder),
        supervised_trial_mesh_id=mesh.mesh_id, max_replans=1), now=0, log_event=lambda *e: events.append(e))
    start, goal = (24.8, 8., 0.), (40., 8., 0.)
    sample, navigation = samples(start, goal)
    sample = replace(sample, heading=1.5 * math.pi)
    assert coordinator.step(0, sample, navigation).keys == frozenset({'w'})
    follower = coordinator.follower
    corridor = coordinator.corridor
    assert corridor.count > 1
    monkeypatch.setattr(coordinator.mesh, 'plan_corridor', lambda _: pytest.fail('unexpected full replan'))

    for seq, xy in enumerate(((25.2, 9.2), (26.6, 10.5)), 2):
        command = coordinator.step(.2 * seq, replace(sample, sequence=seq, position_xy=xy),
                                   SimpleNamespace(**{**vars(navigation), 'sequence': seq}))

    assert not command.keys and not command.tap and command.outcome is None
    assert coordinator.follower is follower and coordinator.corridor is corridor
    assert coordinator.route.requested_goal[:2] == goal[:2]
    assert coordinator.route.points[0][:2] == pytest.approx((26.6, 10.5), abs=.001)
    assert coordinator.replans == 1
    assert sum(kind == 'nav3d_corridor_repaired' for kind, _ in events) == 1


@pytest.mark.parametrize("option", ["matching", "outside_trial", "geometry_mismatch", "bounded_height", "excess_height"])
def test_fast_tiled_plan_uses_wider_interior_only_when_verified(tmp_path, monkeypatch, option):
    source, _ = write_scene(tmp_path / "scene", "wall")
    frame = Frame("12.1.5.70961", 2927, "quest:90896:2", "east_north_up")
    source.write_text(json.dumps({**json.loads(source.read_text()), "frame": asdict(frame)}))
    folder = tmp_path / "mesh"
    mesh, _, _ = build_scene(source, folder, tile_cells=64, with_clearance=True)
    alternate = folder / "clearance" / "manifest.json"
    if option == "geometry_mismatch":
        alternate.write_text(json.dumps({**json.loads(alternate.read_text()), "geometry_sha256": "different"}))

    events = []
    runtime = Nav3DCoordinator(Nav3DConfig(mode="nav3d", mesh=str(folder), supervised_trial_mesh_id=mesh.mesh_id),
        now=0, clearance_route_allowed=lambda _: option != "outside_trial", log_event=lambda *event: events.append(event))
    if option in {"bounded_height", "excess_height"}:
        wider = runtime.wider_support().mesh
        plan = wider.plan
        height_shift = .35 if option == "bounded_height" else .41

        def shifted_detail(query):
            route = plan(query)
            points = list(route.points)
            midpoint = len(points) // 2
            x, y, z = points[midpoint]
            points[midpoint] = x, y, z + height_shift
            return replace(route, points=tuple(points))

        monkeypatch.setattr(wider, "plan", shifted_detail)
    start, goal = (3., 4., 0.), (21., 4., 0.)
    sample, navigation = samples(start, goal)
    fast = FastPoseSample(1, 1, sample.world_id, sample.ui_map_id, start[:2], 0., True, True, False)
    runtime.step(0, sample, navigation, fast_pose=fast)

    baseline = runtime.corridor.route
    assert runtime.route.points[0] == baseline.points[0]
    assert runtime.route.points[-1] == baseline.points[-1]
    assert runtime.route.mesh_id == mesh.mesh_id
    selected = option in {"matching", "bounded_height"}
    if selected:
        assert max(p[1] for p in runtime.route.points) > max(p[1] for p in baseline.points) + .1
        assert all(p[2] == pytest.approx(baseline.points[0][2], abs=.001) for p in runtime.route.points)
    else:
        assert runtime.route == baseline
    projections = [data["max_projection_delta_yards"] for event, data in events
                   if event == "nav3d_clearance_route_selected"]
    assert bool(projections) == selected
    if option == "bounded_height":
        assert projections == pytest.approx([.35], abs=.001)


@pytest.mark.parametrize('elapsed,sequence,outcome,recovered', [
    (1.03, 6, None, True),
    (.2, 6, 'nav3d_displacement_discontinuity', False),
    (1.03, 2, 'nav3d_displacement_discontinuity', False),
])
def test_travel_can_replan_after_a_short_gap_on_the_same_corridor(
        live_mesh, tmp_path, elapsed, sequence, outcome, recovered):
    mesh, start, goal = live_mesh
    sample, nav = samples(start, goal)
    events = []
    config = Nav3DConfig(mode='nav3d', mesh=str(tmp_path/'mesh'), max_replans=2,
                         supervised_trial_mesh_id=mesh.mesh_id)
    runtime = Nav3DCoordinator(config, now=0, log_event=lambda *event: events.append(event))
    runtime.step(0, sample, nav, travel_only=True)
    position = next(point[:2] for point in runtime.route.points if math.dist(point[:2], start[:2]) > 2.2)
    runtime.follower.commanded_motion = True
    nav.sequence = sequence
    command = runtime.step(elapsed, replace(sample, sequence=sequence, position_xy=position), nav, travel_only=True)

    assert command.outcome == outcome and not command.keys and not command.tap
    assert runtime.replans == int(recovered)
    assert any(event == 'nav3d_pose_gap_recovered' for event, _ in events) == recovered
    if recovered:
        assert runtime.follower is None and runtime.tracker.last[:2] == position


def test_lua_nav3d_packet_round_trip_and_secret_values():
    lupa=pytest.importorskip('lupa.lua51');lua=lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute('''
      WowAgent={};WowAgentChannelFrame={};UIParent={};packets={};fastPackets={};callbacks={}
      function CreateFrame(_,name)
        return {name=name,SetSize=function()end,SetPoint=function()end,SetFrameStrata=function()end,
          Hide=function()end,Show=function()end,SetScript=function(_,_,fn) callbacks[name]=fn end}
      end
      function WowAgent.NewPixelRow(f,offset)
        local rows=f.name=='WowAgentFastPoseFrame' and fastPackets or packets
        return function(tag,value) rows[offset/16+1]={tag,value} end
      end
      function UnitPosition()return -888.1,-2100.6,0,2927 end
      function GetPlayerFacing()return 1.2 end
      function GetBuildInfo()return '12.1.5','70961' end
      function UnitIsDeadOrGhost()return false end
      function UnitAffectingCombat()return false end
      function IsMounted()return false end
      function IsFlying()return false end
      function IsFalling()return false end
      function IsSwimming()return false end
      function CreateVector2D(x,y)return {x=x,y=y} end
      C_Map={GetWorldPosFromMapPos=function()return 2927,{GetXY=function()return -895,-2090 end} end,
             GetBestMapForUnit=function()return 2451 end}
      bit={band=function(a,b) local r,p=0,1 while a>0 and b>0 do if a%2==1 and b%2==1 then r=r+p end a=math.floor(a/2);b=math.floor(b/2);p=p*2 end return r end}
    ''')
    lua.execute(Path('addon/WowAgent/Nav3D.lua').read_text())
    lua.execute('WowAgent.WriteNav3D(7,90896,2,2451,2451,.2,.3,4096)')
    image=np.zeros((416,142,3),np.uint8)
    for i in range(26):
        row=lua.globals().packets[i+1];spec.render_frame(image,0,i*16,spec.encode_frame(row[1],row[2]))
    s=decode_nav3d(image)
    assert s.position_xy==(2100.6,-888.1) and s.goal_xy==(2090,-895) and s.sequence==7
    assert s.public_z==0 and s.grounded and s.bindings_verified
    lua.execute('WowAgent.WriteNav3D(7,90896,2,2451,2451,.2,.3,4096,3968)')
    for i in range(26):
        row=lua.globals().packets[i+1];spec.render_frame(image,0,i*16,spec.encode_frame(row[1],row[2]))
    updated=decode_nav3d(image)
    assert updated.encounter_profile and updated.encounter_selector_verified
    assert updated.encounter_target_matches and updated.encounter_target_alive and updated.encounter_target_attackable
    assert updated.position_xy == s.position_xy and updated.goal_xy == s.goal_xy
    lua.execute('function issecretvalue(v)return v==0 end; WowAgent.WriteNav3D(8,90896,2,2451,2451,nil,nil,4096)')
    assert lua.globals().packets[2][2]%4096 & 2 == 0
    lua.execute('WowAgent.WriteNav3D(7,90896,2,2451,2451,.2,.3,4096,0,function()return true end)')
    lua.execute('function UnitPosition()return -888.1,-2100.0,0,2927 end; callbacks.WowAgentFastPoseFrame(nil,.068)')
    fast_image=np.zeros((176,142,3),np.uint8)
    for i in range(11):
        row=lua.globals().fastPackets[i+1];spec.render_frame(fast_image,0,i*16,spec.encode_frame(row[1],row[2]))
    fast=decode_fast_pose(fast_image)
    assert fast.sequence == 2 and fast.base_sequence==7 and fast.position_xy==(2100,-888.1)
    assert fast.grounded and fast.bindings_verified and not fast.in_combat
    assert with_fast_pose(s,fast).goal_xy == s.goal_xy
    assert with_fast_pose(s,replace(fast,base_sequence=8)) is None


def test_fast_pose_does_not_refresh_an_expired_navigation_frame(live_mesh, tmp_path):
    m,start,goal=live_mesh;s,n=samples(start,goal)
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d-shadow',mesh=str(tmp_path/'mesh')),now=0)
    f=FastPoseSample(3,s.sequence,s.world_id,s.ui_map_id,s.position_xy,s.heading,True,True,False)
    assert rt.step(0,s,n,fast_pose=f).outcome=='nav3d_shadow_complete'
    assert rt.step(.51,s,n,fast_pose=replace(f,sequence=9)).outcome=='nav3d_stale_base_pose'


def test_quest_shadow_gate_runs_before_old_npc_recovery_or_visual_inputs(tmp_path,monkeypatch):
    from test_npc_obstacle import npc
    from bot.modules import quest
    from bot.control.quest_travel import TravelCommand
    from bot.perception.quest_channel import QuestState
    run,ctx,tick,nav,events=npc(tmp_path,monkeypatch)
    ctx.cfg.nav3d.mode='nav3d-shadow';calls=[]
    run.nav3d_coordinator=SimpleNamespace(step=lambda *a,**kw:(calls.append(a),TravelCommand(outcome='nav3d_shadow_complete'))[1])
    monkeypatch.setattr(quest,'decode_travel',lambda _:nav)
    monkeypatch.setattr(run,'recover',lambda *_:pytest.fail('legacy recovery must not run'))
    monkeypatch.setattr(run,'interact_with_npc',lambda *_:pytest.fail('legacy approach must not run'))
    tick(QuestState.READY)
    assert calls and run.OUTCOME=='stopped:nav3d_shadow_complete'
    assert not ctx.input.count('key_down') and not ctx.input._held_keys and run.terrain is None


def test_deadline_and_duplicate_sequence_cannot_renew_navigation_budget(live_mesh,tmp_path):
    m,start,goal=live_mesh;s,n=samples(start,goal)
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d-shadow',mesh=str(tmp_path/'mesh'),budget_s=1),now=0)
    assert rt.step(0,s,n).outcome=='nav3d_shadow_complete'
    assert rt.step(.3,s,n).outcome is None
    assert rt.step(.6,s,n).outcome=='nav3d_stale_pose'
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d-shadow',mesh=str(tmp_path/'mesh'),budget_s=1),now=0)
    assert rt.step(1.1,s,n).outcome=='nav3d_budget_exhausted'


def test_casc_cache_corruption_is_never_returned_as_geometry(tmp_path):
    from bot.nav3d.casc import LocalCASC
    import hashlib
    reader=object.__new__(LocalCASC);reader.cache=tmp_path;reader.records={}
    path=tmp_path/hashlib.sha256(b'1349477').hexdigest()
    path.write_bytes(b'valid');path.with_suffix('.sha256').write_text(hashlib.sha256(b'valid').hexdigest())
    assert reader.read(1349477)==b'valid'
    path.write_bytes(b'corrupt')
    with pytest.raises(NavigationError,match='checksum_mismatch'):reader.read(1349477)


def test_alignment_booleans_without_verified_receipts_cannot_enable_input(live_mesh,tmp_path):
    m,start,goal=live_mesh;s,n=samples(start,goal)
    evidence=dict(mesh_id=m.mesh_id,frame=asdict(m.frame),evidence_kind='live_observation',
        alignment_verified=True,phase_verified=True,receipts=['not evidence'])
    path=tmp_path/'alignment.json';path.write_text(json.dumps(evidence))
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d',mesh=str(tmp_path/'mesh'),alignment_file=str(path)),now=0)
    assert rt.step(0,s,n).outcome=='nav3d_world_alignment_unverified'


def candidate_runtime(live_mesh,tmp_path,monkeypatch,mode='nav3d-shadow'):
    m,start,goal=live_mesh;s,n=samples(start,goal);events=[]
    rt=Nav3DCoordinator(Nav3DConfig(mode=mode,mesh=str(tmp_path/'mesh')),now=0,
        log_event=lambda *e:events.append(e))
    actual=rt.tracker.candidates(goal[:2])[0]
    low=(*actual[:2],actual[2]-5);high=(*actual[:2],actual[2]+5)
    candidates=rt.tracker.candidates
    monkeypatch.setattr(rt.tracker,'candidates',lambda xy,**kw:[high,actual,low] if tuple(xy)==tuple(goal[:2]) else candidates(xy,**kw))
    return rt,s,n,events,low,actual,high


def test_target_floors_tried_bottom_up_skipping_disconnected(live_mesh,tmp_path,monkeypatch):
    rt,s,n,events,low,actual,high=candidate_runtime(live_mesh,tmp_path,monkeypatch)
    cmd=rt.step(0,s,n)
    assert cmd.outcome=='nav3d_shadow_complete' and not cmd.tap
    assert rt.goals==[low,actual,high] and rt.goal_index==1
    assert rt.route.requested_goal==actual
    failures=[data for event,data in events if event=='nav3d_candidate_finished']
    assert len(failures)==1 and failures[0]['goal']==low and not failures[0]['reached_in_game']


def test_floor_arrival_without_target_receipt_advances_without_resetting_budget(live_mesh,tmp_path,monkeypatch):
    from bot.control.quest_travel import TravelCommand
    rt,s,n,events,low,actual,high=candidate_runtime(live_mesh,tmp_path,monkeypatch)
    rt.step(0,s,n);rt.config.mode='nav3d';deadline=rt.deadline
    rt.follower=SimpleNamespace(index=0,step=lambda *a,**kw:TravelCommand(outcome='nav3d_route_arrived'))
    def step(now,seq):return rt.step(now,replace(s,sequence=seq),SimpleNamespace(**{**vars(n),'sequence':seq}))
    assert step(.1,2).outcome is None and rt.arrived_at==.1
    assert step(1,3).outcome is None and rt.goal_index==1
    assert step(2.2,4).outcome=='nav3d_candidates_exhausted'
    assert rt.deadline==deadline and rt.goal_index==3
    assert any(d['reason']=='target_receipt_missing' for e,d in events if e=='nav3d_candidate_finished')


def test_stuck_candidate_moves_up_and_preserves_retry_budget(live_mesh,tmp_path,monkeypatch):
    from bot.control.quest_travel import TravelCommand
    rt,s,n,events,*_=candidate_runtime(live_mesh,tmp_path,monkeypatch)
    rt.step(0,s,n);rt.config.mode='nav3d'
    rt.follower=SimpleNamespace(index=0,step=lambda *a,**kw:TravelCommand(outcome='nav3d_stuck_replan_required'))
    cmd=rt.step(.1,replace(s,sequence=2),SimpleNamespace(**{**vars(n),'sequence':2}))
    assert cmd.outcome is None and not cmd.tap and rt.goal_index==2 and rt.replans==1


@pytest.mark.parametrize('flags,name',[(0x4000000,'anything'),(0x80,'anything'),(0,'antiportal')])
def test_visibility_groups_are_excluded_but_physical_wall_remains(flags,name):
    from bot.nav3d.extract import SceneBuilder
    builder=SceneBuilder(None,None,[[0,0],[10,10]])
    root=dict(groupIDs=[2,3],groupCount=2,groupNames={'0':name})
    wall=dict(flags=0,nameOfs=10,indices=[0,1,2],materialInfo=[{'flags':8}],vertices=[1,0,-1,2,0,-1,1,2,-1])
    groups={1:root,2:dict(wall,flags=flags,nameOfs=0),3:wall}
    builder.read=lambda kind,fid:groups[fid]
    builder.wmo(1,np.eye(4),dict(uniqueId=4,doodadSet=0))
    assert len(builder.triangles)==1 and builder.instances[0]['file_id']==3
    assert builder.excluded_wmo_groups[0]['file_id']==2


def test_float32_shared_surface_boundary_is_not_missing():
    tracker=object.__new__(SurfaceTracker)
    tracker.triangles=np.array([[[2097.2000007629395,-879.7999992370605,61.646751403808594],
        [2098.0000019073486,-879.2000007629395,62.54675102233887],
        [2097.6159992218018,-878.6000022888184,62.54675102233887]]])
    assert len(tracker.candidates((2097.377779006958,-879.6666679382324)))==1
    assert tracker.candidates((2097.2,-879.9))==[]


def test_explicit_mesh_bound_trial_produces_intent_without_claiming_alignment(live_mesh,tmp_path):
    m,start,goal=live_mesh;s,n=samples(start,goal);events=[]
    cfg=Nav3DConfig(mode='nav3d',mesh=str(tmp_path/'mesh'),supervised_trial_mesh_id=m.mesh_id)
    rt=Nav3DCoordinator(cfg,now=0,log_event=lambda *e:events.append(e))
    command=rt.step(0,s,n)
    assert command.outcome is None and (command.tap or command.keys==frozenset({'w'}))
    assert not rt.alignment and not rt.follower.alignment_verified and rt.follower.supervised_trial
    assert events[0][0]=='nav3d_supervised_trial' and events[0][1]['phase_verified'] is False
    # World/build/phase checks remain mandatory even during a supervised trial.
    rt=Nav3DCoordinator(cfg,now=0)
    assert rt.step(0,replace(s,world_id=0),n).outcome=='nav3d_world_frame_changed'


def test_supervised_trial_cannot_transfer_to_another_mesh(live_mesh,tmp_path):
    cfg=Nav3DConfig(mode='nav3d',mesh=str(tmp_path/'mesh'),supervised_trial_mesh_id='0'*64)
    with pytest.raises(NavigationError,match='nav3d_trial_mesh_mismatch'):
        Nav3DCoordinator(cfg,now=0)


def test_manual_entry_executes_nav3d_intent_through_existing_executor(tmp_path,monkeypatch):
    from test_npc_obstacle import npc
    from bot.modules import quest
    from bot.control.quest_travel import TravelCommand
    from bot.perception.quest_channel import QuestState
    run,ctx,tick,nav,events=npc(tmp_path,monkeypatch)
    ctx.cfg.nav3d.mode='nav3d'
    run.nav3d_coordinator=SimpleNamespace(step=lambda *a,**kw:TravelCommand(tap='left',hold_s=.03,precise=True))
    monkeypatch.setattr(quest,'decode_travel',lambda _:nav)
    monkeypatch.setattr(run,'recover',lambda *_:pytest.fail('legacy recovery must not run'))
    tick(QuestState.READY)
    assert run.execution_started and ctx.input.count('key_down')==1
    assert not ctx.input._held_keys and run.OUTCOME is None


@pytest.mark.parametrize('heading',[0,math.pi/2,math.pi,3*math.pi/2])
def test_actual_coordinator_commands_obey_precise_executor_contract(live_mesh,tmp_path,monkeypatch,heading):
    from bot.control.input_mock import MockInput
    from bot.control import input_base
    m,start,goal=live_mesh;s,n=samples(start,goal)
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d',mesh=str(tmp_path/'mesh'),
        supervised_trial_mesh_id=m.mesh_id),now=0)
    command=rt.step(0,replace(s,heading=heading),n)
    assert command.outcome is None
    if command.tap:  # misaligned: bounded precise turn pulse, W released over ~30 degrees
        assert command.precise and command.hold_s<=.12
        sleeps=[]
        monkeypatch.setattr(input_base.time,'sleep',sleeps.append)
        # Mock only the OS injection; retain InputBase.tap_precise's real validation
        # and release logic, exactly as inherited by HelperInput.
        executor=MockInput()
        executor.tap_precise(command.tap,command.hold_s)
        assert sleeps==[command.hold_s]
        assert executor.count('key_down',command.tap)==executor.count('key_up',command.tap)==1
        assert not executor._held_keys
    else:  # aligned cruise holds W instead of stutter-tapping
        assert command.keys==frozenset({'w'})


def edge_tracker():
    # A sloped doorway boundary and an overlapping roof; translated geometry
    # reproduces the 0.02-yard miss in run 20261118T082051-339c21e3.
    triangles=np.array([[[0,0,0],[.8,.6,.9],[.416,1.2,.9]],
        [[-2,-2,17],[3,-2,17],[-2,3,17]],[[3,-2,17],[3,3,17],[-2,3,17]]])
    mesh=SimpleNamespace(debug_triangles=lambda:triangles,frame=Frame('test',0,'test','east_north_up'),
        agent=Agent(),plan=lambda q:SimpleNamespace(length=math.dist(q.start,q.goal)))
    return SurfaceTracker(mesh)


def test_doorway_edge_tolerance_retains_connected_floor_not_roof():
    tracker=edge_tracker();tracker.last=(.5,.4,.575)
    assert tracker.candidates((.7,.5))==[(.7,.5,17.)]
    actual=tracker.update((.7,.5))
    assert actual[:2]==(.7,.5) and actual[2]==pytest.approx(.774)
    assert tracker.update((.7,.5))==actual  # held position remains stable


def test_short_stair_movement_respects_agent_climb_and_connected_path():
    triangles = np.array([
        [[-1,-1,0],[0,-1,0],[0,1,0]],
        [[0,-1,.6],[1,-1,.6],[0,1,.6]],
    ])
    mesh = SimpleNamespace(debug_triangles=lambda: triangles, frame=Frame('test',0,'test','east_north_up'),
                           agent=Agent(max_climb=.8),
                           plan=lambda q: SimpleNamespace(length=math.dist(q.start,q.goal)))
    tracker = SurfaceTracker(mesh)
    assert tracker.update((-.15, 0)) == (-.15, 0., 0.)
    assert tracker.update((.15, 0)) == (.15, 0., .6)
    mesh.plan = lambda q: SimpleNamespace(length=12.)
    with pytest.raises(NavigationError, match='missing'):
        tracker.update((-.15, 0))


def test_short_descent_requires_exact_support_and_a_direct_connected_path():
    triangles = np.array([
        [[-1, -1, .97], [0, -1, .97], [0, 1, .97]],
        [[0, -1, 0], [1, -1, 0], [0, 1, 0]],
    ])
    mesh = SimpleNamespace(debug_triangles=lambda: triangles,
                           frame=Frame("test", 0, "test", "east_north_up"),
                           agent=Agent(max_climb=.8),
                           plan=lambda q: SimpleNamespace(length=1.27, points=(q.start, q.goal)))
    tracker = SurfaceTracker(mesh)
    tracker.update((-.3, 0))
    assert tracker.update((.3, 0)) == (.3, 0., 0.)
    with pytest.raises(NavigationError, match="missing"):
        tracker.update((-.3, 0))
    tracker.last = (-.3, 0, .97)
    mesh.plan = lambda q: SimpleNamespace(length=2.8, points=(q.start, (0, 1, .97), q.goal))
    with pytest.raises(NavigationError, match="missing"):
        tracker.update((.3, 0))


def test_edge_tolerance_does_not_select_a_floor_without_continuity():
    with pytest.raises(NavigationError,match='ambiguous'):edge_tracker().update((.7,.5))
    tracker=edge_tracker();tracker.last=(.5,.4,.575)
    with pytest.raises(NavigationError,match='missing'):tracker.update((.7,.3))
    # A nearby point across a wall still needs a short complete connection.
    tracker=edge_tracker();tracker.last=(.5,.4,.575)
    tracker.mesh.plan=lambda q:SimpleNamespace(length=5)
    with pytest.raises(NavigationError,match='missing'):tracker.update((.7,.5))


@pytest.mark.parametrize("height", [.28, .4])
def test_only_close_connected_edge_projections_can_continue_a_floor(height):
    triangles = np.array([
        [[-1, -1, 0], [-.05, -1, 0], [-.05, 1, 0]],
        [[.05, -1, height], [1, -1, height], [.05, 1, height]],
    ])
    mesh = SimpleNamespace(debug_triangles=lambda: triangles, frame=Frame("test", 0, "test", "east_north_up"),
                           agent=Agent(max_climb=.8),
                           plan=lambda q: SimpleNamespace(length=math.dist(q.start[:2], q.goal[:2])))
    tracker = SurfaceTracker(mesh)
    with pytest.raises(NavigationError, match="ambiguous"):
        tracker.update((0, 0))
    tracker.update((-.1, 0))
    if height == .28:
        assert tracker.update((0, 0)) == (0., 0., 0.)
    else:
        with pytest.raises(NavigationError, match="ambiguous"):
            tracker.update((0, 0))


@pytest.mark.parametrize('invalid',[None,'stale','moved','mesh','goal','stop'])
def test_resume_edge_stop_requires_matching_recent_run(live_mesh,tmp_path,monkeypatch,invalid):
    from bot.nav3d import runtime
    m,start,goal=live_mesh;s,n=samples(start,goal)
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d-shadow',mesh=str(tmp_path/'mesh')),now=0,resume_runs=tmp_path/'runs')
    position=rt.tracker.candidates(start[:2])[0]
    mesh_id='bad' if invalid=='mesh' else m.mesh_id
    prior_goal=[0,0,0] if invalid=='goal' else goal
    stop='nav3d_state_unsafe' if invalid=='stop' else 'nav3d_support_missing'
    rows=[dict(kind='nav3d_plan',data=dict(route=dict(mesh_id=mesh_id,frame=asdict(m.frame),requested_goal=prior_goal))),
        dict(kind='nav3d_control',data=dict(position=position)),
        dict(kind='nav3d_pose',data=dict(position_xy=start[:2])),
        dict(kind='nav3d_stop',data=dict(reason=stop)),
        dict(kind='module_end',ts=0 if invalid=='stale' else 999,data=dict(status='stopped:'+stop))]
    folder=tmp_path/'runs'/'previous';folder.mkdir(parents=True)
    (folder/'summary.json').write_text('{}');(folder/'events.jsonl').write_text('\n'.join(map(json.dumps,rows)))
    monkeypatch.setattr(runtime.time,'time',lambda:1000)
    if invalid=='moved':s=replace(s,position_xy=(start[0]+.2,start[1]))
    rt.resume_support(s)
    assert (rt.tracker.last is not None)==(invalid is None)


def cruise_runtime(live_mesh,tmp_path,now=0):
    m,start,goal=live_mesh;s,n=samples(start,goal)
    rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d',mesh=str(tmp_path/'mesh'),
        supervised_trial_mesh_id=m.mesh_id),now=now)
    rt.step(now,s,n)
    return rt,s,n,list(start[:2])

def drive(rt,s,n,now,seq,xy,heading):
    s=replace(s,sequence=seq,position_xy=xy,heading=heading)
    nav=SimpleNamespace(**{**vars(n),'sequence':seq})
    return rt.step(now,s,nav),s,nav

def waypoint_heading(rt,xy):
    target=rt.route.points[rt.follower.index]
    return math.atan2(-(target[0]-xy[0]),target[1]-xy[1])%math.tau


def test_follower_cruises_on_held_w_without_stutter_taps(live_mesh,tmp_path):
    rt,s,n,xy=cruise_runtime(live_mesh,tmp_path)
    now,seq,commands=.2,2,[]
    for _ in range(6):
        heading=waypoint_heading(rt,xy)
        cmd,s,n=drive(rt,s,n,now,seq,tuple(xy),heading)
        commands.append(cmd)
        target=rt.route.points[rt.follower.index]
        dx,dy=target[0]-xy[0],target[1]-xy[1]
        length=math.hypot(dx,dy) or 1
        xy=[xy[0]+dx/length*.5,xy[1]+dy/length*.5]
        now+=.2;seq+=1
    holds=[c for c in commands if not c.tap]
    assert len(holds)>=5
    assert all(c.keys==frozenset({'w'}) and c.outcome is None for c in holds)
    # A duplicate receipt keeps the established hold instead of releasing keys.
    repeat=rt.step(now,s,n)
    assert repeat.keys==frozenset({'w'}) and not repeat.tap and repeat.outcome is None


def test_follower_steers_on_the_move_and_rotates_in_place_only_past_30_degrees(live_mesh,tmp_path):
    rt,s,n,xy=cruise_runtime(live_mesh,tmp_path)
    heading=waypoint_heading(rt,xy)
    off15=(heading-math.radians(15))%math.tau
    # Late enough to clear the initial turn pulse's interval gate.
    cmd,s,n=drive(rt,s,n,1.0,2,tuple(xy),off15)
    assert cmd.tap=='left' and cmd.precise and cmd.keys==frozenset({'w'}) and cmd.hold_s<=.12
    off60=(heading-math.radians(60))%math.tau
    cmd,s,n=drive(rt,s,n,1.6,3,tuple(xy),off60)
    assert cmd.tap=='left' and cmd.keys==frozenset()
    # Inside the pulse interval at large error: no motion while misaligned.
    cmd,s,n=drive(rt,s,n,1.7,4,tuple(xy),off60)
    assert not cmd.tap and cmd.keys==frozenset()


def test_held_forward_cruise_still_reports_stuck_without_progress(live_mesh,tmp_path):
    rt,s,n,xy=cruise_runtime(live_mesh,tmp_path)
    heading=waypoint_heading(rt,xy)
    for i,now in enumerate([.2,.6,1.0,1.4,1.8,2.2]):
        cmd,s,n=drive(rt,s,n,now,2+i,tuple(xy),heading)
    assert cmd.outcome is None and not cmd.keys and rt.replans==1
    cmd,s,n=drive(rt,s,n,2.4,8,tuple(xy),heading)
    assert cmd.outcome=='nav3d_candidates_exhausted' and not cmd.keys
