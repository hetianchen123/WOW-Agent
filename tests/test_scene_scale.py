"""Fixed camera anchor and digital close-up must preserve ground coordinates."""
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from bot.perception.scene_scale import AvatarCrop, anchor_profile, merge_scales
from bot.perception.player_identity import PlayerIdentityGate
from bot.perception.travel_channel import CameraSample, CameraScene, CameraResult
from bot.control import quest_terrain
from experiments.qwen3_vl import run as vision
from test_terrain_retreat import setup
from test_clutter_escape import inject

LIVE_CASES = json.loads((Path(__file__).parent / 'fixtures/anchor-scales-1917.json').read_text())


def camera():
    return CameraSample(32, 2, CameraScene.INTERACT, CameraScene.INTERACT,
                        1, CameraResult.DISTANCE_VERIFIED, False, False)


def observations():
    crop = AvatarCrop.for_scene((1012, 586))
    raw = {"kind": "humanoid", "box": [430, 570, 580, 710],
           "feet": [500, 710], "evidence": "red upright humanoid, turtle on left"}
    avatar = crop.avatar(raw)
    x, y = avatar["feet"]
    route = {"origin": [x, y], "front": "open", "left": "blocked", "right": "open",
             "next_view": "front", "feature": "open_ground", "landmark": [x, y - 45],
             "obstacle": None, "bypass": None, "evidence": "continuous ground ahead"}
    return crop, raw, avatar, route


def test_fixed_center_is_transformed_to_scene_then_window_not_used_as_feet():
    crop, _, avatar, _ = observations()
    assert crop.pixels == (385, 281, 627, 516)
    x, y = crop.to_window(avatar["feet"])
    assert x == pytest.approx(500, abs=1)
    assert y > 500  # Feet below camera's fixed character-center anchor.
    assert avatar["feet"] != [500, 500]


@pytest.mark.parametrize('size', [(1012, 586), (1200, 700), (800, 600)])
def test_pixel_rounded_crop_maps_without_resizing_drift(size):
    c = AvatarCrop.for_scene(size)
    assert c.to_scene([0, 0]) == pytest.approx([c.pixels[0] / size[0] * 1000, c.pixels[1] / size[1] * 1000], abs=.001)
    assert c.to_scene([1000, 1000]) == pytest.approx([c.pixels[2] / size[0] * 1000, c.pixels[3] / size[1] * 1000], abs=.001)


@pytest.mark.parametrize('change', [{'kind': 'pet'}, {'kind': 'unknown'}, {'feet': None},
    {'box': [0, 500, 150, 710]}, {'feet': [900, 900]}, {'evidence': ''},
    {'kind': 'humanoid/pet/unknown'}])
def test_anchor_prior_does_not_invent_character_or_feet(change):
    crop, raw, _, _ = observations()
    assert crop.avatar(raw | change) is None


@pytest.mark.parametrize('change', [{'view': 1}, {'paused': True}, {'zoom': 5},
    {'result': CameraResult.PENDING}, {'applied': CameraScene.PROBE}])
def test_uncalibrated_or_pending_camera_does_not_activate_fixed_anchor(change):
    assert anchor_profile(camera())
    assert not anchor_profile(replace(camera(), **change))


def test_local_and_wide_agreement_replaces_species_guess_only_with_known_camera():
    _, _, avatar, route = observations()
    view = merge_scales(avatar, route, vision.parse_observation)
    gate = PlayerIdentityGate()
    assert not gate.review(view, {'at': 1, 'heading': 0})['identity_verified']
    accepted = gate.review(view, {'at': 1, 'heading': 0, 'anchor_profile': 'centered_view_2'})
    assert accepted['identity_verified'] and accepted['front'] == 'open'


@pytest.mark.parametrize('change', [{'origin': None}, {'origin': [200, 500]}, {'landmark': None},
    {'landmark': [500, 950]}, {'landmark': [800, 650]}, {'landmark': [500, 200]}])
def test_unknown_or_disconnected_ground_never_authorizes_forward(change):
    _, _, avatar, route = observations()
    assert merge_scales(avatar, route | change, vision.parse_observation)['front'] != 'open'


def test_grass_beyond_reported_barrier_cannot_authorize_crossing():
    _, _, avatar, route = observations()
    x, y = avatar['feet']
    view = merge_scales(avatar, route | {'obstacle': [x-20, y-35, x+20, y-15]}, vision.parse_observation)
    assert view['front'] == 'blocked' and 'landmark' not in view


def test_model_copying_avatar_box_as_fence_stays_unknown():
    _, _, avatar, route = observations()
    view = merge_scales(avatar, route | {'obstacle': [avatar['box']]}, vision.parse_observation)
    assert view['front'] == view['left'] == view['right'] == 'unknown'
    assert view['route_rejection'] == 'obstacle_is_avatar_box'
    assert view['identity_candidate'] and view['obstacle'] is None


@pytest.mark.parametrize('case', LIVE_CASES, ids=lambda case: case['image'])
def test_real_fence_frames_anchor_player_but_reject_self_obstacle(case):
    crop = AvatarCrop.for_scene(tuple(case['size']))
    avatar = crop.avatar(case['avatar'])
    assert 480 < avatar['feet'][0] < 520
    view = merge_scales(avatar, case['route'], vision.parse_observation)
    result = PlayerIdentityGate().review(view, {'anchor_profile': 'centered_view_2', 'at': 1, 'heading': 0})
    assert result['identity_verified']
    assert result['front'] == result['left'] == result['right'] == 'unknown'
    assert result['route_rejection'] == 'obstacle_is_avatar_box'


def test_fence_endpoint_only_causes_stationary_look(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    probe.observations = {'left': {'front': 'unknown', 'route_hint': {'side': 'right', 'point': [620, 730]}}}
    probe.capture_poses = {'left': {'heading': 0}}
    command = probe.choose_or_scan(ctx, 1, nav, 0, {'recommendation': 'stop'})
    assert not command.keys and not command.tap and probe.direction == 'gap_right'
    assert events[-1][0] == 'quest_terrain_endpoint_look'
    assert events[-1][1]['movement_authorized'] is False


def test_runtime_uses_camera_profile_and_does_not_zoom_for_local_crop(monkeypatch):
    probe, nav, ctx, _, requests = setup(monkeypatch)
    nav = replace(nav, camera=camera(), probe_camera_available=True)
    probe.phase, probe.direction = 'scan', 'left'
    command = probe.step(ctx, 1, nav, probe.angles['left'])
    assert not command.tap and not command.keys
    assert requests[-1]['player_identity']['screen_anchor']['profile'] == 'centered_view_2'
    assert requests[-1]['pose']['left']['anchor_profile'] == 'centered_view_2'


def test_actual_two_stage_runner_saves_both_scales_and_canonical_coordinates(tmp_path, monkeypatch):
    crop, raw, avatar, route = observations()
    monkeypatch.setattr(vision, 'ROOT', tmp_path)
    path = tmp_path/'data/terrain/case/scene.png'
    path.parent.mkdir(parents=True)
    Image.new('RGB', (1012, 586)).save(path)
    model = vision.VisionModel.__new__(vision.VisionModel)
    model.model = model.processor = model.config = None
    model.requests = 0
    model.template = lambda *args, **kwargs: args[2]
    outputs = iter([raw, route])
    sizes = []
    def generate(*args, **kwargs):
        sizes.append(args[3][0].size)
        return SimpleNamespace(text=json.dumps(next(outputs)))
    model.generate = generate
    case, = model.inspect([path], nearfield=True, player_identity={'screen_anchor': {'profile': 'centered_view_2'}})
    assert sizes == [(484, 470), (1012, 586)]
    assert case['observation']['feet'] == avatar['feet']
    assert case['observation']['front'] == 'open'
    assert {s['name'] for s in case['stages']} == {'avatar', 'wide'}
    assert all(Path(s['model_input']).is_file() for s in case['stages'])
    assert case['stages'][1]['detail_input'] == case['stages'][0]['model_input']


def test_camera_change_invalidates_running_scale_request(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    nav = replace(nav, camera=camera())
    probe.phase, probe.direction = 'scan', 'left'
    probe.step(ctx, 1, nav, probe.angles['left'])
    command = probe.step(ctx, 2, replace(nav, camera=replace(camera(), zoom=24)), probe.angles['left'])
    assert command.outcome == 'probe_pose_changed' and not command.keys


def test_old_frame_cannot_authorize_motion_even_with_anchor_and_ground(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    _, _, avatar, route = observations()
    view = merge_scales(avatar, route, vision.parse_observation)
    view['identity_verified'] = True
    probe.phase, probe.direction = 'confirm', 'left'
    inject(probe, {'confirm': view}, 1)
    command = probe.step(ctx, 22, nav, probe.angles['left'])
    assert not command.keys and probe.phase != 'walk'


def test_validated_scale_result_reaches_short_step_and_invalidates_after_moving(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    _, _, avatar, route = observations()
    view = merge_scales(avatar, route, vision.parse_observation)
    view['identity_verified'] = True
    probe.phase, probe.direction = 'confirm', 'left'
    probe.angles['left'] = 0
    inject(probe, {'confirm': view}, 1)
    assert probe.step(ctx, 1.1, nav, 0).keys == {'w'}
    moved = replace(nav, position=(nav.position[0], nav.position[1]-.001), sequence=2)
    assert not probe.step(ctx, 1.8, moved, 0).keys
    assert probe.phase == 'confirm' and not probe.observations and not probe.capture_poses
