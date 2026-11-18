"""Building geometry simulations and historical screenshot regressions.

Synthetic masks test routing contracts; they are not a model-recognition score.
"""
from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.perception.building_layout import building_layout, flat_navigation_masks
from bot.perception.building_scene import describe_building_scene
from bot.perception.ground_projection import GroundProjection, ProjectionError
from bot.perception.segmentation import fuse_masks
from bot.decision.building_navigation import building_navigation_region, local_observation_region
from bot.control.projected_route import plan_segmented_approach
from bot.perception.travel_channel import TravelFlags
from test_ground_projection import fixture
from test_segmented_runtime import controller, ready


NAMES = {0:'unknown',1:'floor',2:'wall',3:'road',4:'stairs',5:'railing',6:'ceiling',
         7:'table',8:'water',9:'windowpane',10:'door'}


def scene(kind):
    labels=np.zeros((600,800),np.uint8)
    labels[145:455,225:575]=2
    if kind in {'room','indoor_door','attic','stairs'}:
        labels[180:440,245:555]=1
    if kind=='corridor':
        labels[265:435,350:445]=1
        labels[345:435,350:550]=1
    if kind=='courtyard':
        labels[180:440,245:555]=3
        labels[325:365,375:430]=8
    if kind=='room':
        labels[325:385,430:485]=7
    if kind=='attic':
        labels[145:270,225:575]=6
        labels[180:385,465:480]=5
    if kind=='stairs':
        labels[180:280,360:445]=4
        labels[180:280,345:360]=5
        labels[180:280,445:460]=5
    if kind=='gate':
        labels[180:440,245:555]=3
        labels[345:365,245:555]=2
        labels[345:365,380:430]=3
    if kind=='indoor_door':
        labels[345:365,245:555]=2
        labels[345:365,370:430]=1
    confidence=np.ones_like(labels,float)*.98
    ground,solid,unknown=fuse_masks(labels,confidence,np.ones_like(labels,float),NAMES,[])
    result=SimpleNamespace(semantic_labels=labels,semantic_confidence=confidence,ground=ground,
        obstacles=solid,unknown=unknown,occluded=np.zeros_like(ground),water=labels==8,
        forbidden=np.zeros_like(ground),instances=[])
    layout=building_layout(result,NAMES)
    result=flat_navigation_masks(result,NAMES)
    return result,dict(schema=1,surface='unknown',executable=False,doorways=[],layout=layout)


@pytest.mark.parametrize('kind',['gate','room','corridor','indoor_door','courtyard','attic','stairs'])
def test_each_scenario_yields_a_measured_collision_free_observation_step(kind):
    observed,description=scene(kind)
    frame,training,validation,_=fixture()
    projection=GroundProjection(frame,training,validation)
    region,goal=building_navigation_region(projection,observed,None,(.1,.1),description)
    route=plan_segmented_approach(projection,observed,region,frame,now=11,
                                 frame_id=frame.frame_id,image_sha256=frame.image_sha256)
    points=route['approach']['route']['points']
    raster=np.zeros(observed.ground.shape,np.uint8)
    for a,b in zip(points,points[1:]):
        cv2.line(raster,tuple(round(v) for v in a),tuple(round(v) for v in b),1,1)
    assert not np.any(raster.astype(bool) & (observed.obstacles|observed.water|observed.forbidden))
    assert goal['basis']=='local_observation_frontier' and not goal['entrance_verified']
    assert goal['level_id'] is None and route['movement_budget_yards']<=2
    assert not route['executable'] and not route['arrival_confirmed']
    assert np.linalg.norm(np.array(points[-1])-np.array(points[0]))>25


def test_stairs_and_railings_do_not_inherit_adjacent_flat_floor_calibration():
    observed,description=scene('stairs')
    assert description['layout']['connectors']
    c=description['layout']['connectors'][0]
    assert c['from_level'] is None and c['to_level'] is None and c['direction']=='unknown'
    assert c['requires_separate_surface_measurement'] and not c['passage_verified']
    assert observed.forbidden[220,400] and not observed.ground[220,400]
    assert observed.obstacles[220,450] and not observed.occluded[220,450]


TARGETS={'gate':(400,410),'room':(525,415),'corridor':(520,390),
         'indoor_door':(400,410),'courtyard':(400,415),'attic':(530,300)}


@pytest.mark.parametrize('kind',list(TARGETS))
def test_full_local_routes_use_real_openings_and_turn_around_obstacles(kind):
    from bot.decision.image_navigation import ImageNavigator
    observed,_=scene(kind)
    nav=ImageNavigator.from_masks(kind,observed.ground,observed.obstacles,
        ~observed.ground|observed.forbidden|observed.water,source='synthetic_regression')
    route=nav.plan((400,300),TARGETS[kind])
    nav.validate(route.points)
    assert route.points[-1]==TARGETS[kind]
    if kind in {'room','corridor','courtyard','attic'}:
        assert not nav.line_clear((400,300),TARGETS[kind])
        assert len(route.points)>2


def test_upstairs_goal_cannot_be_reached_with_a_flat_floor_route():
    from bot.decision.image_navigation import ImageNavigator,ImageRouteError
    observed,_=scene('stairs')
    nav=ImageNavigator.from_masks('stairs',observed.ground,observed.obstacles,
        ~observed.ground|observed.forbidden,source='synthetic_regression')
    with pytest.raises(ImageRouteError):nav.plan((400,300),(400,220))


@pytest.mark.parametrize('label',['railing','table','bed ','windowpane','cabinet','closed door'])
def test_indoor_obstacles_override_misclassified_floor(label):
    labels=np.ones((80,80),np.uint8)
    instance=dict(label=label.strip(),box=[30,10,50,70],detection_score=.8,mask_score=.98,
                  mask=np.zeros_like(labels,bool))
    instance['mask'][10:70,30:50]=True
    if label=='closed door':
        labels[10:70,30:50]=2;names={1:'floor',2:'door'}
    else:
        labels[10:70,30:50]=2;names={1:'floor',2:label}
    ground,solid,_=fuse_masks(labels,np.ones_like(labels,float),np.ones_like(labels,float),names,[instance])
    assert solid[40,40] and not ground[40,40] and ground[40,15]


def test_door_detections_do_not_override_conflicting_closed_door_evidence():
    labels=np.zeros((120,120),np.uint8)
    labels[20:80,30:90]=2;labels[30:80,40:80]=10;labels[80:100,40:80]=1
    items=[dict(label=label,box=[40,30,80,80],detection_score=.9,mask_score=.98)
           for label in ('open doorway','closed door')]
    observed=SimpleNamespace(semantic_labels=labels,semantic_confidence=np.ones_like(labels,float),
        water=np.zeros_like(labels,bool),instances=items)
    description=describe_building_scene(observed,NAMES,camera_view=2)
    assert not description['doorways']
    assert description['rejected_doorways'][0]['reason']=='closed_door'
    assert description['layout']['portals'][0]['kind']=='closed'


def test_closed_word_over_real_stairway_does_not_erase_the_opening():
    from pathlib import Path
    import json
    from bot.perception.building_layout import door_state
    path=Path(__file__).parent/'fixtures/building-door-state-20261118'
    meta=json.loads(path.with_suffix('.json').read_text())
    with np.load(path.with_suffix('.npz')) as z:
        labels=z['labels'];confidence=z['confidence']
        instances=[dict(v,mask=z['mask_'+str(i)]) for i,v in enumerate(meta['instances'])]
    observed=SimpleNamespace(semantic_labels=labels,semantic_confidence=confidence,
                             water=np.zeros_like(labels,bool),instances=instances)
    result=describe_building_scene(observed,meta['names'],camera_view=4)
    wrong=next(i for i in instances if i['label']=='closed door')
    assert door_state(wrong,labels,confidence,meta['names'])=='opening'
    assert result['doorways'] and not any(p['kind']=='closed' for p in result['layout']['portals'])
    assert all(not d['passage_verified'] for d in result['doorways'])


def test_unsupported_closed_label_cannot_seal_visible_stairs():
    labels=np.ones((100,100),np.uint8);labels[20:80,30:70]=2
    mask=np.zeros_like(labels,bool);mask[20:80,30:70]=True
    ground,solid,unknown=fuse_masks(labels,np.ones_like(labels,float),np.ones_like(labels,float),
        {1:'floor',2:'stairs'},[dict(label='closed door',box=[30,20,70,80],mask=mask,mask_score=.98)])
    assert not solid[40,40] and unknown[40,40]  # Stairs still require their own surface.
    assert ground[90,50]


def test_hidden_floor_is_not_filled_and_small_local_floor_is_retained():
    observed,_=scene('attic')
    observed.semantic_labels[:]=0
    observed.semantic_labels[300:320,400:420]=1  # < .1% of this screenshot.
    layout=building_layout(observed,NAMES)
    assert len(layout['floor_regions'])==1
    assert layout['floor_regions'][0]['pixels']==400
    assert layout['level_id'] is None and layout['room_id'] is None


def test_new_observation_goal_does_not_follow_an_unseen_quest_pin():
    observed,description=scene('courtyard')
    frame,training,validation,_=fixture();p=GroundProjection(frame,training,validation)
    a,_=building_navigation_region(p,observed,None,(0,0),description)
    b,_=building_navigation_region(p,observed,None,(1,1),description)
    assert np.array_equal(a,b)


def test_visited_observation_locations_are_not_selected_repeatedly():
    observed,description=scene('corridor')
    frame,training,validation,_=fixture();p=GroundProjection(frame,training,validation)
    first,_=local_observation_region(p,observed,description)
    y,x=np.argwhere(first)[len(np.argwhere(first))//2]
    delta=p.delta((int(x),int(y)))
    point=tuple(v+d/s for v,d,s in zip(frame.player_position,delta,frame.world_size))
    second,_=local_observation_region(p,observed,description,visited=[point])
    assert not np.any(first&second)


def test_unknown_or_sealed_floor_cannot_authorize_an_observation_walk():
    observed,description=scene('room')
    frame,training,validation,_=fixture();p=GroundProjection(frame,training,validation)
    observed.ground[:]=False
    with pytest.raises(ProjectionError,match='no_reachable'):
        local_observation_region(p,observed,description)


def test_worker_local_frontier_contract_reaches_existing_task_controller(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result['building_scene']=dict(schema=1,surface='unknown',doorways=[],executable=False,
        layout=dict(schema=1,coordinate_space='current_image',size=list(requests[0].frame.size),
                    floor_regions=[dict(box=[0,0,100,100],pixels=10000)]))
    result['automatic_goal']=dict(goal_kind='building_entry_region',basis='local_observation_frontier')
    assert obj.step(ctx,1.2,nav,heading).outcome is None
    command=obj.step(ctx,1.3,nav,heading)
    assert command.tap=='w' and command.precise
    assert obj.plan is not None and not obj.closed


@pytest.mark.parametrize('outdoors',[False,True])
def test_measured_portal_crossing_handles_both_indoor_doors_and_courtyard_gates(tmp_path,monkeypatch,outdoors):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_cross';obj.crossing_baseline=0;obj.crossing_origin=nav.position
    nav=replace(nav,flags=nav.flags|TravelFlags.OUTDOORS if outdoors else nav.flags&~TravelFlags.OUTDOORS)
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result['building_scene']=dict(schema=1,executable=False,surface='unknown',doorways=[{}])
    result['automatic_goal']=dict(goal_kind='building_entry_region',basis='door_floor_crossing')
    result['search_goal_world']=result['next_segment_world'][-1]
    assert obj.step(ctx,1.2,nav,heading).outcome is None
    assert obj.step(ctx,1.3,nav,heading).tap=='w'
    assert obj.step(ctx,1.4,nav,heading).outcome is None
    fresh=replace(nav,sequence=3,position=(nav.position[0]+.00045,nav.position[1]))
    assert obj.step(ctx,1.9,fresh,heading).outcome is None
    assert obj.crossing_confirmed['basis']=='measured_door_floor_and_native_movement'
    deadline=obj.deadline
    assert obj.step(ctx,2,replace(fresh,sequence=4),heading).outcome is None
    assert not obj.closed and obj.goal_mode=='building_search' and len(obj.passages)==1
    assert obj.deadline==deadline and obj.total==pytest.approx(.45)
    assert obj.plan is None and obj.anchor is None


def test_closer_floor_observation_receipt_does_not_claim_passage_or_skip_door(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_cross';obj.crossing_baseline=0;obj.crossing_origin=nav.position
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result['building_scene']=dict(schema=1,executable=False,surface='unknown',doorways=[{}])
    result['automatic_goal']=dict(goal_kind='building_entry_region',basis='door_floor_observation',
                                  requires_reobservation=True,entrance_verified=False)
    result['search_goal_world']=result['next_segment_world'][-1]
    assert obj.step(ctx,1.2,nav,heading).outcome is None
    assert obj.step(ctx,1.3,nav,heading).tap=='w'
    fresh=replace(nav,sequence=3,position=(nav.position[0]+.00045,nav.position[1]))
    assert obj.step(ctx,1.9,fresh,heading).outcome is None
    assert obj.crossing_confirmed is None and not obj.passages and not obj.closed
    assert obj.goal_mode=='building_cross' and obj.plan is None and obj.anchor is None
    assert obj.visited and obj.total==pytest.approx(.45)


def test_detection_foreground_sill_is_not_a_target_beyond_the_door():
    from bot.decision.building_entry import building_entry_region
    from bot.perception.building_layout import doorway_floor_box
    frame,training,validation,_=fixture();projection=GroundProjection(frame,training,validation)
    ground=np.zeros((600,800),bool);ground[280:,220:580]=True
    empty=np.zeros_like(ground)
    observed=SimpleNamespace(ground=ground,obstacles=empty,water=empty,forbidden=empty,occluded=empty)
    door=dict(box=[350,150,450,300],threshold_box=[350,294,450,330])
    assert doorway_floor_box(door)[3]<280
    with pytest.raises(ProjectionError,match='door_floor_unobserved'):
        building_entry_region(projection,observed,None,None,dict(doorways=[door]),crossing=True)


def test_closer_observation_aims_at_visible_floor_instead_of_panel_box_edge():
    from bot.decision.building_entry import doorway_approach_distance
    ground=np.zeros((120,180),bool);ground[70:78,55:75]=True;ground[100:]=True
    wall=np.zeros_like(ground);wall[30:100,100:130]=True
    observed=SimpleNamespace(ground=ground,obstacles=wall,water=np.zeros_like(ground),forbidden=np.zeros_like(ground))
    door=dict(box=[30,10,150,100],threshold_box=[30,94,150,115])
    distance=doorway_approach_distance(observed,[door],crossing=True)
    assert distance[105,65]<distance[105,120]
    assert distance[75,65]==0 and distance[75,120]>0
    assert not observed.ground[75,120]  # The cue creates no passage through panel.


@pytest.mark.parametrize('fault',['wrong_size','old_layout','crossing'])
def test_local_frontier_cannot_override_a_mismatched_scene_or_crossing(fault,tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_cross' if fault=='crossing' else 'building_entry'
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    layout=dict(schema=1,coordinate_space='current_image',size=list(requests[0].frame.size),
                floor_regions=[dict(box=[0,0,100,100],pixels=10000)])
    if fault=='wrong_size':layout['size']=[800,600]
    if fault=='old_layout':layout.pop('coordinate_space')
    result['building_scene']=dict(schema=1,executable=False,surface='unknown',doorways=[],layout=layout)
    result['automatic_goal']=dict(goal_kind='building_entry_region',basis='local_observation_frontier')
    assert obj.step(ctx,1.2,nav,heading).outcome=='building_entrance_evidence_missing'
    assert obj.plan is None


@pytest.mark.parametrize('fault,reason',[
    ('heading','heading_stale'),('combat','character_busy'),('position','position_missing'),
    ('identity','goal_changed'),('bindings','movement_bindings_lost')])
def test_interrupt_records_the_actual_failed_guard(tmp_path,monkeypatch,fault,reason):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    events=[];ctx.log_event=lambda name,data:events.append((name,data))
    if fault=='heading':ctx.heading_age=lambda:.7
    if fault=='combat':nav=replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT)
    if fault=='position':nav=replace(nav,position=None)
    if fault=='identity':nav=replace(nav,objective_index=999)
    if fault=='bindings':nav=replace(nav,flags=nav.flags&~TravelFlags.KEYS_VERIFIED)
    result=obj.step(ctx,1,nav,0)
    if fault=='heading':
        assert not result.outcome and not result.keys and not result.tap
        result=obj.step(ctx,2.01,replace(nav,sequence=nav.sequence+1),0)
    assert result.outcome=='probe_interrupted' and not result.keys and not result.tap
    assert events[-1][0]=='quest_segmented_interrupted' and events[-1][1]['reason']==reason


def test_saved_building_image_keeps_entrance_and_local_surface_evidence():
    from test_building_entry_recovery import recorded
    meta,data,observed,_=recorded()
    result=describe_building_scene(observed,meta['names'],camera_view=2)
    assert result['doorways'] and result['layout']['portals']
    assert result['layout']['floor_regions'] or result['layout']['exterior_regions']
    assert not result['entrance_verified'] and not result['layout']['executable']
    adapted=flat_navigation_masks(observed,meta['names'])
    assert not (adapted.ground & adapted.forbidden).any()
    assert np.array_equal(observed.ground,data['ground'])  # Original evidence is retained.


@pytest.mark.parametrize('mode',['building_entry','building_search'])
def test_start_on_measured_threshold_changes_phase_without_moving(tmp_path,monkeypatch,mode):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode=mode
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result.update(building_scene=dict(schema=1,executable=False,surface='unknown',doorways=[{}]),
        automatic_goal=dict(goal_kind='building_entry_region',basis='door_threshold'),
        next_segment_world=[nav.position,nav.position],search_goal_world=nav.position,
        approach=dict(requires_probe=False),execution_window=dict(schema=1,requires_probe=False,
            cross_track_yards=.15,max_age_s=12,max_pulses=8))
    command=obj.step(ctx,1.2,nav,heading)
    assert not command.tap and not command.outcome and obj.goal_mode=='building_cross'
    assert obj.crossing_origin==nav.position and obj.crossing_confirmed is None
    assert obj.pulses==0 and not obj.passages and obj.window is None
    obj.step(ctx,1.3,nav,heading)
    obj.step(ctx,1.4,replace(nav,sequence=3),heading)
    assert requests[-1].job['crossing_origin']==nav.position
    assert requests[-1].frame.frame_id!=requests[0].frame.frame_id


def test_return_to_threshold_after_detour_does_not_count_as_crossing(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_cross'
    obj.crossing_origin=(nav.position[0]+.00048,nav.position[1])
    obj.crossing_baseline=0
    obj.total=1.7
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result.update(building_scene=dict(schema=1,executable=False,surface='unknown',doorways=[{}]),
        automatic_goal=dict(goal_kind='building_entry_region',basis='door_floor_crossing'),
        search_goal_world=result['next_segment_world'][-1])
    assert obj.step(ctx,1.2,nav,heading).outcome is None
    assert obj.step(ctx,1.3,nav,heading).tap=='w'
    assert obj.step(ctx,1.9,replace(nav,sequence=3,position=obj.crossing_origin),heading).outcome is None
    assert obj.total>2 and obj.crossing_confirmed is None and not obj.passages


def test_crossing_planner_selects_interior_floor_away_from_threshold_origin():
    from bot.decision.building_entry import building_entry_region
    frame,training,validation,world=fixture()
    projection=GroundProjection(frame,training,validation)
    observed,_=scene('room')
    door=dict(box=[350,180,450,340],threshold_box=[350,334,450,360])
    threshold=world((400,310))
    region,goal=building_entry_region(projection,observed,None,None,dict(doorways=[door]),
                                     crossing=True,crossing_origin=threshold)
    assert goal['basis']=='door_floor_crossing' and region.any()
    assert not region[300,400] and not region[310,400]
    delta=projection.metric_delta(threshold)
    for y,x in np.argwhere(region)[::7]:
        assert np.linalg.norm(projection.delta((int(x),int(y)))-delta)>=.45


def test_after_turn_only_door_at_measured_threshold_can_be_crossed():
    frame,train,check,world=fixture()
    projection=GroundProjection(frame,train,check)
    observed,description=scene('room')
    original=dict(box=[350,180,440,305],threshold_box=[350,299,440,315])
    other=dict(box=[270,180,325,315],threshold_box=[270,309,325,330])
    description['doorways']=[other,original]
    region,goal=building_navigation_region(projection,observed,None,None,description,
        crossing=True,crossing_origin=world((400,305)))
    yy,xx=np.nonzero(region)
    assert goal['basis']=='door_floor_crossing' and len(xx)>0 and xx.min()>=350
    for doors in ([other],[original,dict(original)]):
        region,goal=building_navigation_region(projection,observed,None,None,dict(description,doorways=doors),
            crossing=True,crossing_origin=world((400,305)))
        assert goal['basis']=='threshold_reobservation' and goal['requires_reobservation']
        assert not goal['entrance_verified']
        for y,x in np.argwhere(region):
            delta=projection.delta((int(x),int(y)))
            assert np.linalg.norm(delta-projection.metric_delta(goal['threshold_origin']))<=2


def test_visible_door_with_hidden_interior_uses_nearby_connected_space_to_look_again():
    frame,train,check,world=fixture()
    projection=GroundProjection(frame,train,check)
    observed,description=scene('room')
    # The player can move along the sill; the floor inside is obscured.
    observed.ground[:285]=False
    observed.obstacles[:285]=True
    description['doorways']=[dict(box=[365,160,435,293],threshold_box=[365,289,435,313])]
    origin=world((400,300))
    region,goal=building_navigation_region(projection,observed,None,None,description,
        crossing=True,crossing_origin=origin)
    assert goal['basis']=='threshold_reobservation' and not goal['entrance_verified']
    route=plan_segmented_approach(projection,observed,region,frame,now=11,
        frame_id=frame.frame_id,image_sha256=frame.image_sha256)
    assert route['movement_budget_yards']>0 and not route['arrival_confirmed']
    assert goal['threshold_origin']==origin


@pytest.mark.parametrize('stale_origin',[False,True])
def test_hidden_door_reposition_is_bound_to_the_recorded_threshold(tmp_path,monkeypatch,stale_origin):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    obj.goal_mode='building_cross';obj.crossing_origin=nav.position;obj.crossing_baseline=0.
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result.update(building_scene=dict(schema=1,executable=False,surface='unknown',doorways=[],
        layout=dict(schema=1,coordinate_space='current_image',size=list(requests[0].frame.size))),
        automatic_goal=dict(goal_kind='building_entry_region',basis='threshold_reobservation',
            threshold_origin=(0.,0.) if stale_origin else nav.position,requires_reobservation=True),
        search_goal_world=result['next_segment_world'][-1])
    command=obj.step(ctx,1.2,nav,heading)
    if stale_origin:
        assert command.outcome=='building_entrance_evidence_missing' and obj.plan is None
        return
    assert command.outcome is None and obj.step(ctx,1.3,nav,heading).tap=='w'
    assert obj.step(ctx,1.9,replace(nav,sequence=3,position=result['search_goal_world']),heading).outcome is None
    assert obj.goal_mode=='building_cross' and not obj.passages and obj.crossing_confirmed is None
