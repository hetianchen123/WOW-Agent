"""No-history building recovery: simulated native motion, never live success."""
from dataclasses import replace
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.building_relocation import BuildingBackoffProbe, local_backoff_evidence
from bot.control.ground_trace import GroundTrace
from bot.control.quest_travel import TravelCommand
from bot.perception.travel_channel import TravelFlags as F, LifeState, RecoverySample
from test_building_entry_recovery import facade_nav, retry_result, recorded
from test_building_relocation import simulate, walked
from test_segmented_runtime import controller


def local_scene(size):
    w,h=size
    return dict(schema=1,executable=False,surface='unknown',doorways=[],layout=dict(
        schema=1,coordinate_space='current_image',size=list(size),floor_regions=[],
        exterior_regions=[dict(box=[.62*w,.65*h,.70*w,.75*h],pixels=16)]))


def start_failure(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry';obj.ground_trace=GroundTrace()
    nav=facade_nav(replace(nav,height=0),view=4)
    obj.step(ctx,1,nav,0)
    nav=replace(nav,sequence=nav.sequence+1)
    obj.step(ctx,1.1,nav,0)
    requests[0].result=dict(retry_result(requests[0]),building_scene=local_scene(requests[0].frame.size))
    assert obj.step(ctx,1.2,nav,0)==TravelCommand()
    return obj,ctx,nav,requests


def test_small_local_patch_can_trigger_probe_without_ground_fraction_feet_or_door():
    scene=local_scene((800,600))
    scene['surface_evidence']=dict(visible_surface_fraction=.001)
    evidence=local_backoff_evidence(scene,(800,600))
    assert evidence and evidence['rear_path_verified'] is False
    assert evidence['projection_calibrated'] is False


@pytest.mark.parametrize('fault',['roof','water','unknown','distant','stale_size','bad_box','no_pixels'])
def test_unrelated_or_invalid_surface_evidence_does_not_enable_probe(fault):
    scene=local_scene((800,600));layout=scene['layout']
    if fault=='roof':scene['surface']='structure'
    if fault=='water':scene['surface']='water'
    if fault=='unknown':layout['exterior_regions']=[]
    if fault=='distant':layout['exterior_regions'][0]['box']=[300,0,500,100]
    if fault=='stale_size':layout['size']=[900,600]
    if fault=='bad_box':layout['exterior_regions'][0]['box'][1]=float('nan')
    if fault=='no_pixels':layout['exterior_regions'][0]['pixels']=0
    assert local_backoff_evidence(scene,(800,600)) is None


def test_no_history_recovery_moves_then_recaptures_and_remeasures_without_camera_loop(tmp_path,monkeypatch):
    obj,ctx,nav,requests=start_failure(tmp_path,monkeypatch)
    assert isinstance(obj.relocation,BuildingBackoffProbe) and obj.building_recovery is None
    assert requests[0].closed and obj.plan is obj.anchor is None
    assert obj.relocation_used and not obj.passages
    deadline,progress_at=obj.deadline,obj.progress_at
    start=nav.position;old_session=requests[0].job['session_id'];commands=[]
    for i in range(160):
        now=1.3+i*.1
        nav=replace(nav,sequence=nav.sequence+1)
        command=obj.step(ctx,now,nav,0);commands.append(command)
        assert not command.keys and command.outcome is None
        nav,_=simulate(command,nav,0)
        if len(requests)>1:break
    assert len(requests)==2 and requests[-1].frame.player_position==nav.position
    assert requests[-1].job['session_id']!=old_session
    assert 1.5<obj.router.distance(start,nav.position)<=2.1
    assert obj.total<=2.1 and 1<obj.pulses<=12
    assert all(c.precise and c.hold_s<=.12 for c in commands if c.tap=='s')
    assert not any(c.tap in {'left','right'} for c in commands)
    assert obj.deadline==deadline and progress_at<obj.progress_at<=now
    assert obj.goal_mode=='building_entry' and not obj.passages
    # A fresh frame resumes normal geometry, not another automatic retreat.
    requests[-1].result=dict(retry_result(requests[-1]),kind='calibration_orbit',
        yaw_pixels=110,calibration=dict(stage='orbit'),building_scene=local_scene(requests[-1].frame.size))
    obj.step(ctx,now+.1,replace(nav,sequence=nav.sequence+1),0)
    assert obj.calibration_action and obj.relocation is None and obj.plan is None


def test_duplicate_receipts_do_not_confirm_probe_or_allow_another_key(tmp_path,monkeypatch):
    obj,ctx,nav,_=start_failure(tmp_path,monkeypatch);probe=obj.relocation
    first=replace(nav,sequence=nav.sequence+1)
    assert probe.step(ctx,1.6,first,0)==TravelCommand()
    assert probe.step(ctx,1.7,first,0)==TravelCommand()
    second=replace(first,sequence=first.sequence+1)
    cmd=probe.step(ctx,1.8,second,0)
    assert cmd.tap=='s'
    moved,_=simulate(cmd,second,0)
    moved=replace(moved,sequence=second.sequence+1)
    assert probe.step(ctx,2.2,moved,0)==TravelCommand()
    assert probe.step(ctx,2.3,moved,0)==TravelCommand()
    assert probe.total==0 and probe.pulses==1
    assert probe.step(ctx,3,moved,0).outcome=='probe_telemetry_stale'


def test_no_displacement_consumes_probe_once_and_returns_to_observation(tmp_path,monkeypatch):
    obj,ctx,nav,_=start_failure(tmp_path,monkeypatch);commands=[]
    for i in range(12):
        nav=replace(nav,sequence=nav.sequence+1)
        commands.append(obj.step(ctx,1.3+i*.1,nav,0))
        if obj.building_recovery:break
    assert len([c for c in commands if c.tap=='s'])==1
    assert obj.relocation is None and obj.building_recovery and obj.relocation_used
    assert obj.total==0 and not obj.passages


@pytest.mark.parametrize('change',[
    dict(flags=F.KEYS_VERIFIED|F.FALLING),dict(flags=F.KEYS_VERIFIED|F.IN_COMBAT),
    dict(flags=F.KEYS_VERIFIED|F.SWIMMING),dict(flags=F.KEYS_VERIFIED|F.MOUNTED),
    dict(flags=F.KEYS_VERIFIED|F.CASTING),dict(flags=F.KEYS_VERIFIED|F.FLYING),
    dict(flags=F(0)),dict(map_id=999),dict(objective_target=999),dict(objective_index=2),
    dict(recovery=RecoverySample(LifeState.DEAD)),dict(camera_epoch=9),
    dict(camera=SimpleNamespace(paused=True)),
])
def test_interruptions_stop_the_next_probe_even_with_constant_height(tmp_path,monkeypatch,change):
    obj,ctx,nav,_=start_failure(tmp_path,monkeypatch);probe=obj.relocation
    nav=replace(nav,sequence=nav.sequence+1)
    probe.step(ctx,1.4,nav,0)
    nav=replace(nav,sequence=nav.sequence+1)
    assert probe.step(ctx,1.6,nav,0).tap=='s'
    changed=replace(nav,sequence=nav.sequence+1,**change)
    cmd=probe.step(ctx,1.7,changed,0)
    assert cmd.outcome=='probe_interrupted' and not cmd.tap and not cmd.keys
    assert probe.pulses==1


@pytest.mark.parametrize('delta,angle,height',[
    ((.0003,0),0,0),((0,-.0002),0,0),((0,.001),0,0),((0,0),.2,0),((0,0),0,1),
])
def test_unexpected_movement_or_height_stops_before_more_keys(tmp_path,monkeypatch,delta,angle,height):
    obj,ctx,nav,_=start_failure(tmp_path,monkeypatch);probe=obj.relocation
    probe.step(ctx,1.4,replace(nav,sequence=nav.sequence+1),0)
    assert probe.step(ctx,1.6,replace(nav,sequence=nav.sequence+2),0).tap=='s'
    changed=replace(nav,sequence=nav.sequence+3,height=height,
        position=tuple(a+b for a,b in zip(nav.position,delta)))
    cmd=probe.step(ctx,1.7,changed,angle)
    assert cmd.outcome in {'probe_pose_changed','probe_height_drop'} and not cmd.tap


def test_known_rear_collision_prevents_the_probe(tmp_path,monkeypatch):
    obj,ctx,nav,_=start_failure(tmp_path,monkeypatch)
    obj.blocked.append((nav.position[0],nav.position[1]+.0002))
    probe=obj.relocation
    probe.step(ctx,1.4,replace(nav,sequence=nav.sequence+1),0)
    cmd=probe.step(ctx,1.6,replace(nav,sequence=nav.sequence+2),0)
    assert cmd.outcome=='building_relocation_blocked' and probe.pulses==0


def test_recent_trace_keeps_priority_over_exploratory_backoff(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry';trace,nav=walked(nav);obj.ground_trace=trace
    obj.recover_building_view(ctx,1.1,nav,'auto_ground_texture_insufficient',0)
    assert obj.relocation.basis=='recent_ground_trace'


def test_new_rear_contact_overrides_historically_walked_trace(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry';trace,nav=walked(nav);obj.ground_trace=trace
    obj.motion_history=[dict(key='s',moved=0),dict(key='w',moved=0)]
    obj.recover_building_view(ctx,1.1,nav,'auto_calibration_repeated_collision',0)
    assert obj.backoff_blocked and obj.relocation is None and obj.building_recovery


@pytest.mark.parametrize('fault',['expired','camera','pose','rear_already_failed','no_frame','budget'])
def test_failed_measurement_cannot_reuse_stale_evidence_for_backoff(tmp_path,monkeypatch,fault):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry';nav=facade_nav(nav,view=4)
    obj.step(ctx,1,nav,0);obj.step(ctx,1.1,replace(nav,sequence=nav.sequence+1),0)
    obj.building_scene=local_scene(requests[0].frame.size)
    obj.building_observation=(nav,0,1,requests[0].frame.frame_id,requests[0].frame.size)
    now=1.2
    if fault=='expired':now=17
    if fault=='camera':nav=replace(nav,camera_epoch=2)
    if fault=='pose':nav=replace(nav,position=(nav.position[0]+.001,nav.position[1]))
    if fault=='rear_already_failed':obj.motion_history=[dict(key='s',moved=0)]
    if fault=='no_frame':obj.building_observation=None
    if fault=='budget':obj.calls=obj.call_limit-1
    obj.recover_building_view(ctx,now,nav,'auto_ground_texture_insufficient',0)
    assert obj.relocation is None and obj.building_recovery


def test_two_forward_calibration_contacts_enter_building_recovery_instead_of_exit(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry';nav=facade_nav(nav,view=4)
    obj.step(ctx,1,nav,0);obj.step(ctx,1.1,replace(nav,sequence=nav.sequence+1),0)
    obj.building_scene=local_scene(requests[0].frame.size)
    obj.building_observation=(nav,0,1,requests[0].frame.frame_id,requests[0].frame.size)
    obj.motion_history=[dict(key='w',moved=0)]
    obj.calibration_pulse=(1.1,nav.position,nav.sequence,'w',nav.height,0)
    cmd=obj.step(ctx,1.8,replace(nav,sequence=nav.sequence+2),0)
    assert cmd==TravelCommand() and isinstance(obj.relocation,BuildingBackoffProbe)
    assert requests[0].closed and obj.building_recovery is None


def test_saved_failed_door_image_supplies_local_evidence_not_pixel_world_coordinates():
    from bot.perception.building_scene import describe_building_scene
    meta,data,observed,frame=recorded()
    scene=describe_building_scene(observed,meta['names'],camera_view=2)
    evidence=local_backoff_evidence(scene,frame.size)
    assert evidence and scene['executable'] is False
    assert 'world_coordinates' not in evidence and not evidence['rear_path_verified']


def test_stop_cancels_building_probe_and_releases_task_input(tmp_path,monkeypatch):
    from test_npc_obstacle import npc
    run,_,_,_,_=npc(tmp_path,monkeypatch)
    obj,ctx,nav,_=start_failure(tmp_path,monkeypatch)
    run.building_access=SimpleNamespace(close=obj.close)
    probe=obj.relocation
    probe.step(ctx,1.4,replace(nav,sequence=nav.sequence+1),0)
    command=probe.step(ctx,1.6,replace(nav,sequence=nav.sequence+2),0)
    run.apply_travel_command(ctx,command)
    assert ctx.input.count('key_down','s')==ctx.input.count('key_up','s')==1
    assert not ctx.input._held_keys and run.ground_trace.movement_sent
    run.stop(ctx,'game_not_frontmost')
    assert obj.closed and obj.relocation is None and not ctx.input._held_keys


def overview_setup(tmp_path,monkeypatch):
    records=json.loads((Path(__file__).parent/'fixtures/building-backoff-20261118.json').read_text())['cases']
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry';nav=facade_nav(nav,view=4)
    overview=[]
    class Request:
        def __init__(self,rgb,config,view,outdoors,**kwargs):
            assert kwargs['full_window'] is True
            self.frame_id='fresh-wide';self.closed=False
            self.result=dict(records['current-overview']['scene'],frame_id=self.frame_id)
            overview.append(self)
        def poll(self):return self.result
        def close(self):self.closed=True
    obj.overview_capture=lambda _:np.zeros((977,1688,3),np.uint8)
    obj.overview_request_factory=Request
    obj.step(ctx,1,nav,0);obj.step(ctx,1.1,replace(nav,sequence=nav.sequence+1),0)
    requests[0].result=dict(retry_result(requests[0]),building_scene=records['current-crop']['scene'])
    obj.step(ctx,1.2,replace(nav,sequence=nav.sequence+2),0)
    return obj,ctx,nav,requests,overview,records


def test_recorded_crop_failure_requests_one_wide_view_then_backoff_and_fresh_geometry(tmp_path,monkeypatch):
    obj,ctx,nav,requests,overview,records=overview_setup(tmp_path,monkeypatch)
    assert records['current-crop']['scene']['ground_measurement']['ground_pixels']==0
    assert not local_backoff_evidence(records['current-crop']['scene'],(1012,586))
    assert local_backoff_evidence(records['current-overview']['scene'],(1688,977))
    assert obj.overview_request is overview[0] and requests[0].closed and obj.calls==2
    assert obj.relocation is None and obj.plan is None
    obj.step(ctx,1.4,replace(nav,sequence=nav.sequence+3),0)
    assert overview[0].closed and isinstance(obj.relocation,BuildingBackoffProbe)
    # The wide picture never became a route or calibration reference.
    assert obj.anchor is obj.plan is None and obj.overview_used
    for i in range(140):
        nav=replace(nav,sequence=nav.sequence+1)
        cmd=obj.step(ctx,1.5+i*.1,nav,0)
        assert cmd.outcome is None
        nav,_=simulate(cmd,nav,0)
        if len(requests)==2:break
    assert len(requests)==2 and len(overview)==1
    assert requests[-1].frame.size==(1012,586) and requests[-1].job['full_window']
    assert requests[-1].frame.frame_id!=overview[0].frame_id
    assert requests[-1].frame.player_position==nav.position
    assert obj.goal_mode=='building_entry' and not obj.passages


@pytest.mark.parametrize('fault',['moved','camera','old_frame','delayed'])
def test_overview_pose_and_frame_changes_never_authorize_movement(tmp_path,monkeypatch,fault):
    obj,ctx,nav,_,overview,_=overview_setup(tmp_path,monkeypatch)
    now=1.4
    if fault=='moved':nav=replace(nav,position=(nav.position[0]+.001,nav.position[1]))
    if fault=='camera':nav=replace(nav,camera_epoch=nav.camera_epoch+1)
    if fault=='old_frame':overview[0].result['frame_id']='old'
    if fault=='delayed':now=17
    cmd=obj.step(ctx,now,replace(nav,sequence=nav.sequence+10),0)
    assert overview[0].closed and obj.relocation is None and not cmd.tap and not cmd.keys
    assert cmd.outcome is not None or obj.building_recovery


def test_cancelling_pending_overview_closes_worker_request(tmp_path,monkeypatch):
    obj,ctx,nav,_,overview,_=overview_setup(tmp_path,monkeypatch)
    overview[0].result=None
    obj.step(ctx,1.4,replace(nav,sequence=nav.sequence+3),0)
    obj.close()
    assert overview[0].closed and obj.overview_request is None
    assert obj.step(ctx,1.5,nav,0).outcome=='segmentation_closed'


def test_wide_ui_exclusion_keeps_actual_lower_right_world_patch():
    from bot.perception.building_scene import building_ui_exclusion
    mask=building_ui_exclusion((977,1688),full_window=True)
    assert not mask[830,1250]
    for x,y in [(850,800),(1500,500),(1550,160),(200,500),(240,800),(50,930)]:
        assert mask[y,x]
