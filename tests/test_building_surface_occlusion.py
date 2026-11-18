"""Surface acceptance is separate from a traversable route or an entry receipt."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.quest_travel import TravelCommand
from bot.perception.building_scene import describe_building_scene
from experiments.route_models.replay_landing_handoff import native
from test_building_access import setup


FIXTURE = Path(__file__).parent / 'fixtures/building-surface-occlusion-20261117'


def cases():
    meta = json.loads(FIXTURE.with_suffix('.json').read_text())
    with np.load(FIXTURE.with_suffix('.npz')) as data:
        for i, case in enumerate(meta['cases']):
            observed = SimpleNamespace(semantic_labels=data[f'{i}_labels'],
                semantic_confidence=data[f'{i}_confidence'], water=data[f'{i}_water'], instances=[])
            yield case, observed, meta['names']


@pytest.mark.parametrize('case,observed,names', list(cases()), ids=lambda x: x.get('frame_id', '') if isinstance(x, dict) else '')
def test_saved_surface_evidence_keeps_roof_unknown_and_ground_distinct(case, observed, names):
    before = {k: v.copy() for k, v in vars(observed).items() if isinstance(v, np.ndarray)}
    result = describe_building_scene(observed, names, camera_view=case['camera_view'])
    assert result['surface'] == case['expected']
    assert not result['executable'] and result['world_coordinates'] is None
    assert not result['entrance_verified']
    assert all(np.array_equal(v, getattr(observed, k)) for k, v in before.items())
    if case['expected'] == 'exterior':
        assert result['support']['exterior'] < .32  # The actual former rejection.
        assert result['surface_evidence']['basis'] == 'nearby_connected_exterior'
        assert describe_building_scene(observed, names)['surface'] == 'unknown'


NAMES = {0: 'person', 1: 'grass', 2: 'roof', 3: 'water'}


def occluded():
    labels = np.zeros((100, 100), np.uint8)
    labels[69:89, 40:60] = 1  # 25% of review zone, central connected ground.
    # No precise character/foot pixel is needed; this center remains occluded.
    labels[75:80, 48:53] = 0
    return labels


def scene(labels, *, camera_view=4, confidence=None):
    observed = SimpleNamespace(semantic_labels=labels, instances=[], water=labels == 3,
        semantic_confidence=np.ones_like(labels, float) if confidence is None else confidence)
    return describe_building_scene(observed, NAMES, camera_view=camera_view)


def test_actor_hides_anchor_but_nearby_continuous_ground_can_support_surface_review():
    result = scene(occluded())
    assert result['surface'] == 'exterior'
    assert result['surface_evidence']['nearby_patch_fraction'] > .8
    assert not result['executable'] and not result['entrance_verified']


@pytest.mark.parametrize('view', [None, 2, 5])
def test_occlusion_rule_requires_the_recorded_overhead_view(view):
    assert scene(occluded(), camera_view=view)['surface'] == 'unknown'


def test_many_separate_ground_specks_are_not_a_connected_local_surface():
    labels = np.zeros((100, 100), np.uint8)
    labels[60:92:3, 25:75] = 1
    labels[:, 30::3] = 0
    result = scene(labels)
    assert result['support']['exterior'] >= .20
    assert result['surface'] == 'unknown'
    assert not result['surface_evidence']['occlusion_requirements']['connected_patch']


def test_large_remote_grass_patch_cannot_explain_occluded_player_surface():
    labels = np.zeros((100, 100), np.uint8)
    labels[60:92, 25:38] = 1
    result = scene(labels)
    assert result['support']['exterior'] >= .20
    assert result['surface'] == 'unknown'
    assert result['surface_evidence']['nearby_patch_fraction'] == 0


def test_tiny_visible_patch_and_low_confidence_are_not_enough():
    labels = np.zeros((100, 100), np.uint8)
    labels[70:80, 40:50] = 1
    assert scene(labels)['surface'] == 'unknown'
    assert scene(occluded(), confidence=np.full((100, 100), .6))['surface'] == 'unknown'
    assert scene(np.zeros((100, 100), np.uint8))['surface'] == 'unknown'


@pytest.mark.parametrize('hazard', [2, 3])
def test_nearby_roof_or_water_veto_despite_plenty_of_grass(hazard):
    labels = occluded()
    labels[60:92, 25:40] = 1
    labels[70:80, 45:55] = hazard
    result = scene(labels)
    assert result['support']['exterior'] > .32
    assert result['surface'] != 'exterior'
    assert not result['surface_evidence']['occlusion_requirements']['no_surface_conflict']


@pytest.mark.parametrize('case,observed,names', [c for c in cases() if c[0]['expected'] == 'exterior'])
def test_recorded_ground_starts_entrance_observation_before_walking(case, observed, names, tmp_path, monkeypatch):
    obj, ctx, _, events, requests, run = setup(tmp_path, monkeypatch)
    nav = native(case['native'])
    obj.step(ctx, 1, nav, 0)
    obj.ground_only = True  # Simulated resumed building intent; not a live receipt.
    obj.step(ctx, 1.6, replace(nav, sequence=(nav.sequence + 1) % 128), 0)
    requests[-1].result = describe_building_scene(observed, names, camera_view=nav.camera.view)
    cmd = obj.step(ctx, 1.7, replace(nav, sequence=(nav.sequence + 2) % 128), 0)
    assert cmd == TravelCommand() and obj.state == 'ground_travel'
    assert obj.points and obj.points[-1].position == nav.position
    run.building_access = obj
    assert run.building_pending
    # At the REAL saved position (~65 yd), entrance search owns the next step.
    started = []
    obj.entry_factory = lambda *args: started.append(args) or SimpleNamespace(close=lambda: None)
    assert obj.step(ctx,1.8,replace(nav,sequence=(nav.sequence+3)%128),0)==TravelCommand()
    assert obj.state == 'entry' and len(started) == 1
    assert started[0][1].position==nav.position and run.trip is None
    assert not any(e == 'quest_building_entered' for e, _ in events)


def test_capture_request_carries_camera_view_and_changed_view_discards_result(tmp_path, monkeypatch):
    obj, ctx, _, _, requests, _ = setup(tmp_path, monkeypatch)
    factory, passed = obj.request_factory, []
    def request(image, config, camera_view, native_outdoors):
        passed.append((camera_view,native_outdoors))
        return factory(image, config, camera_view, native_outdoors)
    obj.request_factory = request
    nav = native(next(c[0]['native'] for c in cases() if c[0]['expected'] == 'exterior'))
    obj.step(ctx, 1, nav, 0)
    obj.step(ctx, 1.6, replace(nav, sequence=(nav.sequence + 1) % 128), 0)
    assert passed == [(4,True)]
    result = obj.step(ctx, 1.7, replace(nav, sequence=(nav.sequence + 2) % 128,
                                      camera=replace(nav.camera, view=2)), 0)
    assert result == TravelCommand() and requests[0].closed and not obj.points
