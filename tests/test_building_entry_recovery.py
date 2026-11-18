"""Saved entry image and synthetic control receipts, never live entry success."""
from dataclasses import replace
import json
import math
from pathlib import Path
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.control.quest_travel import TravelCommand
from bot.perception.automatic_ground import AutomaticGround
from bot.perception.building_scene import describe_building_scene,building_ui_exclusion
from bot.perception.ground_projection import ProjectionError,ProjectionFrame,GroundProjection
from bot.perception.segmentation import fuse_masks
from bot.perception.travel_channel import CameraSample,CameraScene,CameraResult,TravelFlags
from experiments.route_models.segmentation_worker import building_job
from test_segmented_runtime import controller,ready


def recorded():
    path=Path(__file__).parent/'fixtures/building-entry-20261117'
    meta=json.loads(path.with_suffix('.json').read_text())
    with np.load(path.with_suffix('.npz')) as arrays:
        data={k:arrays[k] for k in arrays.files}
    instances=[dict(item,mask=data['mask_'+str(i)]) for i,item in enumerate(meta['instances'])]
    observed=SimpleNamespace(**{k:data[k] for k in ('ground','obstacles','occluded','water','forbidden')},
        semantic_labels=data['labels'],semantic_confidence=data['confidence'],instances=instances)
    values=meta['request']['frame']
    for key in ('size','world_size','player_position','camera_signature'):values[key]=tuple(values[key])
    return meta,data,observed,replace(ProjectionFrame(**values),captured_at=time.monotonic())


def test_saved_opening_with_stairs_is_recognized_but_hidden_threshold_is_not_passage():
    meta,data,observed,frame=recorded()
    result=describe_building_scene(observed,meta['names'],camera_view=2)
    assert result['doorways'] and all(not d['passage_verified'] for d in result['doorways'])
    assert all(not d['threshold_observed'] for d in result['doorways'])
    assert not result['entrance_verified'] and not result['executable']
    # Real opening lies to the player's left. Wall is tested AROUND the doorway.
    assert result['look_hint']=='left'
    assert all(d['surround_structure'][0]>.18 for d in result['doorways'])
    assert all(np.array_equal(data[k],getattr(observed,k)) for k in ('ground','obstacles','water'))


def test_real_entry_image_retains_door_and_requests_reframe_when_actual_texture_is_insufficient():
    meta,data,observed,frame=recorded()
    assert (data['old_ground']&(data['old_confidence']>=.9)).sum()<1000
    assert (observed.ground&(observed.semantic_confidence>=.9)).sum()>4000
    assert not (observed.ground&building_ui_exclusion(observed.ground.shape)).any()
    job=dict(meta['request'],building_scene=describe_building_scene(observed,meta['names'],camera_view=2))
    calibration=AutomaticGround(camera_views=(2,4,5))
    result=building_job(calibration,frame,data['rgb'],observed,job)
    assert result['kind']=='building_reobserve'
    assert result['reason']=='auto_ground_texture_insufficient'
    assert result['ground_measurement']['texture_features']<40
    assert result['executable'] is False and 'next_segment_world' not in result
    assert calibration.matrix is None  # One saved picture cannot establish scale.


def test_synthetic_clear_texture_resumes_measurement_but_cannot_claim_a_metric_route():
    meta,data,observed,frame=recorded()
    rng=np.random.default_rng(91)
    rgb=cv2.GaussianBlur(rng.integers(0,255,data['rgb'].shape,dtype=np.uint8),(3,3),0)
    job=dict(meta['request'],building_scene=describe_building_scene(observed,meta['names'],camera_view=2))
    result=building_job(AutomaticGround(camera_views=(2,4,5)),frame,rgb,observed,job)
    assert result['kind']=='calibration_orbit' and result['ground_measurement']['texture_features']>=40
    assert not result['executable'] and 'next_segment_world' not in result


def test_real_old_mask_now_requests_view_recovery_without_calibration_or_movement():
    meta,data,observed,frame=recorded();observed.ground=data['old_ground']
    job=dict(meta['request'],building_scene=describe_building_scene(observed,meta['names'],camera_view=2))
    calibration=AutomaticGround(camera_views=(2,4,5))
    result=building_job(calibration,frame,data['rgb'],observed,job)
    assert result['kind']=='building_reobserve' and result['reason']=='auto_ground_texture_insufficient'
    assert result['building_scene']['doorways'] and calibration.base is None


def test_no_door_allows_local_measurement_but_never_a_route_toward_destination():
    meta,data,observed,frame=recorded()
    calibration=AutomaticGround(camera_views=(2,4,5))
    rgb=cv2.GaussianBlur(np.random.default_rng(91).integers(0,255,data['rgb'].shape,dtype=np.uint8),(3,3),0)
    job=dict(meta['request'],building_scene=dict(schema=1,surface='exterior',doorways=[]))
    result=building_job(calibration,frame,rgb,observed,job)
    assert result['kind']=='calibration_orbit' and calibration.base is not None
    assert not result['executable'] and 'next_segment_world' not in result


@pytest.mark.parametrize('label',['building','house','road','grass','stairs'])
def test_context_boxes_preserve_semantic_ground_without_erasing_solids_or_unknown(label):
    labels=np.ones((100,100),np.uint8);labels[:,50:60]=2;labels[:,70:80]=3
    confidence=np.full(labels.shape,.95);confidence[80:90,:]=.4
    context=dict(label=label,mask_score=.95,mask=np.ones_like(labels,bool),box=[0,0,100,100])
    ground,solid,unknown=fuse_masks(labels,confidence,np.full(labels.shape,.8),
                                   {1:'grass',2:'wall',3:'unidentified'},[context])
    assert ground[40,40] and solid[40,55] and unknown[40,75] and unknown[85,40]
    fence=dict(context,label='wooden fence',mask_score=.7,box=[30,0,45,100],mask=np.zeros_like(labels,bool))
    fenced,_,_=fuse_masks(labels,confidence,np.full(labels.shape,.8),
                          {1:'grass',2:'wall',3:'unidentified'},[context,fence])
    assert not fenced[40,40]  # Context cannot override uncertain solid boxes.


@pytest.mark.parametrize('label',['wall','roof','ceiling'])
@pytest.mark.parametrize('score',[.7,.95])
def test_structural_surface_masks_still_override_semantic_grass(label,score):
    labels=np.ones((100,100),np.uint8);mask=np.zeros_like(labels,bool);mask[:,40:60]=True
    ground,solid,unknown=fuse_masks(labels,np.full(labels.shape,.95),np.full(labels.shape,.8),
        {1:'grass'},[dict(label=label,mask_score=score,mask=mask,box=[20,0,80,100])])
    assert not ground[40,50] and ground[40,30]
    assert solid[40,50] if score>=.85 else unknown[40,50]


@pytest.mark.parametrize('fault',['solid_interior','no_frame','window'])
def test_door_label_alone_does_not_authorize_an_entrance(fault):
    labels=np.zeros((120,120),np.uint8)
    labels[20:80,30:90]=1;labels[30:80,40:80]=2
    if fault=='solid_interior':labels[30:80,40:80]=1
    if fault=='no_frame':labels[labels==1]=0
    if fault=='window':labels[30:80,40:80]=3
    observed=SimpleNamespace(semantic_labels=labels,semantic_confidence=np.ones_like(labels,float),
        water=np.zeros_like(labels,bool),instances=[dict(label='open doorway',box=[40,30,80,80],
                                                       detection_score=.8,mask_score=.95)])
    scene=describe_building_scene(observed,{0:'person',1:'house',2:'stairs',3:'window'},camera_view=2)
    assert not scene['doorways']


def facade_nav(nav,view=2,sequence=None,request=0):
    camera=CameraSample(32,view,CameraScene.GROUND,CameraScene.GROUND if view==2 else CameraScene.OVERHEAD,
                        request,CameraResult.DISTANCE_VERIFIED,False,False)
    return replace(nav,camera=camera,camera_restore_available=True,overhead_camera_available=True,
                   sequence=nav.sequence if sequence is None else sequence)


def retry_result(request,reason='auto_ground_texture_insufficient'):
    return dict(frame_id=request.frame.frame_id,target_key='12345:READY:0',kind='building_reobserve',
        reason=reason,executable=False,requires_fresh_feedback=True,
        building_scene=dict(schema=1,executable=False,doorways=[],look_hint=None))


@pytest.mark.parametrize('late_result',[False,True])
def test_first_failure_switches_view_waits_for_fresh_receipt_then_reobserves(tmp_path,monkeypatch,late_result):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry';nav=facade_nav(nav)
    nav,heading=ready(obj,ctx,nav)
    requests[0].result=retry_result(requests[0]);session=obj.session_id;deadline=obj.deadline
    assert obj.step(ctx,1.2,nav,heading)==TravelCommand()
    assert obj.building_recovery and requests[0].closed and obj.session_id!=session
    assert obj.anchor is obj.pending is obj.plan is None and obj.deadline==deadline
    assert obj.step(ctx,1.3,nav,heading).keys==frozenset({'f6'})
    received=facade_nav(nav,view=4,sequence=3,request=1)
    assert obj.step(ctx,1.5,received,heading)==TravelCommand()  # Release camera key.
    assert obj.step(ctx,1.9,received,heading)==TravelCommand()
    assert len(requests)==1
    received=replace(received,sequence=4)
    assert obj.step(ctx,2.1,received,heading)==TravelCommand()
    assert obj.building_recovery is None and obj.anchor is None
    obj.step(ctx,2.7,replace(received,sequence=5),heading)
    obj.step(ctx,2.8,replace(received,sequence=6),heading)
    assert len(requests)==2 and requests[-1].frame.camera_signature[0]==4
    assert requests[-1].job['session_id']!=session
    assert requests[-1].frame.frame_id!=requests[0].frame.frame_id
    assert not obj.plan and obj.total==0
    if late_result:
        requests[-1].result=requests[0].result  # A late result from the former view.
        assert obj.step(ctx,2.9,replace(received,sequence=7),heading).outcome=='segmentation_result_frame_mismatch'
        assert obj.plan is None
    else:
        requests[-1].result=dict(frame_id=requests[-1].frame.frame_id,target_key='12345:READY:0',
            kind='calibration_orbit',yaw_pixels=110,calibration=dict(stage='orbit'),
            executable=False,requires_fresh_feedback=True,
            building_scene=dict(schema=1,executable=False,doorways=[dict(box=[10,10,40,50])]))
        assert obj.step(ctx,2.9,replace(received,sequence=7),heading)==TravelCommand()
        cmd=obj.step(ctx,3,replace(received,sequence=8),heading)
        assert cmd.keys=={'f4'} and obj.orbit_request and obj.plan is None


def test_recovery_is_bounded_and_does_not_reset_movement_or_collision_budgets(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry';nav=facade_nav(nav)
    obj.total=2;obj.pulses=3;obj.calls=5;obj.blocked=[(.4,.51)]
    for count,view in [(1,2),(2,4)]:
        obj.building_scene=dict(look_hint='left')
        assert obj.recover_building_view(ctx,count+1,facade_nav(nav,view),
                                        'auto_ground_texture_insufficient')==TravelCommand()
        assert obj.building_recoveries==count
    assert (obj.total,obj.pulses,obj.calls,obj.deadline)==(2,3,5,61)
    assert obj.blocked==[(.4,.51)]
    assert obj.recover_building_view(ctx,4,nav,'auto_ground_texture_insufficient').outcome==\
        'building_observation_recovery_exhausted'


def test_unseen_position_after_retreat_survives_old_watchdog_and_crosses_door(tmp_path,monkeypatch):
    from test_building_backoff import local_scene
    from test_building_relocation import simulate
    from bot.perception.travel_channel import TaskFlags
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry'
    obj.deadline=181
    nav=facade_nav(nav,view=2)
    heading=1.5*np.pi
    events=[]
    ctx.log_event=lambda name,data:events.append((name,data))
    # Reproduce the late relocation in 90896: only ~14 seconds remain on
    # the ORIGINAL idle clock. The next full inference returns after it expires.
    obj.step(ctx,46,nav,heading)
    nav=replace(nav,sequence=nav.sequence+1)
    obj.step(ctx,46.1,nav,heading)
    requests[0].result=dict(retry_result(requests[0],'tracking_seed_insufficient'),
                            building_scene=local_scene(requests[0].frame.size))
    assert obj.step(ctx,46.2,nav,heading)==TravelCommand() and obj.relocation
    obj.overview_capture=lambda _:np.zeros((300,500,3),np.uint8)
    captured=[]
    for i in range(280):
        now=46.3+i*.1
        nav=replace(nav,sequence=(nav.sequence+1)%128)
        if obj.request and obj.request not in captured:
            request=obj.request
            result=request.result
            request.result=None
            if not captured:
                assert request.job['full_window'] and request.frame.size==(500,300)
                assert request.frame.camera_signature[0]==4
            elif any(k=='quest_building_facade_reacquisition' for k,_ in events):
                assert not request.job['full_window'] and request.frame.size==(100,100)
                assert request.frame.camera_signature[0]==2
            captured.append(request)
        if captured and obj.request is captured[-1] and now>=62:
            origin=list(obj.request.frame.player_position)
            target=[origin[0]+.0005,origin[1]]
            scene=local_scene(obj.request.frame.size)
            if not any(k=='quest_building_facade_reacquisition' for k,_ in events):
                basis='local_observation_frontier'
            else:
                scene['doorways']=[{}]
                basis='door_threshold' if obj.goal_mode=='building_entry' else 'door_floor_crossing'
            result.update(building_scene=scene,
                          automatic_goal=dict(goal_kind='building_entry_region',basis=basis),
                          next_segment_world=[origin,target],search_goal_world=target)
            obj.request.result=result
        command=obj.step(ctx,now,nav,heading)
        assert command.outcome is None
        if command.keys=={'f6'}:
            nav=facade_nav(nav,view=4,request=nav.camera.request+1)
        if command.keys=={'f8'}:
            nav=facade_nav(nav,view=2,request=nav.camera.request+1)
        nav,heading=simulate(command,nav,heading,speed=4.)
        if command.tap=='w':
            # Independently integrate the issued forward pulse at a fixed speed.
            yards=command.hold_s*6.
            nav=replace(nav,position=(nav.position[0]-math.sin(heading)*yards/1000,
                                      nav.position[1]-math.cos(heading)*yards/1000))
        if obj.passages:break
    assert obj.deadline==181 and obj.goal_mode=='building_search' and obj.passages
    assert now>61 and len(captured)>=3
    assert any(k=='quest_building_new_observation_position' for k,_ in events)
    assert sum(k=='quest_building_facade_reacquisition' for k,_ in events)==1
    assert sum(k=='quest_building_passage_crossed' for k,_ in events)==1
    nav=replace(nav,sequence=nav.sequence+1,
                task=TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN|TaskFlags.NPC_WITHIN_5_YARDS)
    assert obj.step(ctx,now+.1,nav,heading).outcome=='building_entered'


def test_return_to_visited_position_does_not_renew_idle_time(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    other=(nav.position[0]+.001,nav.position[1])
    assert obj.record_position(20,other)
    assert not obj.record_position(40,nav.position) and not obj.record_position(45,other)
    assert obj.progress_at==20


def test_ungranted_overhead_lease_releases_then_turns_at_same_position(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry'
    obj.calibration_action=dict(kind='calibration_orbit',yaw_pixels=110)
    assert obj.step(ctx,1,nav,0).keys=={'f4'}
    nav=replace(nav,sequence=nav.sequence+1)
    assert obj.step(ctx,1.6,nav,0)==TravelCommand()
    command=obj.step(ctx,1.7,replace(nav,sequence=nav.sequence+1),0)
    assert command.tap in {'left','right'} and not command.keys and obj.orbit.keyboard
    assert not obj.calibration_coupled and obj.progress_at==1


def test_wide_support_samples_the_same_physical_neighbourhood_as_task_crop():
    from bot.perception.building_scene import support_zone
    full=np.arange(1000*1500).reshape(1000,1500)
    crop=full[80:680,300:1200]
    for box in [(0.25,0.6,0.75,0.92),(0.4,0.68,0.6,0.9)]:
        assert np.array_equal(full[support_zone(full.shape,box,True)],
                              crop[support_zone(crop.shape,box,False)])


def test_fast_building_rotation_requires_full_review_before_translation(monkeypatch):
    from experiments.route_models import segmentation_worker as worker
    from test_automatic_calibration import generated_frames
    calibration=AutomaticGround()
    base,rotated,second,*_=generated_frames()
    job=dict(quest_id=90896,phase='ACTIVE')
    assert not worker.building_rotation_only(calibration,base[0],job)
    calibration.update(*base,now=base[0].captured_at+.1)
    assert worker.building_rotation_only(calibration,rotated[0],job)
    frame,rgb,obs=rotated
    obs.diagnostics={'geometry_only':True}
    obs.forbidden[:]=True
    monkeypatch.setattr(worker.time,'monotonic',lambda:frame.captured_at+.1)
    result=worker.stationary_building_job(calibration,frame,rgb,obs,job)
    assert result['stationary_only'] and result['kind']=='calibration_orbit'
    assert not worker.building_rotation_only(calibration,second[0],job)
    assert not result['executable'] and 'building_scene' not in result and 'next_segment_world' not in result


@pytest.mark.parametrize('kind',['route','calibration_probe','building_reobserve','stair_probe'])
def test_stationary_image_cannot_authorize_motion_or_retreat(tmp_path,monkeypatch,kind):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry'
    nav,heading=ready(obj,ctx,nav)
    requests[0].result.update(stationary_only=True,kind=kind)
    cmd=obj.step(ctx,1.2,nav,heading)
    assert cmd.outcome=='auto_calibration_action_invalid' and not cmd.keys and not cmd.tap
    assert obj.building_observation is obj.relocation is obj.plan is None


@pytest.mark.parametrize('fault',['auto_input_direction_mismatch','probe_telemetry_stale','segmentation_frame_hash_mismatch'])
def test_input_or_state_faults_are_not_retried_as_camera_observability(tmp_path,monkeypatch,fault):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    assert obj.recover_building_view(ctx,2,nav,fault).outcome=='segmentation_'+fault
    assert obj.building_recovery is None


def test_combat_interrupts_camera_recovery_without_input(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry';nav=facade_nav(nav)
    obj.recover_building_view(ctx,1,nav,'auto_ground_texture_insufficient')
    cmd=obj.step(ctx,1.1,replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT),0)
    assert cmd.outcome=='probe_interrupted' and not cmd.keys and not cmd.tap


def test_short_heading_gap_during_reframe_releases_then_resumes(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry';nav=facade_nav(nav)
    obj.recover_building_view(ctx,1,nav,'auto_ground_texture_insufficient')
    deadline=obj.deadline
    ctx.heading_age=lambda:.7
    assert obj.step(ctx,1.1,nav,0)==TravelCommand()
    assert obj.building_recovery and obj.heading_wait==1.1
    ctx.heading_age=lambda:.05
    cmd=obj.step(ctx,1.3,replace(nav,sequence=nav.sequence+1),0)
    assert cmd.keys==frozenset({'f6'}) and not cmd.outcome
    assert obj.heading_wait is None and obj.deadline==deadline


def test_combat_cannot_hide_behind_heading_wait(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    ctx.heading_age=lambda:.7
    assert obj.step(ctx,1,nav,0)==TravelCommand()
    cmd=obj.step(ctx,1.1,replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT),0)
    assert cmd.outcome=='probe_interrupted' and not cmd.keys and not cmd.tap


def test_stale_or_missing_entrance_cannot_authorize_building_movement(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    nav,heading=ready(obj,ctx,nav)
    requests[0].result['automatic_goal']['goal_kind']='building_entry_region'
    assert obj.step(ctx,1.2,nav,heading).outcome=='building_entrance_evidence_missing'
    assert obj.plan is None


def test_door_frontier_follows_observed_entrance_even_away_from_task_pin():
    from test_ground_projection import fixture
    from bot.decision.building_entry import building_entry_region
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    ground=np.ones((600,800),bool);zero=np.zeros_like(ground)
    wall=zero.copy();wall[:,435:]=True
    observed=SimpleNamespace(ground=ground&~wall,obstacles=wall,water=zero,forbidden=zero,occluded=zero)
    # The doorway is left and outside current measured support; the pin is right.
    scene=dict(doorways=[dict(box=[100,200,190,300],threshold_box=[100,294,190,320])])
    region,goal=building_entry_region(projection,observed,None,world((550,300)),scene)
    assert goal['basis']=='door_approach_frontier' and goal['requires_reobservation']
    assert region.any() and np.nonzero(region)[1].max()<400
    assert not (region&wall).any() and not goal['entrance_verified']


def test_visible_but_disconnected_threshold_uses_reachable_frontier_not_wall_crossing():
    from test_ground_projection import fixture
    from bot.decision.building_entry import building_entry_region
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    ground=np.ones((600,800),bool);zero=np.zeros_like(ground)
    wall=zero.copy();wall[:,480:500]=True
    observed=SimpleNamespace(ground=ground&~wall,obstacles=wall,water=zero,forbidden=zero,occluded=zero)
    scene=dict(doorways=[dict(box=[510,200,560,300],threshold_box=[510,294,560,320])])
    region,goal=building_entry_region(projection,observed,None,world((550,300)),scene)
    assert goal['basis']=='door_approach_frontier'
    assert region.any() and np.nonzero(region)[1].max()<480
    region,goal=building_entry_region(projection,observed,None,world((550,300)),scene,crossing=True)
    assert goal['basis']=='door_floor_observation' and goal['requires_reobservation']
    assert not goal['entrance_verified'] and np.nonzero(region)[1].max()<480
    assert not (region&wall).any()
    for y,x in zip(*np.nonzero(region)):
        assert .5<=np.linalg.norm(projection.delta((int(x),int(y))))<=2
