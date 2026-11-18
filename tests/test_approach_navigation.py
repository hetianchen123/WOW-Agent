from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.decision.approach_navigation import ApproachNavigator, TraversalEvidence
from bot.decision.image_navigation import ImageRouteError
from bot.control.projected_route import interaction_region, plan_segmented_approach
from bot.perception.ground_projection import GroundProjection, ProjectionError
from bot.perception.segmentation import navigation_evidence
from test_ground_projection import fixture


def masks():
    ground = np.ones((180,240),bool)
    occluded = np.zeros_like(ground); occluded[55:125,15:75] = True
    ground[occluded] = False
    hard = np.zeros_like(ground); hard[45:135,110:125] = True
    goal = np.zeros_like(ground); goal[45:135,175:215] = True
    return ground,hard,occluded,goal


def navigator(ground,hard,occluded,**kw):
    return ApproachNavigator('f',ground,hard,occluded,source='synthetic_regression',
                             body_radius_px=3,uncertainty_px=1,**kw)


def test_hidden_avatar_can_plan_around_fence_to_region_without_visible_feet():
    ground,hard,occluded,goal=masks()
    nav=navigator(ground,hard,occluded)
    result=nav.plan((45,90),goal,anchor_confirmed=True)
    assert result.requires_probe and not result.arrival_confirmed and not result.executable
    assert result.route.points[0]==(45,90)
    end=result.route.points[-1]
    assert goal[end[1],end[0]] and end!=(195,90)
    assert all(nav.possible.line_clear(a,b) for a,b in zip(result.route.points,result.route.points[1:]))
    assert not ground[90,45]  # No free disk or persistent map mutation.


def test_known_longer_route_preferred_over_occluded_shortcut():
    ground=np.ones((180,240),bool); hard=np.zeros_like(ground)
    occluded=np.zeros_like(ground);occluded[55:125,100:125]=True
    ground[occluded]=False
    goal=np.zeros_like(ground);goal[80:100,195:215]=True
    result=navigator(ground,hard,occluded).plan((40,90),goal,anchor_confirmed=True)
    assert not result.requires_probe
    assert max(p[1] for p in result.route.points)>125 or min(p[1] for p in result.route.points)<55


def test_npc_body_does_not_have_to_be_ground_or_the_exact_goal():
    ground,hard,occluded,goal=masks()
    # NPC body is unknown, while nearby interaction candidates remain available.
    ground[75:105,188:202]=False
    result=navigator(ground,hard,occluded).plan((150,90),goal,anchor_confirmed=True)
    assert not result.arrival_confirmed and not result.requires_probe
    assert result.route.points[-1][0] < 188


@pytest.mark.parametrize('cause',['solid','forbidden','unobserved'])
def test_confirmed_position_does_not_erase_nearby_blockage(cause):
    ground,hard,occluded,goal=masks();forbidden=np.zeros_like(ground)
    if cause=='solid': hard[:,65:80]=True
    if cause=='forbidden': forbidden[:,65:80]=True
    if cause=='unobserved':ground[:,65:80]=False;occluded[:,65:80]=False
    with pytest.raises(ImageRouteError,match='supported_connection'):
        navigator(ground,hard,occluded,forbidden=forbidden).plan((45,90),goal,anchor_confirmed=True)


def test_missing_native_anchor_or_disabled_probe_does_not_authorize_motion():
    ground,hard,occluded,goal=masks();nav=navigator(ground,hard,occluded)
    with pytest.raises(ImageRouteError,match='anchor_unconfirmed'):
        nav.plan((45,90),goal,anchor_confirmed=False)
    with pytest.raises(ImageRouteError):
        nav.plan((45,90),goal,anchor_confirmed=True,allow_probe=False)
    with pytest.raises(ImageRouteError,match='cancelled'):
        nav.plan((45,90),goal,anchor_confirmed=True,cancelled=lambda:True)


def test_recent_traversal_resolves_occlusion_but_new_solid_wins():
    ground,hard,occluded,goal=masks()
    trace=TraversalEvidence('f',occluded,10,'synthetic_regression')
    result=navigator(ground,hard,occluded,traversed=trace,now=11).plan((45,90),goal,anchor_confirmed=True)
    assert not result.requires_probe
    hard[80:100,40:50]=True
    with pytest.raises(ImageRouteError,match='start_needs_other_evidence'):
        navigator(ground,hard,occluded,traversed=trace,now=11).plan((45,90),goal,anchor_confirmed=True)
    for evidence,now in [(replace(trace,frame_id='old'),11),(trace,20),(trace,9)]:
        with pytest.raises(ImageRouteError,match='trace_unregistered_or_stale'):
            navigator(ground,hard,occluded,traversed=evidence,now=now)


def test_water_and_unresolved_fence_not_reclassified_as_grass_occlusion():
    labels=np.ones((80,100),np.uint8);labels[10:30,10:30]=2
    confidence=np.full(labels.shape,.9);unknown=np.ones(labels.shape,bool)
    empty=np.zeros_like(unknown);ui=empty.copy();ui[50:60,50:60]=True
    item={'label':'wooden fence','box':[40,10,60,30],'mask':empty,'mask_score':.7}
    soft,water,forbidden=navigation_evidence(labels,confidence,{1:'grass',2:'water'},
        [item],empty,empty,unknown,ui)
    assert soft[40,40] and water[20,20] and not soft[20,20]
    assert forbidden[20,50] and forbidden[55,55] and not soft[20,50]
    assert not empty.any()


def test_measured_projection_exports_only_bounded_probe_and_keeps_full_candidate():
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    ground=np.ones((600,800),bool);occluded=np.zeros_like(ground)
    occluded[270:330,370:430]=True;ground[occluded]=False
    data=SimpleNamespace(ground=ground,obstacles=np.zeros_like(ground),occluded=occluded,
                         water=np.zeros_like(ground),forbidden=np.zeros_like(ground))
    goal=interaction_region(projection,world((520,300)),.6)
    result=plan_segmented_approach(projection,data,goal,frame,now=11,frame_id='f',image_sha256='a'*64)
    assert result['next_step']=='probe_then_reobserve'
    assert result['requires_fresh_feedback'] and not result['arrival_confirmed']
    points=result['next_segment_world'];scale=np.array(frame.world_size)
    assert sum(np.linalg.norm((np.array(b)-a)*scale) for a,b in zip(points,points[1:])) <= .750001
    assert result['world_route']['length_yards']>.75
    assert result['next_segments'] and sum(s['yards'] for s in result['next_segments']) <= .750001
    assert not result['executable']
    # Camera and exposure freshness fail before any image search can issue a plan.
    with pytest.raises(ProjectionError,match='camera_changed'):
        plan_segmented_approach(projection,data,goal,replace(frame,camera_signature=(4,30,8)),
            now=11,frame_id='f',image_sha256='a'*64)
    with pytest.raises(ProjectionError,match='expired'):
        plan_segmented_approach(projection,data,goal,frame,now=100,frame_id='f',image_sha256='a'*64)
    nearby=interaction_region(projection,frame.player_position,1)
    check=plan_segmented_approach(projection,data,nearby,frame,now=11,frame_id='f',image_sha256='a'*64)
    assert check['next_step']=='check_interaction_range' and not check['next_segments']
    assert not check['arrival_confirmed']
    data.water[270:330,370:430]=True
    with pytest.raises(ProjectionError,match='water_mode_required'):
        plan_segmented_approach(projection,data,goal,frame,now=11,frame_id='f',image_sha256='a'*64)


def test_interaction_region_uses_world_scale_and_error_margin_not_pixel_circle():
    frame,train,checks,world=fixture();projection=GroundProjection(frame,train,checks)
    a=interaction_region(projection,world((500,300)),1)
    b=interaction_region(projection,world((500,300)),1,position_error_yards=.4)
    assert a.sum()>b.sum()>0 and not (b & ~a).any()
    assert a[300,500]
    with pytest.raises(ProjectionError,match='target_region_invalid'):
        interaction_region(projection,world((500,300)),1,position_error_yards=2)
