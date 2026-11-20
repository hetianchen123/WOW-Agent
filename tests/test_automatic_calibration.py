"""Synthetic geometry/movement regressions; none represent live game success."""
from dataclasses import replace
from types import SimpleNamespace
import math

import cv2
import numpy as np
import pytest

from bot.perception.automatic_ground import (AutomaticGround,MotionPair,MotionProjection,
                                           fit_motion_plane,motion_error,orbit_anchor)
from bot.perception.ground_calibration import transform
from bot.perception.ground_projection import ProjectionFrame,ProjectionError
from bot.decision.automatic_approach import calibration_probe,npc_search_region
from test_segmented_runtime import controller,ready


def geometry():
    m=np.array([[.04,.002,-16.84],[-.003,.035,-13.5],[.00005,-.0001,1.]])
    p=np.linalg.solve(m,[0,0,1]);anchor=p[:2]/p[2]
    points=np.array([(x,y) for y in range(150,550,35) for x in range(140,700,35)])
    def pair(d,i=0):
        t=np.eye(3);t[:2,2]=d;h=np.linalg.inv(m)@t@m
        return MotionPair(h,np.array(d),transform(h,points),points,(str(i),str(i+1)))
    return m,anchor,pair


def test_motion_conjugacy_recovers_projective_scale_without_clicked_points():
    m,a,pair=geometry()
    fit=fit_motion_plane([pair((.5,.1)),pair((-.1,.5),1)],a,(800,600))
    for d in ((.3,-.2),(-.4,-.1)):
        assert motion_error(fit,pair(d))<1e-8
    assert np.allclose(transform(fit,[(600,300)]),transform(m,[(600,300)]),atol=1e-7)
    with pytest.raises(ProjectionError,match='more_translation_axes'):
        fit_motion_plane([pair((.5,0)),pair((-.5,0))],a,(800,600))


def test_stationary_orbit_recovers_anchor_even_when_no_character_pixels_exist():
    m,a,_=geometry()
    for angle in (-.17,.19):
        r=np.array([[math.cos(angle),-math.sin(angle),0],
                    [math.sin(angle),math.cos(angle),0],[0,0,1]])
        assert np.allclose(orbit_anchor(np.linalg.inv(m)@r@m,(800,600)),a)
    with pytest.raises(ProjectionError,match='unobservable'):
        orbit_anchor(np.array([[1.,0,10],[0,1,0],[0,0,1]]),(800,600))


def observed(shape=(600,800)):
    return SimpleNamespace(ground=np.ones(shape,bool),semantic_confidence=np.ones(shape),
        obstacles=np.zeros(shape,bool),occluded=np.zeros(shape,bool),water=np.zeros(shape,bool),
        forbidden=np.zeros(shape,bool),instances=[],timings={})


def generated_frames():
    """Rendered static planar texture follows camera yaw and native translation."""
    rng=np.random.default_rng(18)
    image=cv2.GaussianBlur(rng.integers(0,255,(600,800,3),dtype=np.uint8),(3,3),0)
    m,a,_=geometry()
    states=[(0,(0,0)),(.18,(0,0)),(-.18,(0,0)),
            (-.18,(.55,0)),(-.18,(.55,.55)),(-.18,(0,.55)),(-.18,(0,0))]
    for i,(angle,pos) in enumerate(states):
        r=np.array([[math.cos(angle),-math.sin(angle),0],
                    [math.sin(angle),math.cos(angle),0],[0,0,1]])
        t=np.eye(3);t[:2,2]=pos
        h=np.linalg.inv(m)@t@r@m
        rgb=cv2.warpPerspective(image,np.linalg.inv(h),(800,600))
        frame=ProjectionFrame(str(i),'a'*64,(800,600),2451,'automatic-local-plane',(1000,1000),
            (.4+pos[0]/1000,.5+pos[1]/1000),None,10,angle,(4,32,1),100+i)
        yield frame,rgb,observed()


def test_automatic_image_and_native_sequence_fits_then_checks_independent_motion():
    calibration=AutomaticGround();stages=[]
    for frame,rgb,masks in generated_frames():
        projection,evidence=calibration.update(frame,rgb,masks,now=frame.captured_at+.1)
        stages.append(evidence['stage'])
    assert stages[:2]==['orbit','orbit'] and stages[-1]=='ready'
    assert projection.source=='automatic_native_motion'
    assert evidence['fit_movements']==2 and evidence['validation_movements']==2
    assert evidence['manual_points']==0 and evidence['combined_bound_yards']<.25
    # A later same-scene observation reuses the fit, with fresh image registration.
    fresh=replace(frame,frame_id='fresh',captured_at=110)
    again,proof=calibration.update(fresh,rgb,masks,now=110.1)
    assert again.frame.frame_id=='fresh' and proof['stage']=='ready'
    # A different map automatically starts a new fit, without asking for a bundle.
    again,proof=calibration.update(replace(fresh,map_id=99),rgb,masks,now=110.2)
    assert again is None and proof['stage']=='orbit'


def test_independent_bad_native_feedback_never_passes_calibration():
    calibration=AutomaticGround()
    for i,(frame,rgb,masks) in enumerate(generated_frames()):
        if i==5:
            frame=replace(frame,player_position=(frame.player_position[0]+.0005,frame.player_position[1]+.0004))
            with pytest.raises(ProjectionError,match='independent_motion_error'):
                calibration.update(frame,rgb,masks,now=frame.captured_at+.1)
            break
        calibration.update(frame,rgb,masks,now=frame.captured_at+.1)


def test_probe_uses_ground_corridor_alternates_axes_and_excludes_collision():
    masks=observed((200,200));anchor=(100,100)
    # Avatar occludes its own ground; never require the shoe pixel to be visible.
    masks.ground[95:106,95:106]=False;masks.occluded[95:106,95:106]=True
    first=calibration_probe(masks,anchor,[],strafe_verified=True)
    second=calibration_probe(masks,anchor,[dict(key=first['key'],moved=.4)],strafe_verified=True)
    assert first['key'] in {'w','s'} and second['key'] in {'q','e'}
    third=calibration_probe(masks,anchor,[dict(key='s',moved=0)],strafe_verified=True)
    assert third['key']!='s'
    masks.obstacles[90:111,:]=True
    with pytest.raises(ProjectionError,match='no_observed_probe'):
        calibration_probe(masks,anchor,[],strafe_verified=True)


def test_automatic_npc_region_is_search_not_exact_pin_or_interaction():
    frame=ProjectionFrame('npc','a'*64,(200,200),2451,'auto',(1000,1000),(.4,.5),(100,100),10,0,(4,32,1),100)
    m=np.array([[.05,0,-5],[0,.05,-5],[0,0,1.]])
    hull=np.array([[[5,5]],[[195,5]],[[195,195]],[[5,195]]],np.float32)
    p=MotionProjection(frame,m,hull,.1,2)
    masks=observed((200,200));masks.obstacles[:,140:145]=True
    region,evidence=npc_search_region(p,masks,np.zeros((200,200,3),np.uint8),(.403,.5))
    assert region.any() and not region[100,100] and not region[:,140:145].any()
    assert evidence['manual_regions']==0 and evidence['interaction_verified'] is False
    assert evidence['basis']=='native_task_search_area'


def test_controller_calibrates_before_route_and_uses_new_feedback(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    result=requests[0].result
    result.update(kind='calibration_probe',calibration={'stage':'translation'},key='e',hold_s=.07,metric_route=False)
    assert obj.step(ctx,1.2,nav,heading).tap is None
    command=obj.step(ctx,1.3,nav,heading)
    assert command.tap=='e' and command.hold_s==.07
    fresh=replace(nav,sequence=3,position=(nav.position[0],nav.position[1]+.0004))
    assert obj.step(ctx,2,fresh,heading).tap is None
    assert obj.motion_history[-1]['moved']==pytest.approx(.4)
    assert obj.step(ctx,2.1,fresh,heading).tap is None and obj.pending is not None


def test_old_orbit_result_and_excessive_orbit_never_send_input(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    requests[0].result.update(kind='calibration_orbit',calibration={'stage':'orbit'},yaw_pixels=300)
    command=obj.step(ctx,1.2,nav,heading)
    assert command.outcome=='auto_calibration_action_invalid' and command.camera_yaw_px is None


def test_camera_orbit_waits_for_native_lease_receipt_then_releases(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    requests[0].result.update(kind='calibration_orbit',calibration={'stage':'orbit'},yaw_pixels=110)
    assert obj.step(ctx,1.2,nav,heading).camera_yaw_px is None
    command=obj.step(ctx,1.3,nav,heading)
    assert command.keys==frozenset({'f4'}) and command.camera_yaw_px is None
    assert obj.step(ctx,1.4,nav,heading).camera_yaw_px is None
    fresh=replace(nav,sequence=3,calibration_orbit_active=True,
                  camera=SimpleNamespace(view=4,zoom=32,request=1))
    command=obj.step(ctx,1.45,fresh,heading)
    assert command.camera_yaw_px==110 and command.keys==frozenset({'f4'})
    assert not obj.step(ctx,1.5,fresh,heading).keys


def test_stationary_calibration_without_mouse_lease_uses_verified_turn(tmp_path, monkeypatch):
    obj, ctx, nav, requests = controller(tmp_path, monkeypatch)
    nav = replace(nav, calibration_orbit_available=False, overhead_camera_available=True)
    nav, heading = ready(obj, ctx, nav)
    requests[0].result.update(kind="calibration_orbit", calibration={"stage": "orbit"}, yaw_pixels=110)
    assert obj.step(ctx, 1.2, nav, heading).tap is None
    command = obj.step(ctx, 1.3, nav, heading)
    assert command.tap in {"left", "right"} and not command.keys
    assert obj.orbit.keyboard and obj.orbit_request is None


def test_orbit_mouse_failure_releases_button_and_lease(tmp_path,monkeypatch):
    from bot.control.input_mock import MockInput
    from bot.control.quest_travel import TravelCommand
    from bot.modules import quest
    from test_npc_obstacle import npc
    run,ctx,_,nav,_=npc(tmp_path,monkeypatch)
    monkeypatch.setattr(quest,'frontmost_game_bounds',lambda:(0,0,1600,1000))
    calls=[]
    original=ctx.input._move_raw
    def move(x,y):
        calls.append((x,y))
        if len(calls)>1:raise OSError('lost focus')
        original(x,y)
    monkeypatch.setattr(ctx.input,'_move_raw',move)
    with pytest.raises(OSError,match='lost focus'):
        run.apply_travel_command(ctx,TravelCommand(keys=frozenset({'f4'}),camera_yaw_px=110))
    assert not ctx.input._held_keys and not ctx.input._held_buttons
    assert ctx.input.count('mouse_up','right')==1


def test_worker_automatic_sequence_needs_no_bundle_or_registered_target(tmp_path,monkeypatch,capsys):
    import hashlib,json
    from dataclasses import asdict
    from experiments.route_models import segmentation_worker as worker
    clock={'now':100}
    class Segmenter:
        def __init__(self,*a,**kw):self.load_s=0
        def inspect(self,rgb,**kw):return observed(rgb.shape[:2])
    models=tmp_path/'models.json';models.write_text('{}')
    requests=[]
    for i,(frame,rgb,_) in enumerate(generated_frames()):
        folder=tmp_path/str(i);folder.mkdir();path=folder/'image.png'
        cv2.imwrite(str(path),cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
        frame=replace(frame,image_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        job=dict(automatic=True,session_id='test',frame=asdict(frame),quest_id=12345,phase='READY',
            models=str(models),checkpoint='synthetic',device='cpu',convention='ccw_west',
            strafe_verified=True,destination=[.404,.5],motion_history=[])
        requests.append((frame.captured_at,dict(id=str(i),images=[str(path)],target=job)))
    def inputs():
        for stamp,request in requests:
            clock['now']=stamp+.1
            yield json.dumps(request)
    monkeypatch.setattr(worker,'LocalSegmenter',Segmenter)
    monkeypatch.setattr(worker.time,'monotonic',lambda:clock['now'])
    monkeypatch.setattr(worker.sys,'stdin',inputs())
    worker.main()
    replies=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any('error' in row for row in replies),replies
    result=replies[-1]['cases'][0]
    assert result['kind']=='route' and result['registration']['validation_movements']==2
    assert result['automatic_goal']['manual_regions']==0
    assert result['calibration']['manual_points']==0
    assert result['next_segment_world'][0]==pytest.approx([.4,.5],abs=1e-6)
