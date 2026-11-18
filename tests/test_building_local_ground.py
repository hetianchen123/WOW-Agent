"""21:45 saved images + simulated receipts. No claim of live building entry."""
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.control.pathfinding import GridRouter
from bot.control.quest_travel import TravelCommand
from bot.control import segmented_detour as sd
from bot.perception.automatic_ground import AutomaticGround, sample
from bot.perception.building_scene import building_ground_measurement
from bot.perception.ground_projection import ProjectionFrame, GroundProjection, ProjectionError
from bot.perception.ground_tracking import validate_tracking
from bot.perception.travel_channel import TravelFlags as F
from experiments.route_models.replay_landing_handoff import native
from experiments.route_models.segmentation_worker import building_job
from test_building_access import setup
from test_building_entry_recovery import facade_nav, retry_result
from test_segmented_runtime import controller, ready


FIXTURE=Path(__file__).parent/'fixtures/building-local-ground-20261117'


def recorded(index):
    meta=json.loads(FIXTURE.with_suffix('.json').read_text())[index]
    with np.load(FIXTURE.with_suffix('.npz')) as data:
        rgb=data[f'{index}_rgb']
        fields={k:data[f'{index}_{k}'] for k in ('ground','obstacles','occluded','water','forbidden')}
        fields['semantic_confidence']=data[f'{index}_confidence'].astype(np.float32)
    return meta,rgb,SimpleNamespace(**fields)


@pytest.mark.parametrize('index',[0,1,2])
def test_low_ground_surface_hands_off_without_claiming_exterior_or_walking_to_pin(index,tmp_path,monkeypatch):
    meta,rgb,_=recorded(index)
    obj,ctx,_,events,requests,run=setup(tmp_path,monkeypatch)
    nav=native(meta['saved_native'])
    obj.step(ctx,1,nav,0);obj.ground_only=True
    obj.step(ctx,1.6,replace(nav,sequence=nav.sequence+1),0)
    requests[-1].result=meta['scene']
    assert meta['scene']['surface']=='unknown'
    assert meta['scene']['support']['exterior']<.12
    assert obj.step(ctx,1.7,replace(nav,sequence=nav.sequence+2),0)==TravelCommand()
    assert obj.state=='ground_travel' and not obj.points
    started=[]
    obj.entry_factory=lambda *args:started.append(args) or SimpleNamespace(close=lambda:None)
    assert obj.step(ctx,1.8,replace(nav,sequence=nav.sequence+3),0)==TravelCommand()
    assert obj.state=='entry' and len(started)==1 and len(requests)==1
    assert not any(e=='quest_building_entered' for e,_ in events)
    assert any(e=='quest_building_local_observation' and not d['exterior_verified']
               and not d['translation_authorized'] for e,d in events)
    run.building_access=obj
    assert run.building_pending and run.trip is None


def test_saved_small_patch_reaches_real_measurement_controller_without_extra_camera_cycle(tmp_path,monkeypatch):
    meta,rgb,observed=recorded(1)
    obj,ctx,_,events,requests,_=setup(tmp_path,monkeypatch)
    nav=native(meta['saved_native'])
    created=[];entries=[]
    monkeypatch.setattr(sd,'preflight',lambda *_:'automatic-local-plane')
    class Request:
        def __init__(self,image,frame,job):
            created.append(self)
            self.result=building_job(AutomaticGround(camera_views=(2,4,5)),frame,image,observed,
                                     dict(job,building_scene=meta['scene']))
        def poll(self):return self.result
        def close(self):pass
    def entry(at,nav,ctx):
        value=sd.SegmentedDetour(at,nav,GridRouter(*nav.world_size),'ccw_west',obj.config,
            lambda _:rgb,deadline=at+60,request_factory=Request,goal_mode='building_entry')
        entries.append(value)
        return value
    obj.entry_factory=entry
    obj.step(ctx,1,nav,0);obj.ground_only=True
    obj.step(ctx,1.6,replace(nav,sequence=nav.sequence+1),0)
    requests[-1].result=meta['scene']
    commands=[]
    for i,at in enumerate([1.7,1.8,1.9,2.5,2.6,2.7,2.8],2):
        commands.append(obj.step(ctx,at,replace(nav,sequence=(nav.sequence+i)%128),0))
    assert len(created)==1 and created[0].result['kind']=='calibration_orbit'
    assert entries[0].building_recoveries==0 and entries[0].plan is None
    assert commands[-1].keys=={'f4'} and entries[0].orbit_request
    assert all(not (c.keys-{'f4'}) and c.tap not in {'w','s','q','e','f6','f8'} for c in commands)
    assert not obj.points and obj.state=='entry'


def test_ground_measurement_changes_look_direction_instead_of_following_occluded_door(tmp_path,monkeypatch):
    meta,_,_=recorded(0)
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    nav=facade_nav(nav)
    obj.seed_building_observation(meta['scene'])
    assert obj.initial_building_reframe
    assert obj.step(ctx,1,nav,0)==TravelCommand()
    assert not requests and obj.building_recovery
    assert obj.building_scene['look_hint']=='left'
    assert obj.building_recovery.hint==meta['scene']['ground_measurement']['look_hint']
    assert obj.building_recovery.hint=='right'
    assert obj.step(ctx,1.1,replace(nav,sequence=nav.sequence+1),0).keys==frozenset({'f6'})


@pytest.mark.parametrize('kind',['calibration_orbit','calibration_observe','calibration_probe'])
def test_losing_door_during_measurement_does_not_reset_local_geometry(kind,tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    nav,heading=ready(obj,ctx,nav)
    result=dict(retry_result(requests[0]),kind=kind)
    if kind=='calibration_orbit':result.update(calibration=dict(stage='orbit'),yaw_pixels=110)
    if kind=='calibration_observe':result.update(calibration=dict(stage='observe'),wait_s=.7)
    if kind=='calibration_probe':result.update(calibration=dict(stage='translation'),
        key='s',hold_s=.07,metric_route=False)
    requests[0].result=result
    session=obj.session_id
    assert obj.step(ctx,1.2,nav,heading)==TravelCommand()
    assert obj.session_id==session and obj.calibration_action==result
    assert obj.building_recovery is None and obj.plan is None


def test_missing_door_after_actual_geometry_still_cannot_generate_task_pin_route():
    from test_ground_projection import fixture
    frame,train,checks,_=fixture();projection=GroundProjection(frame,train,checks)
    rgb=cv2.GaussianBlur(np.random.default_rng(17).integers(0,255,(600,800,3),dtype=np.uint8),(3,3),0)
    zero=np.zeros((600,800),bool)
    observed=SimpleNamespace(ground=~zero,obstacles=zero,occluded=zero,water=zero,forbidden=zero,
        semantic_confidence=np.ones(zero.shape))
    calibration=SimpleNamespace(base=True,update=lambda *_a,**_k:(projection,dict(stage='ready')))
    result=building_job(calibration,frame,rgb,observed,dict(quest_id=1,phase='READY',
        goal_mode='building_entry',building_scene=dict(surface='unknown',doorways=[])))
    assert result['kind']=='building_reobserve' and result['reason']=='building_entrance_unobserved'
    assert not result['executable'] and 'next_segment_world' not in result


def test_local_registration_does_not_depend_on_ground_fraction_of_image():
    # Identical usable local patch, with increasing unrelated scenery/UI around
    # it. Former 1.5% screen-area threshold rejected this valid local geometry.
    xx,yy=np.meshgrid(np.linspace(440,520,24),np.linspace(390,470,24))
    points=np.c_[xx.ravel(),yy.ravel()]
    target=points+np.array([5,-3])
    outputs=[]
    for size in [(800,600),(1600,1200),(3840,2160)]:
        matrix,proof=validate_tracking(points,target,size)
        assert proof['holdout_max_px']<.01 and proof['fit_ratio']==1
        outputs.append(matrix)
    assert all(np.allclose(outputs[0],m) for m in outputs[1:])
    # Many points on a narrow line remain unusable despite perfect residuals.
    line=points.copy();line[:,1]*=.01
    with pytest.raises(ProjectionError,match='spatial_support'):
        validate_tracking(line,line+[5,-3],(800,600))


def test_small_ground_area_can_begin_observation_without_claiming_projection():
    frame=ProjectionFrame('small','a'*64,(800,600),2451,'automatic-local-plane',(1000,1000),
        (.4,.5),None,0,0,(4,32,0),time.monotonic())
    ground=np.zeros((600,800),bool);ground[400:430,390:420]=True
    observed=SimpleNamespace(ground=ground,semantic_confidence=np.ones(ground.shape))
    result=sample(frame,np.zeros((600,800,3),np.uint8),observed)
    assert np.count_nonzero(result.ground_mask)==900
    # Sampling is not fitting: actual textures, rotations, native movement,
    # independent holdouts, current hull/corridor still gate any route.


def test_structure_surface_does_not_become_local_measurement_permission():
    meta,rgb,observed=recorded(1)
    frame=ProjectionFrame('hazard','a'*64,(rgb.shape[1],rgb.shape[0]),2451,'automatic-local-plane',
        (1000,1000),(.4,.5),None,0,0,(4,32,0),time.monotonic())
    with pytest.raises(ProjectionError,match='building_not_ground_surface'):
        building_job(AutomaticGround(),frame,rgb,observed,
                     dict(building_scene=dict(meta['scene'],surface='structure')))


def test_actual_water_without_dry_ground_cannot_authorize_measurement_or_movement():
    meta,rgb,observed=recorded(1)
    # Overall scene labels are diagnostic. Here the local masks themselves
    # contain no dry support, so even the stationary orbit must be deferred.
    observed.ground[:]=False
    observed.water[:]=True
    frame=ProjectionFrame('water','a'*64,(rgb.shape[1],rgb.shape[0]),2451,'automatic-local-plane',
        (1000,1000),(.4,.5),None,0,0,(4,32,0),time.monotonic())
    result=building_job(AutomaticGround(),frame,rgb,observed,
        dict(quest_id=1,phase='READY',building_scene=dict(meta['scene'],surface='water')))
    assert result['kind']=='building_reobserve'
    assert result['reason']=='auto_ground_texture_insufficient'
    assert not result['executable'] and 'next_segment_world' not in result
