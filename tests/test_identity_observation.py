"""Actual NPC confusion and real-latency observations, with no game input."""
import json
import math
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control import quest_terrain as terrain
from bot.control.quest_travel import QuestTrip
from bot.perception.player_identity import PlayerIdentityGate, profile_from_sample
from bot.perception.travel_channel import CameraSample, CameraScene, CameraResult
from experiments.qwen3_vl.run import parse_near_observation
from test_terrain_retreat import setup
from test_clutter_escape import region

PROFILE = {'race_id': 8, 'race': 'troll', 'class_id': 3}
CASES = json.loads((Path(__file__).parent / 'fixtures/identity-1814.json').read_text())


def candidate(**changes):
    return parse_near_observation(json.dumps(region(schema=4, observed_race='troll',
        identity_evidence='visible tusks and long ears', **changes)), player_identity=PROFILE)


def test_live_npc_box_is_rejected_even_when_model_echoes_troll_identity():
    case = next(c for c in CASES if c['source'] == 'identity-regression-1814' and c['image'] == 'right.png')
    view = parse_near_observation(case['raw_output'], player_identity=PROFILE)
    assert view['schema_inferred']  # Full v4 fields identify the schema; all checks still apply.
    assert view['origin_verified'] and not view['identity_candidate']
    assert view['identity_reason'] == 'outside_avatar_region'
    for heading in (0, 1):
        gated = PlayerIdentityGate().review(view, {'heading': heading, 'at': 1})
        assert not gated['identity_verified'] and gated['left'] == gated['front'] == 'unknown'


def test_live_feet_and_ground_basis_coexist_without_losing_valid_origin():
    case = next(c for c in CASES if c['source'] == 'identity-regression-1814' and c['image'] == 'left.png')
    view = parse_near_observation(case['raw_output'], player_identity=PROFILE)
    assert view['origin_verified'] and view['identity_candidate']
    assert view['front'] == 'blocked' and view['left'] == 'open'
    result = PlayerIdentityGate().review(view, {'heading': 0, 'at': 1})
    assert result['front'] == result['left'] == 'unknown'  # One view is not identity confirmation.


def test_two_consistent_headings_are_required_and_repeated_same_view_cannot_confirm():
    gate, view = PlayerIdentityGate(), candidate()
    assert not gate.review(view, {'heading': 0, 'at': 1})['identity_verified']
    assert not gate.review(view, {'heading': 0, 'at': 2})['identity_verified']
    confirmed = gate.review(view, {'heading': .6, 'at': 3})
    assert confirmed['identity_verified'] and confirmed['front'] == 'open'
    assert not gate.review(view, {'heading': 1.2, 'at': 30})['identity_verified']
    gate.clear()
    assert not gate.review(view, {'heading': 1.8, 'at': 31})['identity_verified']


@pytest.mark.parametrize('changes', [
    {'observed_race': 'orc'}, {'observed_race': 'unknown'}, {'identity_evidence': ''},
    {'player_box': [600, 500, 800, 800]},
])
def test_wrong_or_unknown_avatar_evidence_cannot_authorize_movement(changes):
    raw = region(schema=4, observed_race='troll', identity_evidence='ears') | changes
    view = parse_near_observation(json.dumps(raw), player_identity=PROFILE)
    gate = PlayerIdentityGate()
    gate.review(view, {'heading': 0, 'at': 1})
    assert not gate.review(view, {'heading': .6, 'at': 2})['identity_verified']


def test_missing_character_profile_or_legacy_model_format_is_not_identity_proof():
    assert profile_from_sample(SimpleNamespace(race_id=8, class_id=3)) == PROFILE
    assert profile_from_sample(SimpleNamespace(race_id=99, class_id=3)) is None
    view = parse_near_observation(json.dumps(region()))
    assert view['origin_verified'] and not view['identity_candidate']
    assert not PlayerIdentityGate().review(view, {'heading': 0, 'at': 1})['identity_verified']


def test_identity_candidate_only_causes_a_stationary_cross_view_look(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    probe.observations = {'right': PlayerIdentityGate().review(candidate(), {'heading': .9, 'at': 1})}
    probe.capture_poses = {'right': {'heading': .9, 'at': 1}}
    result = probe.choose_or_scan(ctx, 2, nav, .9, {'recommendation': 'stop'})
    assert not result.keys and not result.tap and not result.outcome
    assert probe.direction == 'gap_left'
    assert math.isclose(probe.angles['gap_left'], .9 + math.radians(35))
    assert events[-1][0] == 'quest_terrain_identity_look'


def test_scan_consumes_one_picture_without_waiting_for_the_other_side(monkeypatch):
    probe, nav, ctx, events, requests = setup(monkeypatch)
    probe.phase, probe.direction = 'scan', 'right'
    assert not probe.step(ctx, 1, nav, probe.angles['right']).keys
    assert probe.request is not None and len(requests[-1]['pose']) == 1
    probe.step(ctx, 10.6, nav, probe.angles['right'])
    received = [d for n, d in events if n == 'quest_terrain_observation_received']
    assert list(received[-1]['views']) == ['right']
    assert probe.direction == 'left' and probe.request is None


def test_real_latency_second_side_result_is_consumed_after_old_30_second_cutoff(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    probe.escape_started = 14.8
    probe.phase, probe.direction = 'scan', 'right'
    probe.request_started, probe.request_deadline, probe.captured_at = 32, 54, 32
    future = Future()
    probe.request = SimpleNamespace(future=future, close=lambda: None)
    assert probe.step(ctx, 41, nav, probe.angles['right']).outcome is None
    future.set_result(({'right': {'schema': 4, 'feet': None, 'front': 'unknown'}}, {'recommendation': 'stop'}))
    assert probe.step(ctx, 41.7, nav, probe.angles['right']).outcome is None
    assert probe.direction == 'left'
    # The second request fits the total deadline and must not be killed at t=44.8.
    probe.settled_at = 43
    assert not probe.step(ctx, 44, nav, probe.angles['left']).keys
    assert probe.request is not None
    views, _ = probe.request.future.result()
    views['left'].update(schema=4, feet=None)
    result = probe.step(ctx, 53, nav, probe.angles['left'])
    assert not result.keys and result.outcome == 'escape_evidence_insufficient'
    received = [d for n, d in events if n == 'quest_terrain_observation_received']
    assert [list(d['views']) for d in received] == [['right'], ['left']]


def test_budget_does_not_start_a_request_without_time_to_consume_it(monkeypatch):
    probe, nav, ctx, events, requests = setup(monkeypatch)
    probe.phase, probe.direction = 'scan', 'left'
    probe.observation_cost = 11
    result = probe.step(ctx, 49, nav, probe.angles['left'])
    assert result.outcome == 'escape_time_budget' and not requests and not result.keys
    assert events[-2][0] == 'quest_terrain_observation_skipped'


def test_pending_request_and_overall_deadline_both_release_and_cancel(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    closed = []
    probe.phase, probe.direction = 'scan', 'left'
    probe.request = SimpleNamespace(future=Future(), close=lambda: closed.append(True))
    probe.request_deadline = 45
    result = probe.step(ctx, 45, nav, probe.angles['left'])
    assert result.outcome == 'escape_observation_timeout' and not result.keys and closed
    assert probe.step(ctx, 60, nav, probe.angles['left']).outcome == 'probe_budget_exhausted'


def test_completed_first_picture_is_saved_when_second_picture_is_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(terrain, 'DATA_DIR', tmp_path)
    calls = []
    def inspect(paths, **kwargs):
        calls.append([p.name for p in paths])
        if paths[0].stem == 'right':
            raise ValueError('visual request cancelled')
        return [{'image': paths[0].name, 'observation': {'front': 'blocked'}, 'raw_output': 'first result'}]
    monkeypatch.setattr(terrain, 'inspect_images', inspect)
    frame = np.zeros((5, 5, 3), dtype=np.uint8)
    request = terrain.SceneRequest({'left': frame, 'right': frame})
    with pytest.raises(ValueError, match='cancelled'):
        request.future.result(timeout=2)
    report = json.loads((request.evidence_dir / 'report.json').read_text())
    assert calls == [['left.png'], ['right.png']]
    assert report['views']['left']['front'] == 'blocked' and len(report['cases']) == 1
    assert report['error'] == 'visual request cancelled'


def test_live_scene_request_never_treats_geometry_as_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(terrain, 'DATA_DIR', tmp_path)
    view = parse_near_observation(json.dumps(region()))
    monkeypatch.setattr(terrain, 'inspect_images', lambda *_a, **_k: [{'observation': view}])
    gate = PlayerIdentityGate()
    request = terrain.SceneRequest({'confirm': np.zeros((5, 5, 3), dtype=np.uint8)},
        validate=lambda name, v: gate.review(v, {'at': 1, 'heading': 0}))
    views, _ = request.future.result(timeout=2)
    assert views['confirm']['origin_verified'] and not views['confirm']['identity_verified']
    assert views['confirm']['front'] == 'unknown'


def test_normal_wide_camera_is_kept_and_hidden_avatar_starts_nearby_side_look(monkeypatch):
    _, nav, ctx, _, _ = setup(monkeypatch)
    nav = replace(nav, probe_camera_available=True, camera=CameraSample(32, 2, CameraScene.INTERACT,
        CameraScene.INTERACT, 1, CameraResult.DISTANCE_VERIFIED))
    probe = terrain.TerrainDetour(0, nav, 0, QuestTrip(nav.world_size, nav.destination, 'ccw_west'), None)
    assert probe.phase == 'view_check'  # No F6 zoom-in into the same occluders.
    f = Future()
    f.set_result(({'composition': {'usable': True, 'player_visible': False}}, None))
    probe.request, probe.captured_at = SimpleNamespace(future=f, close=lambda: None), 1
    result = probe.step(ctx, 10, nav, 0)
    assert not result.keys and probe.phase == 'scan' and probe.direction in {'left', 'right'}


def test_public_character_facts_must_be_fresh(tmp_path, monkeypatch):
    from test_npc_obstacle import npc
    from bot.perception.observation import ChannelObservation
    run, _, _, _, _ = npc(tmp_path, monkeypatch)
    sample = SimpleNamespace(race_id=8, class_id=3)
    run.observation = SimpleNamespace(combat=ChannelObservation(sample, 1, 10, 10))
    assert run.terrain_identity(11) == PROFILE
    assert run.terrain_identity(12) is None


def test_cross_view_identity_does_not_survive_map_change_or_large_relocation():
    gate, view = PlayerIdentityGate(), candidate()
    pose = {'heading': 0, 'at': 1, 'map_id': 2451, 'position': (.2, .2), 'world_size': (1000, 500)}
    gate.review(view, pose)
    assert not gate.review(view, pose | {'heading': .6, 'at': 2, 'map_id': 99})['identity_verified']
    assert not gate.review(view, pose | {'heading': 1.2, 'at': 3, 'position': (.21, .2)})['identity_verified']


def test_manual_camera_pause_cancels_observation_without_movement(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    nav = replace(nav, camera=CameraSample(32, 2, CameraScene.INTERACT, CameraScene.INTERACT,
                                         1, CameraResult.PAUSED, paused=True))
    closed = []
    probe.request = SimpleNamespace(future=Future(), close=lambda: closed.append(True))
    result = probe.step(ctx, 1, nav, 0)
    assert result.outcome == 'probe_interrupted' and not result.keys and closed


def test_live_request_pipeline_confirms_identity_before_any_visual_step(tmp_path, monkeypatch):
    _, nav, ctx, events, _ = setup(monkeypatch)
    # Restore the real asynchronous request pipeline, substituting only model output/capture.
    # setup replaced SceneRequest; use an undo boundary to recover the production class.
    monkeypatch.undo()
    monkeypatch.setattr(terrain, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(terrain, 'capture_scene', lambda _: np.zeros((8, 8, 3), dtype=np.uint8))
    calls = []
    def inspect(paths, **kwargs):
        assert kwargs['player_identity'] == PROFILE
        calls.append(paths[0].stem)
        return [{'observation': candidate(), 'image': paths[0].name}]
    monkeypatch.setattr(terrain, 'inspect_images', inspect)
    probe = terrain.TerrainDetour(0, nav, 0, QuestTrip(nav.world_size, nav.destination, 'ccw_west'),
                                 None, retreat_first=False, player_identity=PROFILE)
    first_heading = probe.angles['left']
    assert not probe.step(ctx, 1, nav, first_heading).keys
    probe.request.future.result(timeout=2)
    assert not probe.step(ctx, 2, nav, first_heading).keys
    assert probe.phase == 'scan' and probe.direction == 'gap_left'
    second_heading = probe.angles['gap_left']
    assert not probe.step(ctx, 3, nav, second_heading).keys
    probe.request.future.result(timeout=2)
    assert not probe.step(ctx, 4, nav, second_heading).keys
    assert probe.phase == 'confirm'
    assert not probe.step(ctx, 5, nav, second_heading).keys
    probe.request.future.result(timeout=2)
    command = probe.step(ctx, 6, nav, second_heading)
    assert command.keys == {'w'} and probe.confirmed_scene['identity_verified']
    assert calls == ['left', 'gap_left', 'confirm']
