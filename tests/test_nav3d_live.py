"""Behavior/geometry checks. These fixtures are not live game success evidence."""
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import json
import math
import numpy as np
import pytest
from bot.core.config import Nav3DConfig
from bot.nav3d import Frame, NavMesh, NavigationError
from bot.nav3d.demo import scene
from bot.nav3d.extract import terrain, placement_matrix, quaternion_matrix
from bot.nav3d.runtime import SurfaceTracker, Nav3DCoordinator
from bot.perception.nav3d_channel import Nav3DSample, decode_nav3d, ROWS
from bot.perception import channel_spec as spec


def test_terrain_winding_holes_and_coordinates():
    ch=dict(position=[-900,-2100,50],vertices=[0]*145,flags=0,holesLowRes=1,holesHighRes=[0]*8)
    v,t=terrain(ch)
    assert len(t)==240  # one legacy hole removes 2x2 cells
    assert np.all(np.cross(v[t[:,1]]-v[t[:,0]],v[t[:,2]]-v[t[:,0]])[:,2]>0)
    assert tuple(v[0])==(2100,-900,50)
    ch.update(flags=0x10000,holesHighRes=[1]+[0]*7)
    assert len(terrain(ch)[1])==252


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


def test_lua_nav3d_packet_round_trip_and_secret_values():
    lupa=pytest.importorskip('lupa.lua51');lua=lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute('''
      WowAgent={};WowAgentChannelFrame={};UIParent={};packets={}
      function CreateFrame() return {SetSize=function()end,SetPoint=function()end,SetFrameStrata=function()end,Hide=function()end,Show=function()end} end
      function WowAgent.NewPixelRow(_,offset) return function(tag,value) packets[offset/16+1]={tag,value} end end
      function UnitPosition()return -888.1,-2100.6,0,2927 end
      function GetPlayerFacing()return 1.2 end
      function GetBuildInfo()return '12.1.5','70961' end
      function UnitIsDeadOrGhost()return false end
      function CreateVector2D(x,y)return {x=x,y=y} end
      C_Map={GetWorldPosFromMapPos=function()return 2927,{GetXY=function()return -895,-2090 end} end}
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
    lua.execute('function issecretvalue(v)return v==0 end; WowAgent.WriteNav3D(8,90896,2,2451,2451,nil,nil,4096)')
    assert lua.globals().packets[2][2]%4096 & 2 == 0


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
    monkeypatch.setattr(rt.tracker,'candidates',lambda xy:[high,actual,low] if tuple(xy)==tuple(goal[:2]) else candidates(xy))
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
    assert command.outcome is None and command.tap
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
