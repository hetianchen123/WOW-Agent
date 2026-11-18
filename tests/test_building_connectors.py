"""Separate stair surfaces and useful approach behavior, without fake climbing."""
from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.perception.building_layout import building_layout,flat_navigation_masks
from bot.perception.portal_review import merge_portal_detail
from bot.perception.segmentation import SegmentedMap,fuse_masks
from bot.perception.ground_projection import GroundProjection,ProjectionError
from bot.decision.building_navigation import building_navigation_region,connector_observation_region
from bot.control.projected_route import plan_segmented_approach
from test_ground_projection import fixture
from test_segmented_runtime import controller,ready

NAMES={0:'unknown',1:'floor',2:'wall',3:'stairs'}


def stair_scene(box=(360,190,445,250)):
    labels=np.zeros((600,800),np.uint8);labels[180:440,245:555]=1
    l,t,r,b=box;labels[t:b,l:r]=3
    confidence=np.full(labels.shape,.98)
    ground,solid,unknown=fuse_masks(labels,confidence,np.ones_like(confidence),NAMES,[])
    empty=np.zeros_like(ground)
    observed=SimpleNamespace(semantic_labels=labels,semantic_confidence=confidence,ground=ground,
        obstacles=solid,unknown=unknown,occluded=empty,water=empty,forbidden=empty,instances=[])
    scene=dict(schema=1,surface='unknown',executable=False,doorways=[],layout=building_layout(observed,NAMES))
    return flat_navigation_masks(observed,NAMES),scene


@pytest.mark.parametrize('box',[(360,190,445,250),(360,350,445,410),(270,265,330,335)])
def test_approach_uses_reachable_edge_for_up_down_or_side_view_without_projecting_steps(box):
    observed,scene=stair_scene(box)
    frame,training,validation,_=fixture();projection=GroundProjection(frame,training,validation)
    region,goal=building_navigation_region(projection,observed,None,(0,0),scene)
    assert goal['basis']=='connector_observation_frontier'
    assert goal['requires_separate_surface_measurement'] and not goal['entrance_verified']
    assert goal['level_id'] is None
    assert not np.any(region & (observed.forbidden|observed.obstacles))
    route=plan_segmented_approach(projection,observed,region,frame,now=11,
                                 frame_id=frame.frame_id,image_sha256=frame.image_sha256)
    raster=np.zeros(observed.ground.shape,np.uint8)
    points=route['approach']['route']['points']
    for a,b in zip(points,points[1:]):
        cv2.line(raster,tuple(round(v) for v in a),tuple(round(v) for v in b),1,1)
    assert not np.any(raster & (observed.forbidden|observed.obstacles))
    assert .5<=np.linalg.norm(projection.delta(points[-1]))<=2.01
    assert not route['executable'] and not route['arrival_confirmed']
    l,t,r,b=box
    def gap(p):
        x,y=p;return np.hypot(max(l-x,0,x-r),max(t-y,0,y-b))
    assert gap(points[-1])<gap(points[0])-8


def test_closed_wall_cannot_be_crossed_to_observe_a_staircase():
    observed,scene=stair_scene()
    observed.obstacles[270:280,245:555]=True;observed.ground[270:280,245:555]=False
    frame,training,validation,_=fixture();projection=GroundProjection(frame,training,validation)
    with pytest.raises(ProjectionError,match='connector_approach_unobserved'):
        connector_observation_region(projection,observed,scene)


@pytest.mark.parametrize('door_box',[[490,180,540,220],[440,180,540,260]])
def test_door_recovery_does_not_chase_unrelated_steps_elsewhere_in_room(door_box):
    observed,scene=stair_scene()
    scene['doorways']=[dict(box=door_box,threshold_box=[490,214,540,245])]
    frame,training,validation,_=fixture();projection=GroundProjection(frame,training,validation)
    with pytest.raises(ProjectionError,match='connector_approach_unobserved'):
        connector_observation_region(projection,observed,scene,door_related=True)


@pytest.mark.parametrize('reason',['already_close','outside_calibration','visited'])
def test_no_repeated_or_unmeasured_connector_step(reason):
    observed,scene=stair_scene((360,240,445,280) if reason=='already_close' else (360,190,445,250))
    frame,training,validation,_=fixture();projection=GroundProjection(frame,training,validation)
    visited=[]
    if reason=='outside_calibration':projection.hull=np.array([[350,290],[450,290],[450,400],[350,400]])
    if reason=='visited':
        first,_=connector_observation_region(projection,observed,scene)
        visited=[tuple(a+d/s for a,d,s in zip(frame.player_position,projection.delta((int(x),int(y))),frame.world_size))
                 for y,x in np.argwhere(first)[::5]]
    try:
        region,_=connector_observation_region(projection,observed,scene,visited=visited)
    except ProjectionError as exc:
        assert str(exc)=='building_connector_approach_unobserved'
    else:
        assert reason=='visited' and not np.any(region&first)


def local_stair_review(*,score=.6,mask_score=.97):
    shape=(120,180);empty=np.zeros(shape,bool);labels=np.ones(shape,np.uint8)
    before=SegmentedMap(~empty,empty,empty,[],labels,np.full(shape,.98),{}, {},empty,empty,empty,empty)
    crop=(40,20,140,100);local=np.zeros((80,100),bool);local[25:45,20:80]=True
    instance=dict(label='stairs',mask=local,box=[20,25,80,45],detection_score=score,mask_score=mask_score)
    view=(np.ones(local.shape,np.uint8),np.full(local.shape,.95),np.full(local.shape,.9))
    return before,merge_portal_detail(before,NAMES,crop,view,view,[instance])


def test_local_stair_mask_returns_to_original_coordinates_without_claiming_a_flat_surface():
    before,(after,details)=local_stair_review()
    assert details['transferred_stair_instances']==1
    stair=after.instances[0]
    assert stair['box']==[60,45,120,65]
    assert stair['mask'].sum()==1200 and stair['mask'][45:65,60:120].all()
    flat=flat_navigation_masks(after,NAMES)
    assert flat.forbidden[45:65,60:120].all() and not flat.ground[45:65,60:120].any()
    assert before.ground.all() and not before.instances
    assert flat.ground[5,5]  # Nearby measured floor remains available.


@pytest.mark.parametrize('scores',[(.24,.97),(.6,.84)])
def test_unreliable_crop_detection_does_not_invent_a_staircase(scores):
    _,(after,details)=local_stair_review(score=scores[0],mask_score=scores[1])
    assert not after.instances and details['transferred_stair_instances']==0


def connector_reply(request):
    connector=dict(kind='stairs',box=[30,10,70,40],requires_separate_surface_measurement=True)
    layout=dict(schema=1,coordinate_space='current_image',size=list(request.frame.size),
                floor_regions=[dict(box=[0,0,100,100],pixels=10000)],connectors=[connector])
    request.result['building_scene']=dict(schema=1,executable=False,surface='unknown',doorways=[],layout=layout)
    request.result['automatic_goal']=dict(goal_kind='building_entry_region',basis='connector_observation_frontier',
        connector_kind='stairs',connector_box=list(connector['box']),requires_separate_surface_measurement=True,
        requires_reobservation=True,entrance_verified=False)
    request.result['search_goal_world']=request.result['next_segment_world'][-1]


@pytest.mark.parametrize('mode',['building_entry','building_search','building_cross'])
def test_approach_feedback_reobserves_without_claiming_climb_or_portal_passage(mode,tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode=mode
    events=[];ctx.log_event=lambda name,data:events.append((name,data))
    nav,heading=ready(obj,ctx,nav);connector_reply(requests[0])
    if mode=='building_cross':requests[0].result['building_scene']['doorways']=[{}]
    assert obj.step(ctx,1.2,nav,heading).outcome is None
    assert obj.step(ctx,1.3,nav,heading).tap=='w'
    fresh=replace(nav,sequence=3,position=(nav.position[0]+.00045,nav.position[1]))
    assert obj.step(ctx,1.9,fresh,heading).outcome is None
    assert obj.goal_mode==mode and not obj.passages and obj.crossing_confirmed is None
    assert obj.plan is None and obj.anchor is None and obj.visited
    event=next(data for name,data in events if name=='quest_building_connector_approached')
    assert not event['surface_measured'] and not event['passage_verified']
    assert obj.step(ctx,2.3,replace(fresh,sequence=4),heading).tap is None
    assert obj.pending is not None


@pytest.mark.parametrize('fault',['no_connector','wrong_connector','wrong_size','no_reobserve'])
@pytest.mark.parametrize('door_visible',[False,True])
def test_unmatched_connector_result_never_reaches_movement(fault,door_visible,tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    nav,heading=ready(obj,ctx,nav);connector_reply(requests[0])
    scene=requests[0].result['building_scene'];goal=requests[0].result['automatic_goal']
    if door_visible:scene['doorways']=[{}]
    if fault=='no_connector':scene['layout']['connectors']=[]
    if fault=='wrong_connector':goal['connector_box']=[1,2,3,4]
    if fault=='wrong_size':scene['layout']['size']=[1,1]
    if fault=='no_reobserve':goal['requires_reobservation']=False
    command=obj.step(ctx,1.2,nav,heading)
    assert command.outcome=='building_entrance_evidence_missing' and not command.tap
    assert obj.plan is None
