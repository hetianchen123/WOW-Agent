from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.perception.ground_projection import GroundAnchor,ProjectionFrame,GroundProjection,ProjectionError


def fixture():
    frame=ProjectionFrame('f','a'*64,(800,600),2451,'outdoor-ground',(1000,500),(.25,.36),
                          (400,300),100,0,(4,32,7,90),10)
    # Perspective, anisotropy and rotated world axes; no hardcoded camera angle.
    h=np.array([[.028,.012,-14.8],[-.007,.035,-7.7],[.00015,-.0001,1.]])
    def world(pixel):
        d=h@[*pixel,1];d=d[:2]/d[2]
        return tuple(float(o+v/s) for o,v,s in zip(frame.player_position,d,frame.world_size))
    def anchor(p,i):return GroundAnchor(p,world(p),str(i),'synthetic_regression')
    training=[anchor(p,i) for i,p in enumerate([(220,140),(400,140),(580,140),(580,460),(400,460),(220,460)])]
    validation=[anchor(p,i+10) for i,p in enumerate([(280,220),(520,220),(400,400)])]
    return frame,training,validation,world


def test_measured_projection_handles_perspective_rotation_and_unequal_map_scales():
    frame,train,check,world=fixture();projection=GroundProjection(frame,train,check)
    route=SimpleNamespace(frame_id='f',points=[(400,300),(500,330),(500,200),(350,200)])
    result=projection.project_route(route,frame,now=11)
    assert np.allclose(result.world_points,[world(p) for p in route.points],atol=1e-7)
    assert result.validation_max_error_yards<1e-4
    assert result.world_points[-1][0]!=frame.player_position[0]
    assert result.executable is False  # Math regression cannot authorize game movement.


def test_bad_independent_measurement_cannot_be_hidden_by_perfect_fit():
    frame,train,check,_=fixture()
    check[1]=replace(check[1],world_position=(check[1].world_position[0]+.001,check[1].world_position[1]))
    with pytest.raises(ProjectionError,match='holdout_error'):GroundProjection(frame,train,check)


def test_reusing_training_observation_for_validation_is_rejected():
    frame,train,check,_=fixture();check[0]=replace(check[0],observation_id=train[0].observation_id)
    with pytest.raises(ProjectionError,match='not_independent'):GroundProjection(frame,train,check)


def test_single_direction_motion_cannot_calibrate_two_dimensional_movement():
    frame,train,check,_=fixture()
    train=[replace(a,pixel=(300+i*20,300)) for i,a in enumerate(train)]
    with pytest.raises(ProjectionError,match='coverage_degenerate'):GroundProjection(frame,train,check)


def test_model_estimated_world_points_are_not_measurements():
    frame,train,check,_=fixture()
    train=[replace(a,source='model_guess') for a in train]
    with pytest.raises(ProjectionError,match='unmeasured'):GroundProjection(frame,train,check)


@pytest.mark.parametrize('change,reason',[
    ({'image_sha256':'b'*64},'frame_mismatch'),({'floor_id':'upper-floor'},'floor_changed'),
    ({'camera_signature':(4,24,8,90)},'camera_changed'),({'height':102},'pose_changed'),
    ({'heading':.1},'pose_changed'),({'player_position':(.251,.36)},'pose_changed')])
def test_changed_frame_or_pose_cannot_reuse_old_projection(change,reason):
    frame,train,check,_=fixture();projection=GroundProjection(frame,train,check)
    route=SimpleNamespace(frame_id='f',points=[frame.player_feet,(500,200)])
    with pytest.raises(ProjectionError,match=reason):
        projection.project_route(route,replace(frame,**change),now=11)


def test_expired_or_extrapolated_route_rejected_instead_of_clamping():
    frame,train,check,_=fixture();projection=GroundProjection(frame,train,check)
    route=SimpleNamespace(frame_id='f',points=[frame.player_feet,(600,200)])
    with pytest.raises(ProjectionError,match='outside_calibrated'):
        projection.project_route(route,frame,now=11)
    with pytest.raises(ProjectionError,match='expired'):
        projection.project_route(route,frame,now=31)


@pytest.mark.parametrize('change',[{'floor_id':'unknown'},{'camera_signature':(4,32,7,None)}])
def test_unknown_layer_or_camera_values_cannot_form_a_calibration(change):
    frame,train,check,_=fixture()
    with pytest.raises(ProjectionError,match='frame_incomplete'):
        GroundProjection(replace(frame,**change),train,check)


def test_renamed_training_point_cannot_act_as_held_out_validation():
    frame,train,check,_=fixture();check[0]=replace(train[0],observation_id='new-id')
    with pytest.raises(ProjectionError,match='not_independent'):GroundProjection(frame,train,check)
