from dataclasses import replace
from types import SimpleNamespace

import pytest

from bot.control.overhead_camera import OverheadObservation
from bot.perception.travel_channel import CameraSample, CameraScene as Scene, CameraResult as Result, TravelFlags
from test_quest_travel import sample
from test_model_detour import fixture


def setup():
    events=[]
    ctx=SimpleNamespace(log_event=lambda n,d:events.append((n,d)))
    nav=sample(flags=TravelFlags.KEYS_VERIFIED,overhead_camera_available=True,
        camera=CameraSample(32,5,Scene.INTERACT,Scene.INTERACT,7,Result.DISTANCE_VERIFIED))
    return OverheadObservation(),ctx,nav,events


def receipt(nav,sequence=2):
    return replace(nav,sequence=sequence,camera=replace(nav.camera,view=4,
        request=0,applied=Scene.OVERHEAD,result=Result.DISTANCE_VERIFIED))


def test_new_receipt_then_stable_frame_required_before_observing():
    ctl,ctx,nav,events=setup()
    assert ctl.step(ctx,0,nav).keys=={'f6'}
    # Matching old success is not this action's receipt.
    assert ctl.step(ctx,.1,receipt(nav,nav.sequence)).keys==set()
    assert ctl.settle is None
    nav=receipt(nav)
    assert not ctl.step(ctx,.2,nav).keys
    assert ctl.step(ctx,.5,replace(nav,sequence=3)) is not None
    assert ctl.step(ctx,.71,replace(nav,sequence=4)) is None
    assert ctl.done and ctl.used
    assert all(not d['composition_verified'] for _,d in events)


def test_unsaved_overhead_preset_does_not_send_key_or_fake_calibration():
    ctl,ctx,nav,_=setup()
    command=ctl.step(ctx,0,replace(nav,overhead_camera_available=False))
    assert command.outcome=='escape_overhead_preset_unconfigured' and not command.keys
    assert not ctl.used


@pytest.mark.parametrize('change',[
    {'view':2}, {'zoom':9}, {'applied':Scene.PROBE}, {'result':Result.UNKNOWN}, {'request':3}])
def test_mismatched_receipt_stops_without_motion(change):
    ctl,ctx,nav,_=setup();ctl.step(ctx,0,nav)
    new=receipt(nav)
    command=ctl.step(ctx,.2,replace(new,camera=replace(new.camera,**change)))
    assert command.outcome and not command.keys and not command.tap


@pytest.mark.parametrize('flag',[TravelFlags.IN_COMBAT,TravelFlags.FLYING,TravelFlags.FALLING,
    TravelFlags.MOUNTED,TravelFlags.SWIMMING,TravelFlags.CASTING])
def test_unsafe_state_releases_camera_lease(flag):
    ctl,ctx,nav,_=setup();ctl.step(ctx,0,nav)
    command=ctl.step(ctx,.1,replace(nav,sequence=2,flags=nav.flags|flag))
    assert command.outcome=='probe_interrupted' and not command.keys


def test_stale_stream_and_time_budget_are_bounded():
    ctl,ctx,nav,_=setup();ctl.step(ctx,0,nav)
    assert ctl.step(ctx,.8,nav).outcome=='probe_telemetry_stale'
    ctl,ctx,nav,_=setup();ctl.step(ctx,0,nav)
    assert ctl.step(ctx,8.61,replace(nav,sequence=2)).outcome=='escape_overhead_camera_unconfirmed'


def test_runtime_capture_waits_for_overhead_then_never_uses_low_angle_projection(monkeypatch):
    ctl,nav,ctx,calls,events=fixture(monkeypatch)
    ctl.overhead=OverheadObservation()
    _,_,camera_nav,_=setup()
    nav=replace(nav,camera=camera_nav.camera,overhead_camera_available=True)
    assert ctl.step(ctx,0,nav,0).keys=={'f6'}
    assert not calls
    nav=receipt(nav)
    assert not ctl.step(ctx,.2,nav,0).keys
    nav=replace(nav,sequence=3)
    ctl.step(ctx,.71,nav,0)
    assert len(calls)==1 and calls[0]['view_mode']=='overhead'
    # Even an injected world plan cannot bypass the uncalibrated overhead gate.
    command=ctl.step(ctx,.8,replace(nav,sequence=4),0)
    assert command.outcome=='escape_route_overhead_image_valid_projection_unverified'
    assert not command.keys and not command.tap and ctl.follower is None


def test_parent_records_probe_for_restore_even_if_interrupted(monkeypatch):
    from bot.control.quest_terrain import TerrainDetour
    from bot.control.quest_travel import QuestTrip
    ctl,nav,ctx,_,_=fixture(monkeypatch)
    ctl.overhead=OverheadObservation()
    _,_,camera_nav,_=setup()
    nav=replace(nav,camera=camera_nav.camera,overhead_camera_available=True)
    trip=QuestTrip(nav.world_size,nav.destination,'ccw_west')
    terrain=TerrainDetour(0,nav,0,trip,None)
    terrain.phase='model_route';terrain.model_detour=ctl
    assert terrain.step(ctx,.1,nav,0).keys=={'f6'}
    assert terrain.probe_view_used
    command=terrain.step(ctx,.2,replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT),0)
    assert command.outcome=='probe_interrupted' and not command.keys
    assert terrain.probe_view_used and ctl.closed


def test_rejected_overhead_map_does_not_repeat_same_unchanged_observation(monkeypatch):
    from concurrent.futures import Future
    from bot.decision.route_planner import PlannerError
    calls=[]
    def request(*args):
        calls.append(True)
        future=Future();future.set_exception(PlannerError('route_overhead_outline_unconfirmed'))
        return SimpleNamespace(future=future,close=lambda:None,evidence_dir='test')
    ctl,nav,ctx,_,_=fixture(monkeypatch,request)
    ctl.overhead=OverheadObservation()
    _,_,camera_nav,_=setup()
    nav=replace(nav,camera=camera_nav.camera,overhead_camera_available=True)
    ctl.step(ctx,0,nav,0)
    nav=receipt(nav);ctl.step(ctx,.2,nav,0)
    nav=replace(nav,sequence=3);ctl.step(ctx,.71,nav,0)
    command=ctl.step(ctx,.8,replace(nav,sequence=4),0)
    assert command.outcome=='escape_route_overhead_outline_unconfirmed'
    assert len(calls)==1 and ctl.request is None
