from dataclasses import replace
from types import SimpleNamespace

import pytest

from bot.control.camera import CameraController
from bot.control.input_mock import MockInput
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import CameraSample, CameraScene as Scene, CameraResult as Result
from bot.perception.travel_channel import CameraBindings, TravelFlags, TravelSample


def setup(scene=Scene.GROUND, *, close=False, request=0):
    controller = CameraController()
    events = []
    ctx = SimpleNamespace(input=MockInput(), log_event=lambda event, data: events.append((event, data)))
    nav = TravelSample(12345, 2451, 2451, (.5, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 0, camera_restore_available=True,
                       camera_zoom_close=close, protocol_version=15,
                       camera=CameraSample(8 if close else 20, 5, scene, Scene.UNKNOWN, request, Result.IDLE))
    return controller, ctx, nav, events


def test_camera_waits_for_matching_new_receipt_and_settles_before_ground_resume():
    camera, ctx, nav, events = setup(request=7)
    assert camera.tick(ctx, 0, nav)
    nav = replace(nav, sequence=1)
    assert camera.tick(ctx, .7, nav)
    assert ctx.input.count("key_down", "f8") == 1 and not ctx.input.count("key_up", "f8")
    # An old success, even for this scene, must not complete the new request.
    nav = replace(nav, sequence=2, camera=replace(nav.camera, applied=Scene.GROUND, result=Result.DISTANCE_VERIFIED))
    assert camera.tick(ctx, .9, nav) and camera.pending
    nav = replace(nav, sequence=3, camera=replace(nav.camera, request=0))
    assert camera.tick(ctx, 1.1, nav) and camera.pending is None
    assert ctx.input.count("key_up", "f8") == 1
    assert camera.tick(ctx, 1.3, replace(nav, sequence=4))
    assert not camera.tick(ctx, 1.6, replace(nav, sequence=5))
    payload = next(data for event, data in events if event == "quest_camera_result")
    assert payload["result"] == "DISTANCE_VERIFIED" and not payload["composition_verified"]


@pytest.mark.parametrize("result", [Result.LIMITED, Result.UNKNOWN, Result.TIMED_OUT, Result.INTERRUPTED])
def test_camera_does_not_repeat_a_failed_obstruction_forever(result):
    camera, ctx, nav, _ = setup(close=True)
    camera.tick(ctx, 0, nav)
    camera.tick(ctx, .7, replace(nav, sequence=1))
    nav = replace(nav, camera=replace(nav.camera, applied=Scene.GROUND, request=1, result=result))
    camera.tick(ctx, 1, replace(nav, sequence=2))
    for seq in range(3, 30):
        camera.tick(ctx, seq, replace(nav, sequence=seq))
    assert ctx.input.count("key_down", "f8") == 1
    # Only a new obstruction after a clear view rearms recovery.
    camera.tick(ctx, 30, replace(nav, sequence=30, camera_zoom_close=False))
    camera.tick(ctx, 31, replace(nav, sequence=31))
    camera.tick(ctx, 31.6, replace(nav, sequence=32))
    assert ctx.input.count("key_down", "f8") == 2


@pytest.mark.parametrize("scene", [Scene.FLIGHT, Scene.LANDING, Scene.RANGED, Scene.MELEE, Scene.GHOST])
def test_camera_background_transaction_never_blocks_combat_or_flight(scene):
    camera, ctx, nav, _ = setup(scene)
    assert not camera.tick(ctx, 0, nav, background=True)
    assert not camera.tick(ctx, .7, replace(nav, sequence=1), background=True)
    assert camera.pending and "f8" in ctx.input._held_keys
    assert not camera.tick(ctx, .8, replace(nav, sequence=2), background=True, locked=True)
    assert camera.pending is None and "f8" not in ctx.input._held_keys


def test_camera_release_on_lost_data_and_late_reply_cannot_resume_old_transaction():
    camera, ctx, nav, _ = setup()
    camera.tick(ctx, 0, nav)
    nav = replace(nav, sequence=1)
    camera.tick(ctx, .7, nav)
    camera.guard(ctx, 1.5, nav)  # unchanged sequence
    assert camera.pending is None and ctx.input.count("key_up", "f8") == 1
    camera.tick(ctx, 1.6, replace(nav, sequence=2, camera=replace(
        nav.camera, request=1, applied=Scene.GROUND, result=Result.DISTANCE_VERIFIED)))
    assert camera.pending is None


def test_dynamic_camera_releases_the_previously_held_physical_key():
    camera, ctx, nav, _ = setup()
    nav = replace(nav, addon_build=319, camera_bindings=CameraBindings(1, "y", "h"))
    assert camera.tick(ctx, 0, nav)
    assert camera.tick(ctx, .7, replace(nav, sequence=1))
    assert ctx.input.count("key_down", "y") == 1 and not ctx.input.count("key_down", "f8")
    latest = replace(nav, sequence=2, camera_bindings=CameraBindings(2, "y", "h"))
    assert camera.guard(ctx, .8, latest)
    assert camera.pending is None and ctx.input.count("key_up", "y") == 1


def test_camera_scene_debounce_manual_pause_and_unknown_context():
    camera, ctx, nav, _ = setup()
    camera.tick(ctx, 0, nav)
    mounted = replace(nav, sequence=1, camera=replace(nav.camera, scene=Scene.MOUNTED))
    camera.tick(ctx, .3, mounted)
    camera.tick(ctx, .7, replace(nav, sequence=2))
    assert not ctx.input.count("key_down")
    camera.tick(ctx, 1.4, replace(nav, sequence=3))
    assert ctx.input.count("key_down", "f8") == 1
    camera.tick(ctx, 1.5, replace(nav, sequence=4, camera=replace(nav.camera, paused=True)))
    assert camera.pending is None
    camera.tick(ctx, 7, replace(nav, sequence=5, camera=replace(nav.camera, scene=Scene.GAME_CONTROL)))
    assert ctx.input.count("key_down", "f8") == 1


def test_camera_timeout_without_receipt_releases_lease_without_blind_retry():
    camera, ctx, nav, _ = setup()
    camera.tick(ctx, 0, nav)
    camera.tick(ctx, .7, replace(nav, sequence=1))
    for i in range(2, 15):
        camera.tick(ctx, i, replace(nav, sequence=i))
    assert camera.pending is None and ctx.input.count("key_down", "f8") == 1
    assert ctx.input.count("key_up", "f8") == 1


def test_camera_allows_full_near_to_max_zoom_before_receipt_deadline():
    camera, ctx, nav, events = setup(close=True)
    camera.tick(ctx, 0, nav)
    camera.tick(ctx, .7, replace(nav, sequence=1))
    for seq in range(2, 9):
        camera.tick(ctx, seq, replace(nav, sequence=seq, camera=replace(
            nav.camera, request=1, applied=Scene.GROUND, result=Result.PENDING)))
        assert camera.pending and "f8" in ctx.input._held_keys
    receipt = replace(nav, sequence=9, camera=replace(nav.camera, zoom=39,
        request=1, applied=Scene.GROUND, result=Result.DISTANCE_VERIFIED))
    camera.tick(ctx, 8.3, receipt)
    assert camera.pending is None and "f8" not in ctx.input._held_keys
    assert next(data for event, data in events if event == "quest_camera_result")["zoom"] == 39
