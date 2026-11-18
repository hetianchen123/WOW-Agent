from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import time

import numpy as np
import pytest

from bot.control.building_access import BuildingAccess, ExteriorPoint
from bot.control.quest_travel import QuestTrip, TravelCommand
from bot.core.config import SegmentedNavigationConfig
from bot.perception.building_scene import describe_building_scene
from bot.perception.travel_channel import TravelFlags as F, TaskFlags
from test_npc_obstacle import npc


def historical_surfaces():
    path=Path(__file__).parent/'fixtures/building-surfaces-20261117'
    meta=json.loads(path.with_suffix('.json').read_text())
    with np.load(path.with_suffix('.npz')) as data:
        for i,row in enumerate(meta):
            observed=SimpleNamespace(semantic_labels=data[f'{i}_labels'],
                semantic_confidence=data[f'{i}_confidence'],water=data[f'{i}_water'],instances=row['instances'])
            yield row['name'],describe_building_scene(observed,row['names'])


def test_real_rooftops_are_rejected_and_obscured_ground_does_not_require_feet():
    results=list(historical_surfaces())
    assert [r['surface'] for _,r in results]==['exterior','structure','structure']
    # Models detected windows/decorations as doors/stairs in these actual frames.
    assert all(not r['doorways'] for _,r in results)
    assert all(not r['executable'] and r['world_coordinates'] is None for _,r in results)


def setup(tmp_path,monkeypatch,surface='exterior'):
    run,ctx,_,nav,events=npc(tmp_path,monkeypatch)
    nav=replace(nav,flags=F.KEYS_VERIFIED|F.OUTDOORS|F.CAN_FLY|F.CAN_MOUNT,height=0,
                destination=(.6,.5),descent_key='x')
    pending=[]
    class Request:
        def __init__(self,*_):
            self.frame_id='test-'+str(len(pending));self.folder=tmp_path/self.frame_id
            self.folder.mkdir();(self.folder/'image.png').write_bytes(b'test fixture')
            self.digest=hashlib.sha256(b'test fixture').hexdigest()
            self.result=dict(schema=1,surface=surface,executable=False,doorways=[])
            self.closed=False;pending.append(self)
        def poll(self):return self.result
        def close(self):self.closed=True
    obj=BuildingAccess(SegmentedNavigationConfig(enabled=True),capture=lambda _:np.zeros((50,50,3),np.uint8),
        convention='ccw_west',request_factory=Request,memory_path=tmp_path/'memory.json')
    return obj,ctx,nav,events,pending,run


def observe(obj,ctx,nav,*,start=1):
    assert obj.step(ctx,start,nav,0)==TravelCommand()
    nav=replace(nav,sequence=nav.sequence+1)
    assert obj.step(ctx,start+.6,nav,0)==TravelCommand()
    return obj.step(ctx,start+.7,replace(nav,sequence=nav.sequence+1),0)


def test_initial_exterior_is_persisted_and_normal_travel_resumes(tmp_path,monkeypatch):
    obj,ctx,nav,events,_,_=setup(tmp_path,monkeypatch)
    assert observe(obj,ctx,nav).outcome is None
    assert obj.state=='pass' and not obj.ground_only and obj.points
    assert obj.step(ctx,1.8,replace(nav,sequence=4),0) is None
    loaded=BuildingAccess(obj.config,capture=obj.capture,convention=obj.convention,memory_path=obj.memory_path)
    assert len(loaded.points)==1
    Path(obj.points[0].image).write_bytes(b'corrupt')
    loaded=BuildingAccess(obj.config,capture=obj.capture,convention=obj.convention,memory_path=obj.memory_path)
    assert not loaded.points


def test_zero_height_and_not_flying_do_not_authorize_rooftop_arrival(tmp_path,monkeypatch):
    obj,ctx,nav,_,requests,_=setup(tmp_path,monkeypatch,'structure')
    assert observe(obj,ctx,nav).outcome=='building_exterior_landing_unavailable'
    assert obj.ground_only and requests[0].closed and not obj.points
    assert not ctx.input._held_keys


def test_rooftop_uses_reviewed_ground_never_fixed_offset(tmp_path,monkeypatch):
    obj,ctx,nav,events,_,_=setup(tmp_path,monkeypatch,'structure')
    point=ExteriorPoint(nav.map_id,nav.world_size,(.25,.5),time.time(),'evidence','hash')
    obj.points=[replace(point,map_id=99),point]
    assert observe(obj,ctx,nav).outcome is None
    assert obj.state=='recover' and obj.recovery.destination==point.position
    assert obj.step(ctx,1.8,replace(nav,sequence=4),0).tap=='6'
    assert obj.recovery.mode=='fly'
    assert any(name=='quest_building_roof_recovery' for name,_ in events)


def test_rooftop_flight_failure_cannot_fall_back_to_walking(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch,'structure')
    obj.points=[ExteriorPoint(nav.map_id,nav.world_size,(.25,.5),time.time(),'evidence','hash')]
    observe(obj,ctx,nav)
    obj.recovery.failed_modes.add('fly')
    result=obj.step(ctx,1.8,replace(nav,sequence=4),0)
    assert result.outcome=='building_roof_flight_unavailable' and not result.keys and not result.tap


def test_expired_ground_and_other_map_are_not_flight_targets(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch,'structure')
    obj.points=[ExteriorPoint(nav.map_id,nav.world_size,(.25,.5),time.time()-21601,'e','h'),
                ExteriorPoint(99,nav.world_size,(.25,.5),time.time(),'e','h')]
    assert observe(obj,ctx,nav).outcome=='building_exterior_landing_unavailable'


def test_landing_reviews_before_dismount_or_search_and_requires_fresh_sequences(tmp_path,monkeypatch):
    obj,ctx,nav,_,requests,_=setup(tmp_path,monkeypatch)
    observe(obj,ctx,nav)
    flight=replace(nav,sequence=4,flags=nav.flags|F.FLYING|F.MOUNTED)
    assert obj.step(ctx,1.8,flight,0) is None
    landed=replace(flight,sequence=5,flags=flight.flags&~F.FLYING,position=(.59,.5))
    assert obj.step(ctx,2,landed,0)==TravelCommand()
    assert obj.step(ctx,2.6,landed,0)==TravelCommand()
    assert len(requests)==1
    assert obj.step(ctx,2.7,replace(landed,sequence=6),0)==TravelCommand()
    assert len(requests)==2


@pytest.mark.parametrize('change',[dict(position=(.41,.5)),dict(camera_epoch=2)])
def test_scene_result_is_discarded_after_pose_change(tmp_path,monkeypatch,change):
    obj,ctx,nav,_,requests,_=setup(tmp_path,monkeypatch)
    obj.step(ctx,1,nav,0);obj.step(ctx,1.6,replace(nav,sequence=2),0)
    result=obj.step(ctx,1.7,replace(nav,sequence=3,**change),0)
    assert result==TravelCommand() and not obj.points and requests[0].closed


def test_falling_and_dead_state_cannot_issue_building_movements(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch)
    assert obj.step(ctx,1,replace(nav,flags=nav.flags|F.FALLING),0)==TravelCommand()
    assert not obj.request and not obj.points
    obj.ground_only=True
    assert obj.step(ctx,1.1,nav,0,in_combat=True) is None
    assert obj.ground_only and obj.state=='initial'


def test_ground_recovery_stays_on_foot_until_entry_receipt(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,run=setup(tmp_path,monkeypatch)
    observe(obj,ctx,nav)
    obj.ground_only=True;obj.state='ground_travel'
    ctx.cfg.segmented_navigation.enabled=True;ctx.cfg.segmented_navigation.building_access=True
    run.require_prepared=True;run.building_access=obj
    assert run.building_pending
    obj.entry_factory=lambda *_:SimpleNamespace(step=lambda *_:TravelCommand(),close=lambda:None)
    assert run.process_building_access(ctx,1.8,replace(nav,sequence=4),0)
    assert obj.state=='entry' and obj.ground_only and run.trip is None
    assert run.arrived is False
    obj.state='inside'
    assert not run.building_pending and obj.ground_only


def test_entry_has_to_confirm_crossing_not_merely_return_arrived(tmp_path,monkeypatch):
    obj,ctx,nav,events,_,_=setup(tmp_path,monkeypatch)
    observe(obj,ctx,nav)
    obj.ground_only=True;obj.state='ground_travel'
    near=replace(nav,sequence=4,position=(.57,.5))
    entry=SimpleNamespace(step=lambda *_:TravelCommand(),close=lambda:None)
    obj.entry_factory=lambda *_:entry
    assert obj.step(ctx,1.8,near,0)==TravelCommand()
    assert obj.state=='entry'
    assert obj.step(ctx,1.9,replace(near,sequence=5),0)==TravelCommand()
    assert obj.state=='entry'
    entry.step=lambda *_:TravelCommand(outcome='building_entered')
    assert obj.step(ctx,2,replace(near,sequence=6,flags=near.flags&~F.OUTDOORS),0).outcome=='building_target_receipt_missing'
    assert obj.state=='entry'
    confirmed=replace(near,sequence=7,task=near.task|TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN|TaskFlags.NPC_WITHIN_5_YARDS)
    assert obj.step(ctx,2.1,confirmed,0) is None
    assert obj.state=='inside'


def test_small_roof_escape_can_still_launch_flying_mount():
    from test_quest_travel import sample
    nav=sample(flags=F.KEYS_VERIFIED|F.MOUNTED|F.SKYRIDING|F.CAN_FLY,
        position=(.2,.2),destination=(.2,.18),sky_pitch_key='0',sky_dismount_key='z',sky_ascent_key='2')
    trip=QuestTrip(nav.world_size,nav.destination,'ccw_west',flight_threshold=5)
    cmd=trip.step(1,nav,0)
    assert trip.mode=='skyriding' and cmd.tap=='2'


def test_building_entry_does_not_need_selected_npc(tmp_path,monkeypatch):
    from test_segmented_runtime import controller
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry'
    nav=replace(nav,task=TaskFlags.PREPARED,flags=nav.flags|F.OUTDOORS)
    obj.identity=(nav.quest_id,nav.map_id,nav.phase,nav.objective_index,nav.objective_target,
                  nav.world_size,nav.destination,nav.destination_map)
    assert obj.step(ctx,1,nav,0).outcome is None
    assert obj.pending is not None


def test_indoor_flag_before_crossing_is_not_entry_receipt(tmp_path,monkeypatch):
    from test_segmented_runtime import controller
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_cross';obj.total=3;obj.crossing_baseline=3
    assert obj.step(ctx,1,nav,0).outcome is None
    obj.total=3.4
    # Indoor status plus arbitrary motion is not proof of crossing the door.
    assert obj.step(ctx,1.1,replace(nav,sequence=2),0).outcome is None
    assert obj.crossing_confirmed is None and not obj.closed


def test_doorway_goal_keeps_wall_and_unknown_floor_closed():
    from test_ground_projection import fixture
    from bot.perception.ground_projection import GroundProjection,ProjectionError
    from bot.decision.building_entry import building_entry_region
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    empty=np.zeros((600,800),bool);ground=np.ones_like(empty)
    obstacles=empty.copy();obstacles[200:360,450:540]=True
    observed=SimpleNamespace(ground=ground,obstacles=obstacles,water=empty,forbidden=empty,occluded=empty)
    scene=dict(doorways=[dict(box=[450,200,540,360],threshold_box=[450,354,540,380])])
    region,goal=building_entry_region(projection,observed,None,world((520,300)),scene)
    assert goal['basis']=='door_threshold' and not goal['entrance_verified']
    assert region.any() and not (region&obstacles).any()
    closer,goal=building_entry_region(projection,observed,None,world((520,300)),scene,crossing=True)
    assert goal['basis']=='door_floor_observation' and goal['requires_reobservation']
    assert not (closer&obstacles).any() and not goal['entrance_verified']
    assert obstacles[300,500]  # Detection never cuts an opening into a wall.
    obstacles[250:360,460:530]=False
    region,goal=building_entry_region(projection,observed,None,world((520,300)),scene,crossing=True)
    assert region.any() and goal['basis']=='door_floor_crossing'


def test_no_entrance_cannot_create_a_facade_route_toward_pin():
    from test_ground_projection import fixture
    from bot.perception.ground_projection import GroundProjection,ProjectionError
    from bot.decision.building_entry import building_entry_region
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    ground=np.ones((600,800),bool);empty=np.zeros_like(ground)
    solid=empty.copy();solid[:,435:]=True
    observed=SimpleNamespace(ground=ground&~solid,obstacles=solid,water=empty,forbidden=empty,occluded=empty)
    with pytest.raises(ProjectionError,match='building_entrance_unobserved'):
        building_entry_region(projection,observed,None,world((550,300)),dict(doorways=[]))


def test_front_view_camera_receipt_does_not_claim_calibration(tmp_path,monkeypatch):
    from bot.control.overhead_camera import FacadeObservation
    from bot.perception.travel_channel import CameraSample,CameraScene,CameraResult
    _,ctx,nav,events,_,_=setup(tmp_path,monkeypatch)
    camera=CameraSample(32,2,CameraScene.GROUND,CameraScene.GROUND,1,CameraResult.DISTANCE_VERIFIED,False,False)
    nav=replace(nav,camera=camera,camera_restore_available=True)
    view=FacadeObservation()
    assert view.step(ctx,1,nav)==TravelCommand()
    assert view.step(ctx,1.6,nav)==TravelCommand()
    assert view.step(ctx,1.7,replace(nav,sequence=2)) is None
    assert events[-1][0]=='quest_building_facade_camera' and not events[-1][1]['projection_calibrated']


def test_frontal_measurement_is_explicit_not_a_relaxed_default():
    from bot.perception.automatic_ground import sample
    from bot.perception.ground_projection import ProjectionError
    from test_ground_projection import fixture
    frame,*_=fixture();frame=replace(frame,camera_signature=(2,32,7))
    rgb=np.zeros((600,800,3),np.uint8)
    observed=SimpleNamespace(ground=np.ones((600,800),bool),semantic_confidence=np.ones((600,800)))
    with pytest.raises(ProjectionError,match='native_observation_invalid'):sample(frame,rgb,observed)
    assert sample(frame,rgb,observed,camera_views=(2,5)).frame.camera_signature[0]==2


def test_frontal_orbit_uses_turn_feedback_not_overhead_only_f4(tmp_path,monkeypatch):
    from test_segmented_runtime import controller
    from test_building_entry_recovery import facade_nav
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    nav=facade_nav(nav,view=2)
    obj.goal_mode='building_entry'
    obj.calibration_action=dict(kind='calibration_orbit',yaw_pixels=110)
    command=obj.step(ctx,1,nav,0)
    assert command.tap in {'left','right'} and not command.keys and obj.orbit.keyboard
