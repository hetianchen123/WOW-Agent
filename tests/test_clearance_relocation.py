"""20:54 regression: a .63-yard trace is not adequate obstacle clearance."""
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bot.control.ground_trace import GroundPoint
from bot.control.quest_terrain import TerrainDetour
from bot.control.quest_travel import QuestTrip
from test_quest_travel import Flags
from test_terrain_retreat import setup
from test_contact_release import contact
from test_scene_scale import camera


def short_trace(monkeypatch):
    _, nav, ctx, events, requests = setup(monkeypatch)
    trip = QuestTrip(nav.world_size, nav.destination, 'ccw_west')
    rear = replace(nav, position=(nav.position[0], nav.position[1] + .00126), sequence=2)
    trace = SimpleNamespace(backtrack=lambda *_: [GroundPoint(rear.position, 0, 0, nav.height)])
    probe = TerrainDetour(0, nav, 0, trip, None, ground_trace=trace, contact_release=True)
    assert probe.step(ctx, .1, nav, 0).keys == {'s'}
    assert not probe.step(ctx, .3, rear, 0).keys
    probe.step(ctx, .9, rear, 0)
    probe.step(ctx, 1, rear, 0)
    return probe, rear, ctx, events, requests


def occluded(monkeypatch):
    probe, nav, ctx, events, requests = contact(monkeypatch)
    # Begin after a measured first retreat and a normal framing receipt.
    probe.phase = 'scan'
    probe.clearance_total, probe.clearance_pulses = 4, 5
    probe.clearance_trace = [nav.position]
    probe.composition_ok, probe.composition_camera = True, nav.camera
    probe.observations = {'left': {'schema': 5, 'front': 'unknown', 'left': 'unknown',
        'right': 'unknown', 'route_rejection': 'obstacle_is_avatar_box'}}
    return probe, nav, ctx, events, requests


def test_real_short_trace_continues_backward_before_any_model(monkeypatch):
    probe, nav, ctx, events, requests = short_trace(monkeypatch)
    assert probe.phase == 'contact_release'
    assert probe.clearance_total == pytest.approx(.63)
    assert probe.contact_release.target_yards == pytest.approx(3.37)
    assert probe.step(ctx, 1.1, nav, 0).tap == 's'
    assert not requests
    assert any(n == 'quest_clearance_continue' and d['reason'] == 'recent_trace_ended_before_clearance'
               for n, d in events)


def test_trace_plus_extension_counts_actual_total_and_reobserves(monkeypatch):
    probe, nav, ctx, events, requests = short_trace(monkeypatch)
    for i in range(5):
        at = 1.1 + i
        assert probe.step(ctx, at, nav, 0).tap == 's'
        nav = replace(nav, sequence=nav.sequence + 1, position=(nav.position[0],nav.position[1]+.0016))
        probe.step(ctx, at+.7, nav, 0)
    assert probe.phase == 'clearance_check' and not requests
    assert probe.clearance_total == pytest.approx(4.63)
    assert probe.retreat_pulses == 6 and not probe.escape_checked
    report = next(d for n, d in events if n == 'quest_terrain_retreat_result')
    assert report['yards'] == 4.63 and not report['clearance_verified']


def test_wide_camera_replans_directly_after_clearance_without_extra_composition_wait(monkeypatch):
    probe, nav, ctx, _, requests = contact(monkeypatch)
    nav = replace(nav, camera=camera(), probe_camera_available=True)
    for i in range(5):
        at = .1 + i
        assert probe.step(ctx, at, nav, 0).tap == 's'
        nav = replace(nav, sequence=nav.sequence+1, position=(nav.position[0],nav.position[1]+.0016))
        probe.step(ctx, at+.7, nav, 0)
    assert probe.phase == 'scan' and not requests
    command = probe.step(ctx, 7, nav, probe.angles[probe.direction])
    assert not command.keys and not command.tap
    assert requests[-1]['phase'] == 'scan'
    assert requests[-1]['player_identity']['screen_anchor']['profile'] == 'centered_view_2'


def test_secondary_alignment_times_out_without_moving_in_scan_direction(monkeypatch):
    probe, nav, ctx, _, _ = occluded(monkeypatch)
    probe.choose_or_scan(ctx, 25, nav, 1, {'recommendation': 'stop'})
    command = probe.step(ctx, 32.1, nav, 1)
    assert command.outcome == 'escape_time_budget' and not command.tap and not command.keys


def test_stale_feedback_during_extension_stops_before_any_model(monkeypatch):
    probe, nav, ctx, _, requests = short_trace(monkeypatch)
    assert probe.step(ctx, 1.1, nav, 0).tap == 's'
    command = probe.step(ctx, 2.7, nav, 0)
    assert command.outcome == 'probe_telemetry_stale' and not command.keys and not requests


def test_first_unusable_view_relocates_instead_of_waiting_for_second_model(monkeypatch):
    probe, nav, ctx, events, requests = occluded(monkeypatch)
    result = probe.choose_or_scan(ctx, 25, nav, 1, {'recommendation': 'stop'})
    assert not result.keys and probe.phase == 'clearance_align'
    assert probe.clearance_retry_used and not probe.observations and not requests
    # We are looking sideways. Restore the escape heading before any S.
    result = probe.step(ctx, 25.4, nav, 1)
    assert result.tap in {'left','right'} and not result.keys
    assert not probe.step(ctx, 26.1, nav, 0).tap
    assert probe.phase == 'contact_release'
    assert probe.step(ctx, 26.2, nav, 0).tap == 's'


def test_second_retreat_preserves_camera_then_gets_new_route_image(monkeypatch):
    probe, nav, ctx, events, requests = occluded(monkeypatch)
    probe.choose_or_scan(ctx, 25, nav, 0, {'recommendation': 'stop'})
    probe.step(ctx, 25.4, nav, 0)
    for i in range(3):
        at = 25.5 + i
        assert probe.step(ctx, at, nav, 0).tap == 's'
        nav = replace(nav, sequence=nav.sequence+1, position=(nav.position[0],nav.position[1]+.0016))
        probe.step(ctx, at+.7, nav, 0)
    assert probe.phase == 'scan' and probe.clearance_total == pytest.approx(6.4)
    assert not probe.observations and probe.anchor.position == nav.position
    # Replan with a new exposure at the new position; no old forward instruction survives.
    result = probe.step(ctx, 30, nav, probe.angles[probe.direction])
    assert not result.keys and requests[-1]['pose'][probe.direction]['position'] == nav.position
    probe.request.close()
    probe.request = None
    probe.observations = {'right': {'front': 'unknown'}}
    probe.choose_or_scan(ctx, 31, nav, 0, {'recommendation': 'stop'})
    assert probe.phase != 'clearance_align'  # At most one additional relocation.


@pytest.mark.parametrize('change', [
    {'clearance_total': 0}, {'clearance_total': 6}, {'clearance_retry_used': True},
    {'clearance_pulses': 12}, {'allow_clearance': False}, {'escape_checked': True},
    {'failed': {'back'}},
])
def test_no_unbounded_or_blocked_relocation(monkeypatch, change):
    probe, nav, ctx, _, _ = occluded(monkeypatch)
    for key, value in change.items(): setattr(probe, key, value)
    assert not probe.widen_clearance(ctx, 25, nav)


def test_does_not_spend_last_seconds_moving_without_time_to_reobserve(monkeypatch):
    probe, nav, ctx, _, _ = occluded(monkeypatch)
    assert not probe.widen_clearance(ctx, 49, nav)


def test_visible_route_keeps_priority_over_retreat(monkeypatch):
    probe, nav, ctx, _, _ = occluded(monkeypatch)
    probe.observations['left']['right'] = 'open'
    assert not probe.widen_clearance(ctx, 25, nav)


@pytest.mark.parametrize('flags', [Flags.FALLING, Flags.IN_COMBAT, Flags.FLYING])
def test_secondary_clearance_cancels_on_unsafe_state(monkeypatch, flags):
    probe, nav, ctx, _, _ = occluded(monkeypatch)
    probe.choose_or_scan(ctx, 25, nav, 0, {'recommendation': 'stop'})
    command = probe.step(ctx, 25.4, replace(nav, flags=nav.flags|flags), 0)
    assert command.outcome == 'probe_interrupted' and not command.tap and not command.keys


def test_secondary_backward_zero_motion_never_repeats(monkeypatch):
    probe, nav, ctx, _, _ = occluded(monkeypatch)
    probe.choose_or_scan(ctx, 25, nav, 0, {'recommendation': 'stop'})
    probe.step(ctx, 25.4, nav, 0)
    assert probe.step(ctx, 25.5, nav, 0).tap == 's'
    probe.step(ctx, 26.3, replace(nav, sequence=2), 0)
    assert 'back' in probe.failed and not probe.widen_clearance(ctx, 27, nav)
