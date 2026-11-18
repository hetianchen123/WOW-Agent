"""Regression for the 15:45 mouse event with no native/image rotation."""
from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.calibration_orbit import CalibrationOrbit
from bot.control.input_mock import MockInput
from bot.control import input_base
from bot.perception import automatic_ground as ag
from bot.perception.ground_projection import ProjectionError
from test_segmented_runtime import controller, ready
from test_automatic_calibration import generated_frames


def test_mouse_without_motion_uses_turn_keys_before_any_second_model_call(tmp_path, monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    requests[0].result.update(kind='calibration_orbit',calibration={'stage':'orbit'},yaw_pixels=110)
    obj.step(ctx,1.2,nav,heading)
    assert obj.step(ctx,1.3,nav,heading).keys==frozenset({'f4'})
    nav=replace(nav,sequence=3,calibration_orbit_active=True,
                camera=SimpleNamespace(view=4,zoom=32,request=1))
    assert obj.step(ctx,1.45,nav,heading).camera_yaw_px==110
    still=replace(nav,sequence=4,calibration_orbit_active=False)
    command=obj.step(ctx,2.2,still,heading)
    assert command.tap=='left' and command.precise
    assert len(requests)==1 and obj.pending is None
    assert obj.step(ctx,2.3,still,heading).tap is None
    turned=replace(still,sequence=5)
    new_heading=heading+math.radians(12)
    assert obj.step(ctx,2.7,turned,new_heading).outcome is None
    assert obj.orbit is None and obj.orbit_keyboard_preferred
    obj.step(ctx,2.8,turned,new_heading)
    assert obj.pending is not None  # Only now can the next exposure be scheduled.


@pytest.mark.parametrize('convention,key', [('ccw_west','left'),('cw_east','right')])
def test_keyboard_orbit_uses_fresh_feedback_and_bounded_pulses(tmp_path,monkeypatch,convention,key):
    _,ctx,nav,_=controller(tmp_path,monkeypatch)
    orbit=CalibrationOrbit(1,nav,1,110,convention,keyboard=True)
    assert orbit.step(ctx,1,nav,1).tap==key
    assert orbit.step(ctx,1.1,nav,1).tap is None
    assert orbit.step(ctx,1.5,nav,1+math.radians(12)).outcome is None  # Same nav sequence.
    fresh=replace(nav,sequence=nav.sequence+1)
    assert orbit.step(ctx,1.6,fresh,1+math.radians(12)).outcome=='auto_orbit_ready'
    stuck=CalibrationOrbit(2,nav,1,110,convention,keyboard=True)
    stuck.step(ctx,2,nav,1)
    assert stuck.step(ctx,2.5,fresh,1).tap==key
    assert stuck.step(ctx,3,replace(fresh,sequence=fresh.sequence+1),1).outcome=='auto_orbit_turn_unconfirmed'


@pytest.mark.parametrize('change,angle,reason', [
    ({'position':(.3,.3)},0,'auto_orbit_player_moved'),
    ({'height':11},0,'auto_orbit_player_moved'),
    ({},math.radians(36),'auto_orbit_turn_excessive'),
    ({},-math.radians(12),'auto_orbit_turn_direction_mismatch'),
])
def test_keyboard_orbit_rejects_wrong_feedback(tmp_path,monkeypatch,change,angle,reason):
    _,ctx,nav,_=controller(tmp_path,monkeypatch)
    orbit=CalibrationOrbit(1,nav,1,110,'ccw_west',keyboard=True)
    orbit.step(ctx,1,nav,1)
    fresh=replace(nav,sequence=nav.sequence+1,**change)
    assert orbit.step(ctx,1.5,fresh,1+angle).outcome==reason


def test_mouse_turn_waits_for_capture_and_sends_multiple_drags_then_releases(monkeypatch):
    backend=MockInput()
    sleeps=[]
    monkeypatch.setattr(input_base.time,'sleep',lambda duration:sleeps.append((duration,len(backend.actions))))
    x,y=backend.position()
    backend.rmb_turn(110)
    assert sleeps[0]==(.06,1)  # Button is down, no drag yet.
    moves=[args for _,kind,args in backend.actions if kind=='move']
    assert len(moves)==3 and moves[-1]==(x+110,y)
    assert [p[0] for p in moves]==sorted(p[0] for p in moves)
    assert backend.count('mouse_up','right')==1 and not backend._held_buttons
    assert sum(s[0] for s in sleeps)<.2  # Fits inside the existing camera lease.


def test_image_registration_failure_has_one_stationary_retry_not_a_route(monkeypatch):
    calibration=ag.AutomaticGround()
    frames=list(generated_frames())
    first,rgb,masks=frames[0]
    calibration.update(first,rgb,masks,now=100.1)
    def failed(*args,**kwargs):raise ProjectionError('registration_holdout_error')
    monkeypatch.setattr(ag,'match',failed)
    frame,rgb,masks=frames[1]
    projection,evidence=calibration.update(frame,rgb,masks,now=101.1)
    assert projection is None and evidence['stage']=='observe'
    assert evidence['reason']=='registration_holdout_error'
    with pytest.raises(ProjectionError,match='registration_holdout_error'):
        calibration.update(replace(frame,frame_id='new',captured_at=102),rgb,masks,now=102.1)
    assert not calibration.training and not calibration.anchors


def test_observation_retry_waits_without_turn_or_move(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    requests[0].result.update(kind='calibration_observe',calibration={'stage':'observe'},wait_s=.7)
    obj.step(ctx,1.2,nav,heading)
    assert obj.step(ctx,1.3,nav,heading).tap is None
    assert not obj.step(ctx,1.8,replace(nav,sequence=3),heading).keys
    assert obj.pending is None and len(requests)==1
    obj.step(ctx,2.1,replace(nav,sequence=4),heading)
    assert obj.pending is not None and obj.orbits==0


def test_recorded_static_ground_is_not_accepted_as_camera_rotation():
    pairs=np.load(Path(__file__).parent/'fixtures/calibration-orbit-no-motion-20261117.npz')
    with pytest.raises(ProjectionError,match='auto_camera_rotation_unobserved') as failure:
        ag.rotation_observed(pairs['source'],pairs['destination'])
    assert failure.value.evidence['matches']>=40
    assert failure.value.evidence['median_px']<.75


def test_keyboard_orbit_cancels_changed_camera_and_requires_post_input_heading(tmp_path,monkeypatch):
    _,ctx,nav,_=controller(tmp_path,monkeypatch)
    orbit=CalibrationOrbit(1,nav,1,110,'ccw_west',keyboard=True)
    orbit.step(ctx,1,nav,1)
    fresh=replace(nav,sequence=nav.sequence+1)
    ctx.heading_age=lambda:.5
    assert orbit.step(ctx,1.5,fresh,1+math.radians(12)).outcome is None
    changed=replace(fresh,camera=SimpleNamespace(view=2,zoom=32))
    assert orbit.step(ctx,1.6,changed,1).outcome=='auto_orbit_camera_changed'


def test_real_mouse_yaw_receipt_keeps_the_mouse_path(tmp_path,monkeypatch):
    _,ctx,nav,_=controller(tmp_path,monkeypatch)
    orbit=CalibrationOrbit(1,nav,1,110,'ccw_west')
    fresh=replace(nav,sequence=nav.sequence+1)
    assert orbit.step(ctx,1.8,fresh,1-math.radians(12)).outcome=='auto_orbit_ready'
    assert not orbit.keyboard and orbit.pulses==0


def test_failed_worker_geometry_preserves_masks_and_reason(tmp_path,monkeypatch,capsys):
    import json,hashlib
    from dataclasses import asdict
    import cv2
    from experiments.route_models import segmentation_worker as worker
    frame,rgb,masks=next(generated_frames())
    path=tmp_path/'image.png';cv2.imwrite(str(path),rgb)
    frame=replace(frame,image_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    models=tmp_path/'models.json';models.write_text('{}')
    monkeypatch.setattr(worker,'LocalSegmenter',lambda *_,**__:SimpleNamespace(
        inspect=lambda *_,**__:masks,load_s=0))
    def failed(*_,**__):raise ProjectionError('registration_holdout_error')
    monkeypatch.setattr(worker,'automatic_job',failed)
    request={'id':'failure','images':[str(path)],'target':dict(automatic=True,frame=asdict(frame),
        models=str(models),checkpoint='fake',device='cpu',session_id='replay')}
    monkeypatch.setattr(worker.sys,'stdin',[json.dumps(request),'invalid JSON'])
    worker.main()
    replies=[json.loads(row) for row in capsys.readouterr().out.splitlines()]
    assert replies[1]['error']=='registration_holdout_error'
    assert (tmp_path/'masks.npz').exists()
    assert json.loads((tmp_path/'failure.json').read_text())=={
        'request_id':'failure','error':'registration_holdout_error','details':None}
