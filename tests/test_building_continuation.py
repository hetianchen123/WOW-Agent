"""Task ownership across rooms, native handoff, and current-frame planning.

These are simulated geometry/control receipts, not model or live-game claims.
"""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.building_access import building_target_receipt
from bot.control.quest_travel import TravelCommand
from bot.decision.building_navigation import building_navigation_region, _unvisited_portals
from bot.perception.building_scene import describe_building_scene
from bot.perception.observation import ObservationTracker
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import TravelFlags as F, TaskFlags, ObjectiveAction
from bot.perception.ground_projection import GroundProjection
from bot.perception.building_layout import flat_navigation_masks
from bot.perception.segmentation import fuse_masks
from test_building_access import setup,observe
from test_building_navigation import scene,NAMES
from test_ground_projection import fixture
from test_segmented_runtime import controller


def test_room_start_keeps_navigation_ownership_without_target_receipt(tmp_path,monkeypatch):
    obj,ctx,nav,events,_,run=setup(tmp_path,monkeypatch,'interior_floor')
    nav=replace(nav,flags=nav.flags&~F.OUTDOORS)
    observe(obj,ctx,nav)
    assert obj.state=='ground_travel' and obj.ground_only and obj.search_inside
    entry=SimpleNamespace(goal_mode='building_entry',step=lambda *_:TravelCommand(),close=lambda:None)
    obj.entry_factory=lambda *_:entry
    assert obj.step(ctx,1.8,replace(nav,sequence=4),0)==TravelCommand()
    assert entry.goal_mode=='building_search' and obj.state=='entry'
    run.building_access=obj
    assert run.building_pending


def test_losing_native_range_returns_to_local_planning(tmp_path,monkeypatch):
    obj,ctx,nav,events,_,_=setup(tmp_path,monkeypatch)
    observe(obj,ctx,nav);obj.ground_only=True
    confirmed=replace(nav,sequence=4,task=TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN|TaskFlags.NPC_WITHIN_5_YARDS)
    assert obj.step(ctx,1.8,confirmed,0) is None and obj.target_receipt
    entry=SimpleNamespace(goal_mode='building_entry',step=lambda *_:TravelCommand(),close=lambda:None)
    obj.entry_factory=lambda *_:entry
    cmd=obj.step(ctx,1.9,replace(nav,sequence=5),0)
    assert cmd==TravelCommand() and obj.state=='entry'
    assert entry.goal_mode=='building_search' and not obj.target_receipt


def test_combat_interrupt_discards_old_target_handoff(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch)
    observe(obj,ctx,nav);obj.ground_only=True
    confirmed=replace(nav,sequence=4,task=TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN|TaskFlags.NPC_WITHIN_5_YARDS)
    obj.step(ctx,1.8,confirmed,0)
    assert obj.target_receipt
    assert obj.step(ctx,1.9,replace(nav,sequence=5),0,in_combat=True) is None
    assert obj.target_receipt is None and obj.ground_only


@pytest.mark.parametrize('fault',[None,'stale_combat','old_navigation','wrong_target','dead','unsafe','range_unknown','los','unverified','wrong_objective'])
def test_combat_handoff_needs_current_task_identity_and_native_range(tmp_path,monkeypatch,fault):
    _,_,nav,_,_,_=setup(tmp_path,monkeypatch)
    nav=replace(nav,phase=QuestState.ACTIVE,objective_action=ObjectiveAction.COMBAT,objective_target=888,task=TaskFlags.PREPARED)
    combat=SimpleNamespace(target_id=888,target_dead=False,unsafe=False,key_verified=True,
        in_range=True,line_of_sight_blocked=False,state_epoch=3,sequence=1)
    if fault=='wrong_target':combat.target_id=999
    if fault=='dead':combat.target_dead=True
    if fault=='unsafe':combat.unsafe=True
    if fault=='range_unknown':combat.in_range=None
    if fault=='los':combat.line_of_sight_blocked=True
    if fault=='unverified':combat.key_verified=False
    if fault=='wrong_objective':nav=replace(nav,objective_action=ObjectiveAction.INTERACT)
    tracker=ObservationTracker();snap=tracker.snapshot(10,None,nav,combat)
    if fault=='stale_combat':snap=tracker.snapshot(11,None,replace(nav,sequence=nav.sequence+1),combat);nav=snap.navigation.value
    if fault=='old_navigation':nav=replace(nav,position=(.7,.7))
    receipt=building_target_receipt(nav,snap,11 if fault=='stale_combat' else 10.1)
    assert bool(receipt)==(fault is None)


def test_connected_room_search_routes_around_furniture_without_claiming_floor():
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    observed,description=scene('room')
    region,goal=building_navigation_region(projection,observed,np.zeros((600,800,3),np.uint8),
        world((525,415)),description,searching=True)
    assert region.any() and not (region&observed.obstacles).any()
    assert goal['basis']=='building_task_search_region' and goal['level_id'] is None
    assert not goal['interaction_verified'] and not goal['identity_confirmed']


def test_pin_behind_wall_uses_local_observation_not_direct_target():
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    observed,description=scene('room')
    observed.obstacles[:,440:460]=True;observed.ground[:,440:460]=False
    region,goal=building_navigation_region(projection,observed,np.zeros((600,800,3),np.uint8),
        world((550,310)),description,searching=True)
    assert region.any() and not region[:,440:].any()
    assert goal['basis']=='local_observation_frontier'


def test_passed_door_filter_uses_new_measurement_and_keeps_other_levels():
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    door=dict(box=[390,290,410,330],threshold_box=[390,290,410,310])
    receipt=dict(height=frame.height,threshold_position=world((400,300)),position=world((400,320)))
    assert not _unvisited_portals(projection,dict(doorways=[door]),[receipt])['doorways']
    assert _unvisited_portals(projection,dict(doorways=[door]),[dict(receipt,height=frame.height+4)])['doorways']


def test_wall_dominated_room_does_not_trigger_roof_classification():
    labels=np.full((600,800),2,np.uint8)
    labels[450:500,360:440]=1  # < 1% local floor; walls/ceiling dominate.
    observed=SimpleNamespace(semantic_labels=labels,semantic_confidence=np.ones_like(labels,float),
        instances=[],water=np.zeros_like(labels,bool))
    result=describe_building_scene(observed,{1:'floor',2:'wall'},camera_view=2,native_outdoors=False)
    assert result['support']['structure']>.9 and result['surface']=='unknown'
    assert result['layout']['floor_regions'] and not result['executable']


def test_two_doors_and_room_search_share_one_controller_and_end_on_native_range(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_entry'
    heading=1.5*np.pi;now=1.;next_seq=nav.sequence
    phases=iter(['door_threshold','door_floor_crossing','building_task_search_region',
                 'door_threshold','door_floor_crossing','building_task_search_region'])
    modes=[];handled=set();moves=0;initial_deadline=obj.deadline;initial_session=obj.session_id
    for _ in range(130):
        next_seq+=1;nav=replace(nav,sequence=next_seq)
        if obj.request and id(obj.request) not in handled:
            request=obj.request;handled.add(id(request));basis=next(phases)
            modes.append(request.job['goal_mode'])
            result=request.result
            result['automatic_goal']=dict(goal_kind='building_entry_region',basis=basis)
            origin=request.frame.player_position;target=[origin[0]+.0005,origin[1]]
            result['next_segment_world']=[list(origin),target];result['search_goal_world']=target
            result['building_scene']=dict(schema=1,executable=False,surface='unknown',doorways=[{}])
        cmd=obj.step(ctx,now,nav,heading)
        assert cmd.outcome is None
        if cmd.tap=='w':
            # Simulator provides a new measured position after the short input.
            moves+=1;nav=replace(nav,position=(nav.position[0]+.0005,nav.position[1]))
        if moves==6 and obj.pulse is None:break
        now+=.1
    assert moves==6 and len(obj.passages)==2 and not obj.closed
    assert modes==['building_entry','building_cross','building_search','building_search','building_cross','building_search']
    assert obj.deadline==initial_deadline and obj.session_id==initial_session
    assert obj.total==pytest.approx(3.) and obj.pulses==6
    confirmed=replace(nav,sequence=nav.sequence+1,
        task=TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN|TaskFlags.NPC_WITHIN_5_YARDS)
    assert obj.step(ctx,now+.1,confirmed,heading).outcome=='building_entered'
    assert obj.closed


def test_observation_retries_cannot_reset_no_progress_deadline(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);obj.goal_mode='building_search';obj.deadline=181
    obj.calls=10;obj.building_recoveries=2
    cmd=obj.step(ctx,61.01,replace(nav,sequence=nav.sequence+1),0)
    assert cmd.outcome=='building_no_progress_budget' and not cmd.keys and not cmd.tap


def test_combined_floor_road_label_does_not_erase_confirmed_ground():
    labels=np.ones((100,100),np.uint8);labels[:,60:70]=2
    item=dict(label='floor road',box=[0,0,100,100],mask=np.ones_like(labels,bool),mask_score=.95)
    ground,solid,_=fuse_masks(labels,np.ones_like(labels,float),np.ones_like(labels,float),
                              {1:'floor',2:'wall'},[item])
    assert ground[50,50] and solid[50,65] and not ground[50,65]
    item['label']='floor wooden fence'
    ground,_,_=fuse_masks(labels,np.ones_like(labels,float),np.ones_like(labels,float),
                         {1:'floor',2:'wall'},[item])
    assert not ground.any()  # A mixed physical/context label cannot open a route.


def test_detected_stairway_overrides_flat_floor_only_for_surface_separation():
    observed,_=scene('room');mask=np.zeros_like(observed.ground);mask[280:320,380:430]=True
    observed.instances=[dict(label='stairs',box=[380,280,430,320],mask=mask,
                             detection_score=.27,mask_score=.87)]
    assert observed.ground[300,400]
    adapted=flat_navigation_masks(observed,NAMES)
    assert adapted.forbidden[300,400] and not adapted.ground[300,400]
    assert observed.ground[300,400]  # Raw labels remain intact for diagnostics.


def test_water_dominated_image_keeps_local_dry_floor_available_for_measurement():
    from test_building_texture import recorded
    from bot.perception.automatic_ground import AutomaticGround
    from experiments.route_models.segmentation_worker import building_job
    meta,rgb,observed,frame=recorded();description=dict(meta['scene'],surface='water')
    result=building_job(AutomaticGround(camera_views=(2,4,5)),frame,rgb,observed,
        dict(meta['request'],building_scene=description))
    assert result['kind']=='calibration_orbit' and not result['executable']
    assert 'next_segment_world' not in result
