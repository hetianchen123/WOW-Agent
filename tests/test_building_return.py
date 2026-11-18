"""Dead-end rooms return through fresh geometry and native crossing receipts.

All geometry/inputs below are simulated. They do not certify model recognition
of a real doorway or a successful in-game return.
"""
from dataclasses import replace
import math
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.control.building_memory import BuildingRouteMemory, PassageLook
from bot.control.quest_travel import TravelCommand
from bot.control.projected_route import plan_segmented_approach
from bot.decision import building_navigation as planning
from bot.perception.ground_projection import GroundProjection, ProjectionError
from bot.perception.travel_channel import TravelFlags, TaskFlags
from test_building_navigation import scene
from test_ground_projection import fixture
from test_segmented_runtime import controller, ready


def receipt(start=(.5,.5),end=(.5005,.5),frame='forward'):
    return dict(threshold_position=start,position=end,height=0,source_frame=frame,
                before_sequence=1,after_sequence=2,basis='measured_door_floor_and_native_movement')


def test_room_history_is_scoped_by_crossings_even_with_same_height_and_nearby_xy():
    memory=BuildingRouteMemory((1000,1000))
    memory.visited.append((.5,.5));memory.blocked.append((.501,.5))
    first=memory.crossed(receipt())
    assert first['from_area']==0 and first['to_area']==1 and first['level_id'] is None
    assert not memory.blocked and memory.return_edge()['passage_id']==0
    memory.visited.append((.5009,.5));memory.blocked.append((.502,.5))
    reverse=receipt((.5006,.5),(.4999,.5),'reverse')
    result=memory.crossed(reverse,return_id=0)
    assert result['returning'] and memory.current==0
    assert memory.blocked==[(.501,.5)] and (.5009,.5) not in memory.visited
    assert memory.areas[1]['exhausted'] and memory.passages[0]['returned']
    # Another passage at nearly the same XY and identical native height still
    # creates a different area. No XY/height heuristic joins it to area 1.
    result=memory.crossed(receipt((.5,.5),(.5,.5005),'branch'))
    assert result['to_area']==2 and memory.current==2
    assert len(memory.incident())==1 and not memory.blocked
    assert all(p['level_id'] is None for p in memory.passages)


@pytest.mark.parametrize('fault',['wrong_edge','same_side','no_movement','old_sequence','no_basis'])
def test_invalid_return_does_not_change_areas_or_mark_a_branch_exhausted(fault):
    memory=BuildingRouteMemory((1000,1000));memory.crossed(receipt())
    reverse=receipt((.5006,.5),(.4999,.5),'reverse');edge=0
    if fault=='wrong_edge':edge=8
    if fault=='same_side':reverse['position']=(.501,.5)
    if fault=='no_movement':reverse['position']=reverse['threshold_position']
    if fault=='old_sequence':reverse['after_sequence']=1
    if fault=='no_basis':reverse['basis']='outdoors_changed'
    with pytest.raises(ValueError):memory.crossed(reverse,return_id=edge)
    assert memory.current==1 and not memory.areas[1]['exhausted'] and not memory.passages[0]['returned']


def test_return_is_to_parent_only_and_snapshot_cannot_mutate_live_history():
    memory=BuildingRouteMemory((1000,1000));memory.crossed(receipt())
    memory.crossed(receipt((.501,.5),(.5015,.5),'second'))
    assert memory.return_edge()['passage_id']==1
    snapshot=memory.snapshot();snapshot['return_edge']['position']=(.9,.9)
    assert memory.return_edge()['position']==(.5015,.5)
    with pytest.raises(ValueError,match='identity'):memory.crossed(receipt(),return_id=0)


def return_scene(monkeypatch):
    frame,training,validation,world=fixture();projection=GroundProjection(frame,training,validation)
    observed,description=scene('room')
    door=dict(box=[365,180,435,300],threshold_box=[380,282,420,306])
    description['doorways']=[door]
    memory=BuildingRouteMemory(frame.world_size)
    memory.crossed(dict(receipt(world((400,270)),world((400,295))),height=frame.height))
    # Exhaustion is supplied separately; the actual new doorway geometry and
    # collision-aware planning remain real in these tests.
    def exhausted(*_,**__):raise ProjectionError('building_no_reachable_observation_region')
    def disconnected(*_,**__):raise ProjectionError('building_task_region_disconnected')
    monkeypatch.setattr(planning,'local_observation_region',exhausted)
    monkeypatch.setattr(planning,'_connected_search',disconnected)
    return frame,projection,world,observed,description,memory


def test_dead_end_reobserves_its_parent_exit_and_plans_through_visible_free_ground(monkeypatch):
    frame,projection,world,observed,description,memory=return_scene(monkeypatch)
    with pytest.raises(ProjectionError,match='no_reachable'):
        planning._building_navigation_region(projection,observed,None,(.9,.9),description,
            searching=True,passages=memory.incident())
    region,goal=planning.building_navigation_region(projection,observed,None,(.9,.9),description,
        searching=True,passages=memory.incident(),navigation_context=memory.snapshot())
    assert goal['return_passage_id']==0 and goal['from_area']==1 and goal['to_area']==0
    assert goal['basis']=='door_threshold' and goal['level_id'] is None
    result=plan_segmented_approach(projection,observed,region,frame,now=11,
                                  frame_id=frame.frame_id,image_sha256=frame.image_sha256)
    path=result['approach']['route']['points'];raster=np.zeros(region.shape,np.uint8)
    for a,b in zip(path,path[1:]):cv2.line(raster,tuple(map(round,a)),tuple(map(round,b)),1,1)
    assert not np.any(raster.astype(bool)&(observed.obstacles|observed.forbidden|observed.water))
    assert not result['executable'] and not result['arrival_confirmed']


@pytest.mark.parametrize('fault',['closed','other_door','two_doors','new_wall'])
def test_history_never_overrides_changed_or_ambiguous_current_exit(monkeypatch,fault):
    _,projection,_,observed,description,memory=return_scene(monkeypatch)
    if fault=='closed':description['doorways']=[]
    if fault=='other_door':description['doorways'][0]['threshold_box']=[510,380,550,415]
    if fault=='two_doors':description['doorways'].append(dict(description['doorways'][0]))
    if fault=='new_wall':
        observed.obstacles[260:300,:]=True;observed.ground[260:300,:]=False
    with pytest.raises(ProjectionError):
        planning.building_navigation_region(projection,observed,None,(.9,.9),description,
            returning=0,navigation_context=memory.snapshot())


def test_return_crossing_selects_previous_side_not_middle_of_the_door(monkeypatch):
    _,projection,world,observed,description,memory=return_scene(monkeypatch)
    region,goal=planning.building_navigation_region(projection,observed,None,(.9,.9),description,
        crossing=True,crossing_origin=world((400,295)),returning=0,navigation_context=memory.snapshot())
    ys,xs=np.nonzero(region)
    edge=memory.return_edge()
    assert len(xs) and goal['basis']=='door_floor_crossing'
    for x,y in zip(xs[::20],ys[::20]):
        p=world((x,y))
        assert memory.distance(p,edge['threshold_position'])+.15<=memory.distance(p,edge['position'])+1e-7


def test_new_local_door_is_preferred_over_backtracking(monkeypatch):
    _,projection,_,observed,description,memory=return_scene(monkeypatch)
    description['doorways'].append(dict(box=[480,270,550,390],threshold_box=[490,365,535,390]))
    region,goal=planning.building_navigation_region(projection,observed,None,(.9,.9),description,
        searching=True,passages=memory.incident(),navigation_context=memory.snapshot())
    assert region.any() and 'return_passage_id' not in goal


@pytest.mark.parametrize('fault',['no_turn','pose','camera','budget'])
def test_return_observation_turn_is_bounded_and_never_translates(tmp_path,monkeypatch,fault):
    _,_,nav,_=controller(tmp_path,monkeypatch)
    edge=receipt((nav.position[0]-.001,nav.position[1]),nav.position)
    look=PassageLook(1,nav,edge,'ccw_west')
    cmd=look.step(1,nav,1.5*math.pi)
    assert cmd.tap in {'left','right'} and cmd.precise and cmd.hold_s==.06 and not cmd.keys
    changed=replace(nav,sequence=nav.sequence+1)
    now=1.5;angle=1.5*math.pi
    if fault=='pose':changed=replace(changed,position=(nav.position[0]+.001,nav.position[1]))
    if fault=='camera':changed=replace(changed,camera_epoch=nav.camera_epoch+1)
    if fault=='budget':now=13
    cmd=look.step(now,changed,angle)
    assert cmd.outcome and not cmd.tap and not cmd.keys


def test_return_look_needs_fresh_turn_receipt_and_settled_view(tmp_path,monkeypatch):
    _,_,nav,_=controller(tmp_path,monkeypatch)
    edge=receipt((nav.position[0]-.001,nav.position[1]),nav.position)
    look=PassageLook(1,nav,edge,'ccw_west')
    assert look.step(1,nav,1.5*math.pi).tap
    assert look.step(1.5,nav,look.desired)==TravelCommand()  # old sequence
    nav=replace(nav,sequence=nav.sequence+1)
    assert look.step(1.6,nav,look.desired)==TravelCommand()
    assert look.step(2.2,nav,look.desired)==TravelCommand()
    assert look.step(2.3,replace(nav,sequence=nav.sequence+1),look.desired) is None


def test_last_return_turn_shortens_instead_of_oscillating(tmp_path,monkeypatch):
    _,_,nav,_=controller(tmp_path,monkeypatch)
    edge=receipt((nav.position[0]-.001,nav.position[1]),nav.position)
    look=PassageLook(1,nav,edge,'ccw_west')
    heading=look.desired-math.radians(7)
    command=look.step(1,nav,heading)
    assert command.tap=='left' and .025<=command.hold_s<.06
    heading+=math.radians(240)*command.hold_s
    nav=replace(nav,sequence=nav.sequence+1)
    assert look.step(1.5,nav,heading)==TravelCommand()
    assert look.step(2.1,replace(nav,sequence=nav.sequence+1),heading) is None


@pytest.mark.parametrize('fault',['combat','falling','goal_changed','stale_heading','camera_paused','stop'])
def test_return_look_preserves_normal_interruptions(tmp_path,monkeypatch,fault):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);obj.goal_mode='building_search'
    edge=receipt((nav.position[0]-.001,nav.position[1]),nav.position)
    if fault=='camera_paused':nav.camera.paused=True
    obj.return_look=PassageLook(1,nav,edge,'ccw_west')
    if fault=='combat':nav=replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT)
    if fault=='falling':nav=replace(nav,flags=nav.flags|TravelFlags.FALLING)
    if fault=='goal_changed':nav=replace(nav,quest_id=999)
    if fault=='stale_heading':ctx.heading_age=lambda:2
    if fault=='stop':obj.close()
    cmd=obj.step(ctx,1.1,nav,1.5*math.pi)
    assert not cmd.tap and not cmd.keys
    assert cmd.outcome or fault=='stale_heading'


def test_worker_keeps_return_identity_when_door_reobservation_is_needed(monkeypatch):
    from experiments.route_models import segmentation_worker as worker
    frame,*_=fixture()
    def missing(*_,**__):raise ProjectionError('building_return_entrance_unobserved')
    monkeypatch.setattr(worker,'automatic_job',missing)
    monkeypatch.setattr(worker,'building_ground_measurement',lambda *_,**__:dict(texture_features=500))
    memory=BuildingRouteMemory(frame.world_size);memory.crossed(receipt())
    result=worker.building_job(SimpleNamespace(base=None),frame,None,None,
        dict(quest_id=1,phase='READY',building_scene={'surface':'unknown'},navigation_context=memory.snapshot()))
    assert result['kind']=='building_reobserve' and result['return_passage_id']==0
    assert result['requires_fresh_feedback'] and not result['executable']


def test_changed_passage_cannot_accept_an_old_return_plan(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode='building_search'
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result['building_scene']=dict(schema=1,executable=False,surface='unknown',doorways=[{}])
    result['automatic_goal']=dict(goal_kind='building_entry_region',basis='door_threshold',
                                  return_passage_id=0,from_area=1,to_area=0)
    command=obj.step(ctx,1.2,nav,heading)
    assert command.outcome=='building_return_identity_changed' and not command.tap


def simulate_branch_return(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    events=[];ctx.log_event=lambda kind,data:events.append((kind,data))
    modes=[];handled=set();now=1.;heading=1.5*math.pi;moves=0;deadline=obj.deadline
    # Model observations are simulated here; geometric return routes are checked
    # above with the actual planner and obstacle masks.
    stages=iter(['enter','cross','dead_end','return_entry','return_cross','branch','branch_cross'])
    for _ in range(500):
        nav=replace(nav,sequence=(nav.sequence+1)%128)
        if obj.request and id(obj.request) not in handled:
            request=obj.request;handled.add(id(request));stage=next(stages)
            modes.append((stage,request.job['navigation_context']['current_area']))
            result=request.result
            result['building_scene']=dict(schema=1,executable=False,surface='unknown',doorways=[{}])
            if stage=='dead_end':
                result.update(kind='building_reobserve',reason='building_return_entrance_unobserved',return_passage_id=0)
            else:
                basis='door_floor_crossing' if 'cross' in stage else 'door_threshold'
                result['automatic_goal']=dict(goal_kind='building_entry_region',basis=basis)
                origin=request.frame.player_position
                target=[origin[0]+.0005,origin[1]]
                if stage.startswith('return_'):
                    target=[origin[0]-.0005,origin[1]]
                    result['automatic_goal'].update(return_passage_id=0,from_area=1,to_area=0)
                if stage.startswith('branch'):target=[origin[0],origin[1]+.0005]
                result['next_segment_world']=[list(origin),target];result['search_goal_world']=target
        during_look=obj.return_look is not None
        command=obj.step(ctx,now,nav,heading)
        assert command.outcome is None
        if during_look:assert command.tap not in {'w','s','q','e'}
        if command.tap in {'left','right'}:
            heading=(heading+(.15 if command.tap=='left' else -.15))%math.tau
        if command.tap=='w':
            moves+=1;nav=replace(nav,position=tuple(obj.pulse[3]))
        if moves==6 and obj.building_memory.current==2:break
        now+=.1
    assert moves==6 and obj.building_memory.current==2
    assert modes==[('enter',0),('cross',0),('dead_end',1),('return_entry',1),('return_cross',1),('branch',0),('branch_cross',0)]
    transitions=[d for kind,d in events if kind=='quest_building_passage_crossed']
    assert [(d['from_area'],d['to_area']) for d in transitions]==[(0,1),(1,0),(0,2)]
    assert obj.passages[0]['returned'] and len(obj.passages)==2
    assert all(d['level_id'] is None and not d['task_arrival_verified'] for d in transitions)
    assert obj.deadline==deadline and obj.calls==7 and obj.pulses==6
    assert obj.total==pytest.approx(3.)
    native=replace(nav,sequence=(nav.sequence+1)%128,
        task=TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN|TaskFlags.NPC_WITHIN_5_YARDS)
    assert obj.step(ctx,now+.1,native,heading).outcome=='building_entered'
    return dict(source='synthetic_control_feedback',game_inputs_sent=False,live_passed=False,
        elapsed_simulated_s=round(now-1,2),movement_pulses=obj.pulses,model_observations=obj.calls,
        moved_simulated_yards=obj.total,transitions=transitions,passages=obj.passages,events=events,
        area_count=len(obj.building_memory.areas),final_area=obj.building_memory.current)


def test_controller_returns_from_dead_end_then_explores_another_branch(tmp_path,monkeypatch):
    simulate_branch_return(tmp_path,monkeypatch)
