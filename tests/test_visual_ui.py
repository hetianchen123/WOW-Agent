"""Verified visibility controls pixel exclusions, never world movement itself."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.quest_travel import TravelCommand
from bot.perception.building_scene import building_ui_exclusion, building_ground_measurement
from test_building_access import setup
from test_segmented_runtime import controller, ready
from test_fluent_detour import prepared, feedback


@pytest.mark.parametrize('flags', [None, -1, 16, True, '15', 1.0])
def test_unknown_visibility_retains_the_existing_exclusions(flags):
    for full in (False, True):
        assert np.array_equal(building_ui_exclusion((100, 100), full_window=full),
            building_ui_exclusion((100, 100), full_window=full, hidden_ui_flags=flags))


@pytest.mark.parametrize('bit,point', [(1, (85, 50)), (2, (50, 85)),
                                    (4, (50, 25)), (8, (70, 23))])
def test_only_the_confirmed_group_area_is_freed(bit, point):
    previous = building_ui_exclusion((100, 100), full_window=True)
    current = building_ui_exclusion((100, 100), full_window=True, hidden_ui_flags=bit)
    assert previous[point] and not current[point]
    assert not np.any(current & ~previous)
    # Telemetry, minimap and title survive even when every supported group hides.
    all_hidden = building_ui_exclusion((100, 100), full_window=True, hidden_ui_flags=15)
    for keep in [(50, 10), (95, 10), (15, 90), (1, 50)]:
        assert all_hidden[keep]


def test_overlapping_crop_hud_needs_both_group_receipts():
    for flags in (2, 4, 8):
        mask = building_ui_exclusion((100, 100), hidden_ui_flags=flags)
        assert mask[70, 10] and mask[70, 98]
    mask = building_ui_exclusion((100, 100), hidden_ui_flags=12)
    assert not mask[70, 10] and mask[70, 98]
    mask = building_ui_exclusion((100, 100), hidden_ui_flags=10)
    assert mask[70, 10] and not mask[70, 98]


def test_revealed_ground_reaches_measurement_but_forbidden_pixels_do_not():
    # Synthetic ground patch where the action bar used to cover the crop.
    ground = np.zeros((100, 100), bool); ground[91:, 30:70] = True
    forbidden = np.zeros_like(ground); forbidden[96:, 30:70] = True
    observed = SimpleNamespace(ground=ground, forbidden=forbidden,
                               semantic_confidence=np.ones_like(ground, float))
    image = np.zeros((100, 100, 3), np.uint8)
    assert building_ground_measurement(image, observed)['ground_pixels'] == 0
    measured = building_ground_measurement(image, observed, hidden_ui_flags=1)
    assert measured['ground_pixels'] == 5*40
    assert measured['translation_authorized'] is False


@pytest.mark.parametrize('hidden', [0, 1, 15])
def test_ui_change_cancels_pending_route_and_reobserves_with_new_flags(tmp_path, monkeypatch, hidden):
    obj, ctx, nav, requests = controller(tmp_path, monkeypatch)
    nav, heading = ready(obj, ctx, nav)
    assert len(requests) == 1
    session, deadline, calls = obj.session_id, obj.deadline, obj.calls
    changed = replace(nav, visual_ui_hidden=hidden, sequence=nav.sequence+1)
    assert obj.step(ctx, 1.2, changed, heading) == TravelCommand()
    assert requests[0].closed and obj.request is None and obj.plan is None
    assert obj.session_id != session and obj.deadline == deadline and obj.calls == calls
    obj.overhead = SimpleNamespace(step=lambda *_: None, used=True)
    assert obj.step(ctx, 1.3, changed, heading) == TravelCommand()
    assert len(requests) == 1
    changed = replace(changed, sequence=changed.sequence+1)
    assert obj.step(ctx, 1.8, changed, heading) == TravelCommand()
    assert obj.step(ctx, 1.9, replace(changed, sequence=changed.sequence+1), heading) == TravelCommand()
    assert len(requests) == 2 and requests[1].job['visual_ui_hidden'] == hidden
    assert requests[1].frame.frame_id != requests[0].frame.frame_id


def test_ui_change_finishes_pending_movement_receipt_before_discarding_route(tmp_path, monkeypatch):
    obj, ctx, nav, heading, requests = prepared(tmp_path, monkeypatch)
    assert obj.step(ctx, 1.3, nav, heading).tap == 'w'
    nav = replace(nav, visual_ui_hidden=1)
    nav, receipt = feedback(obj, ctx, nav, heading, (nav.position[0]+.0005, nav.position[1]))
    assert receipt == TravelCommand() and obj.total == pytest.approx(.5)
    assert obj.step(ctx, 1.75, nav, heading) == TravelCommand()
    assert obj.plan is None and obj.pulse is None and obj.pulses == 1
    assert obj.visual_ui_hidden == 1 and len(requests) == 1


@pytest.mark.parametrize('before,after', [(None, 1), (1, 0), (1, None)])
def test_building_review_rejects_a_result_from_previous_ui_state(tmp_path, monkeypatch, before, after):
    obj, ctx, nav, _, pending, _ = setup(tmp_path, monkeypatch)
    nav = replace(nav, visual_ui_hidden=before)
    original = obj.request_factory
    received = []
    def request(*args, **kwargs):
        received.append(kwargs.get('visual_ui_hidden'))
        return original(*args)
    obj.request_factory = request
    assert obj.step(ctx, 1, nav, 0) == TravelCommand()
    nav = replace(nav, sequence=nav.sequence+1)
    assert obj.step(ctx, 1.6, nav, 0) == TravelCommand()
    assert len(pending) == 1 and received == [before]
    nav = replace(nav, visual_ui_hidden=after, sequence=nav.sequence+1)
    assert obj.step(ctx, 1.7, nav, 0) == TravelCommand()
    assert pending[0].closed and not obj.points and obj.state == 'review'
    assert obj.step(ctx, 1.8, nav, 0) == TravelCommand()
    assert obj.step(ctx, 2.4, replace(nav, sequence=nav.sequence+1), 0) == TravelCommand()
    assert len(pending) == 2 and received == [before, after]
