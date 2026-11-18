"""Real 16:48 failure replay; other geometry here is explicitly synthetic."""
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.perception.automatic_ground import AutomaticGround,MotionProjection,match,orbit_anchor,sample
from bot.perception.ground_calibration import transform
from bot.perception.ground_projection import ProjectionError,ProjectionFrame
from bot.perception.ground_tracking import validate_tracking
from bot.decision.automatic_approach import npc_search_region
from bot.control.projected_route import plan_segmented_approach


def recorded():
    path=Path(__file__).parent/'fixtures/ground-surface-20261117-1648'
    frames=json.loads(path.with_suffix('.json').read_text())['frames']
    with np.load(path.with_suffix('.npz')) as values:
        for index,frame in enumerate(frames):
            for key in ('size','world_size','player_position','camera_signature'):
                frame[key]=tuple(frame[key])
            ground=values[f'ground{index}']
            yield ProjectionFrame(**frame),values[f'image{index}'],SimpleNamespace(
                ground=ground,semantic_confidence=np.ones(ground.shape))


def test_latest_real_failed_frames_have_local_support_but_not_metric_authority():
    rows=list(recorded());base=sample(*rows[0]);anchors=[]
    for row in rows[1:]:
        matrix,evidence=match(sample(*row),base,require_rotation=True)
        anchors.append(orbit_anchor(matrix,row[0].size))
        assert evidence['excluded_surface_matches']>=20
        assert evidence['fit_ratio']>=.9 and evidence['holdout_ratio']>=.9
        assert evidence['holdout_max_px']<=2
        assert min(p[1] for p in evidence['reference_matches'])>=evidence['surface_reference_y_min']
    assert np.linalg.norm(anchors[0]-anchors[1])<2
    calibration=AutomaticGround();reasons=[]
    for frame,rgb,masks in rows:
        projection,evidence=calibration.update(frame,rgb,masks,now=frame.captured_at+.1)
        assert projection is None
        reasons.append(evidence['reason'])
    assert reasons==['automatic_ground_pivot_required','independent_pivot_check','independent_rotation_required']
    assert calibration.matrix is None and calibration.anchor is None


def surface_points():
    x,y=np.meshgrid(np.linspace(210,790,24),np.linspace(220,570,40))
    points=np.column_stack([x.ravel(),y.ravel()])
    matrix=np.array([[1.,.02,-8],[-.02,1.,10],[0,0,1.]])
    return points,transform(matrix,points)


def test_different_far_surface_is_not_forced_into_local_plane():
    source,destination=surface_points()
    far=destination[:,1]<330
    destination[far,0]+=18
    with pytest.raises(ProjectionError):validate_tracking(source,destination,(1000,600))
    matrix,evidence=validate_tracking(source,destination,(1000,600),local_surface=True)
    assert evidence['excluded_surface_matches']>0
    assert min(p[1] for p in evidence['reference_matches'])>=330
    assert evidence['holdout_max_px']<.01
    assert np.max(np.linalg.norm(transform(matrix,source[~far])-destination[~far],axis=1))<.01


@pytest.mark.parametrize('fault',['holdout_far','holdout_near','mixed_near','mirror'])
def test_surface_selection_cannot_cherry_pick_failed_validation_or_mixed_local_ground(fault):
    source,destination=surface_points();train=np.arange(len(source))%5!=0
    if fault=='holdout_far':destination[~train & (destination[:,1]<360),0]+=18
    if fault=='holdout_near':destination[~train & (destination[:,1]>390),0]+=18
    if fault=='mixed_near':destination[np.flatnonzero(train & (destination[:,1]>390))[::2],0]+=18
    if fault=='mirror':destination[:,0]=1000-destination[:,0]
    with pytest.raises(ProjectionError):
        validate_tracking(source,destination,(1000,600),local_surface=True)


def local_scene():
    frame=ProjectionFrame('frontier','a'*64,(400,300),2451,'auto',(1000,1000),
        (.4,.5),(100,150),10,0,(4,32,1),100)
    matrix=np.array([[.05,0,-5],[0,.05,-7.5],[0,0,1.]])
    hull=np.array([[[20,20]],[[300,20]],[[300,280]],[[20,280]]],np.float32)
    projection=MotionProjection(frame,matrix,hull,.1,2)
    ground=np.ones((300,400),bool);zero=np.zeros_like(ground)
    observed=SimpleNamespace(ground=ground,obstacles=zero.copy(),occluded=zero.copy(),
        water=zero.copy(),forbidden=zero.copy(),instances=[])
    return projection,observed,np.zeros((300,400,3),np.uint8)


def test_out_of_support_npc_uses_measured_frontier_and_still_routes_around_wall():
    projection,observed,rgb=local_scene()
    observed.obstacles[80:230,160:180]=True
    region,evidence=npc_search_region(projection,observed,rgb,(.418,.5))
    assert evidence['basis']=='measured_local_frontier' and evidence['intermediate']
    assert evidence['requires_reobservation'] and not evidence['interaction_verified']
    result=plan_segmented_approach(projection,observed,region,projection.frame,now=100.1,
        frame_id='frontier',image_sha256='a'*64,max_step_yards=.75)
    points=result['approach']['route']['points']
    assert len(points)>2
    assert all(projection.contains(p) for p in points)
    assert result['arrival_confirmed'] is False and result['executable'] is False
    assert result['movement_budget_yards']==.75
    for a,b in zip(points,points[1:]):
        pixels=np.rint(np.linspace(a,b,200)).astype(int)
        assert not observed.obstacles[pixels[:,1],pixels[:,0]].any()


@pytest.mark.parametrize('barrier',['obstacles','forbidden','water','unknown'])
def test_frontier_does_not_claim_reachability_through_a_sealed_local_boundary(barrier):
    projection,observed,rgb=local_scene()
    if barrier=='unknown':observed.ground[:,125:145]=False
    else:getattr(observed,barrier)[:,125:145]=True
    with pytest.raises(ProjectionError,match='no_reachable_measured_frontier'):
        npc_search_region(projection,observed,rgb,(.418,.5))
