"""Rendered 3-D geometry and native-feedback simulations, not game validation."""
from dataclasses import replace

import numpy as np
import pytest

from bot.perception.automatic_ground import AutomaticGround
from bot.perception.calibration_cache import load_cache, save_cache
from bot.perception.ground_projection import ProjectionError
from bot.perception.surface_height import height_matches
from bot.perception.travel_channel import TravelFlags
from experiments.route_models.simulate_inclined_surface import frames, evaluate
from test_segmented_runtime import controller, ready
from test_fluent_detour import prepared


@pytest.fixture(scope='module')
def inclined():
    rows = list(frames((.6, 0)))
    cal = AutomaticGround()
    for frame, rgb, masks, _ in rows:
        projection, proof = cal.update(frame, rgb, masks, now=frame.captured_at+.1)
    assert projection is not None
    return rows, cal, proof


@pytest.mark.parametrize('slope', [(0, 0), (.25, 0), (.6, 0), (.35, .35)])
@pytest.mark.parametrize('public_height', [False, True])
def test_physical_camera_recovers_inclined_plane_without_clicked_points(slope, public_height):
    result = evaluate(slope, public_height=public_height)
    assert result['ready'], result
    # Camera ground truth is never used in the estimator or correspondence fit.
    assert result['independent_truth_error_yards'] < .01
    proof = result['proof']
    assert proof['manual_points'] == 0
    assert not set(map(tuple, proof['fit_frames'])) & set(map(tuple, proof['validation_frames']))
    height = proof['surface_height']
    assert height['level_id'] is None
    if public_height and any(slope):
        assert height['gradient'] == pytest.approx(slope)
        assert height['variation_observed'] is True
    else:
        # Fixed public Z cannot prove a flat floor, even on the rendered slope.
        assert height['variation_observed'] is False and height['gradient'] is None


def test_independent_height_disagreement_cannot_be_fitted_away(inclined):
    rows, _, _ = inclined
    cal = AutomaticGround()
    for frame, rgb, masks, _ in rows[:5]:
        cal.update(frame, rgb, masks, now=frame.captured_at+.1)
    original = cal.height_gradient.copy()
    frame, rgb, masks, _ = rows[5]
    frame = replace(frame, height=frame.height+.2)
    with pytest.raises(ProjectionError, match='auto_independent_height_error'):
        cal.update(frame, rgb, masks, now=frame.captured_at+.1)
    assert np.array_equal(cal.height_gradient, original)
    assert cal.reference is None and not cal.validation


def test_rejected_height_fit_does_not_leave_a_route_ready_plane(inclined):
    rows, _, _ = inclined
    cal = AutomaticGround()
    for frame, rgb, masks, _ in rows[:3]:
        cal.update(frame, rgb, masks, now=frame.captured_at+.1)
    # Each movement alone is bounded, but together imply an excessive slope.
    frame, rgb, masks, _ = rows[3]
    cal.update(replace(frame, height=.55), rgb, masks, now=frame.captured_at+.1)
    frame, rgb, masks, _ = rows[4]
    with pytest.raises(ProjectionError, match='auto_surface_height_fit_invalid'):
        cal.update(replace(frame, height=1.1), rgb, masks, now=frame.captured_at+.1)
    assert cal.matrix is None and cal.height_gradient is None and cal.reference is None


@pytest.mark.parametrize('phase', ['orbit', 'translation'])
def test_height_jump_is_not_relabelled_as_a_new_stationary_camera_sample(inclined, phase):
    rows, _, _ = inclined
    cal = AutomaticGround()
    count = 1 if phase == 'orbit' else 3
    for frame, rgb, masks, _ in rows[:count]:
        cal.update(frame, rgb, masks, now=frame.captured_at+.1)
    frame, rgb, masks, _ = rows[count]
    reason = 'auto_orbit_player_moved' if phase == 'orbit' else 'auto_surface_height_discontinuity'
    with pytest.raises(ProjectionError, match=reason):
        cal.update(replace(frame, height=frame.height+2), rgb, masks, now=frame.captured_at+.1)
    assert cal.reference is None


def test_rebase_allows_measured_rise_but_remeasures_a_different_surface(inclined):
    rows, cal, _ = inclined
    # A previously seen view is now a fresh simulated observation, .55 yd up
    # the slope; this exercises re-registration rather than a cached success.
    frame, rgb, masks, _ = rows[3]
    frame = replace(frame, frame_id='fresh-rise', captured_at=110)
    projection, proof = cal.update(frame, rgb, masks, now=110.1)
    assert projection is not None and proof['current_frame'] == 'fresh-rise'
    # Use an independent calibrator so this test never mutates the fixture.
    other = AutomaticGround()
    for f, image, observed, _ in rows:
        other.update(f, image, observed, now=f.captured_at+.1)
    projection, proof = other.update(replace(frame, height=frame.height+.5), rgb, masks, now=110.1)
    assert projection is None and proof['stage'] == 'orbit'
    assert other.matrix is None and not other.validation


@pytest.mark.parametrize('fault', ['height', 'floor'])
def test_cache_replays_slope_evidence_and_rejects_changed_surface(inclined, tmp_path, fault):
    rows, cal, _ = inclined
    path = tmp_path/'inclined.npz'
    save_cache(path, cal, created_at=1000, provenance='synthetic_3d_camera')
    frame, rgb, masks, _ = rows[3]
    frame = replace(frame, frame_id='new-process', captured_at=2000)
    restored = load_cache(path, frame, now=2000, wall_time=1100)
    assert restored.projection.frame.frame_id != frame.frame_id
    p, proof = restored.update(frame, rgb, masks, now=2000.1)
    assert p is not None and proof['surface_height']['gradient'] == pytest.approx([.6, 0])
    bad = replace(frame, height=frame.height+.5) if fault == 'height' else replace(frame, floor_id='another-surface')
    with pytest.raises(ProjectionError, match='scene_changed'):
        load_cache(path, bad, now=2000, wall_time=1100)


def test_calibration_pulse_accepts_bounded_rise_only_after_new_native_receipt(tmp_path, monkeypatch):
    obj, ctx, nav, requests = controller(tmp_path, monkeypatch)
    nav, heading = ready(obj, ctx, nav)
    requests[0].result.update(kind='calibration_probe', calibration={'stage': 'translation'},
                              key='w', hold_s=.07, metric_route=False)
    assert obj.step(ctx, 1.15, nav, heading).tap is None
    assert obj.step(ctx, 1.2, nav, heading).tap == 'w'
    moved = replace(nav, position=(nav.position[0]+.00055, nav.position[1]), height=nav.height+.33)
    cmd = obj.step(ctx, 1.81, moved, heading)
    assert not cmd.outcome and not cmd.tap
    assert obj.calibration_pulse is not None and not obj.motion_history
    fresh = replace(moved, sequence=nav.sequence+1)
    cmd = obj.step(ctx, 2, fresh, heading)
    assert not cmd.outcome and not cmd.tap and obj.calibration_pulse is None
    assert obj.motion_history[-1]['height_delta'] == pytest.approx(.33)
    assert obj.total == pytest.approx(.55)
    # Next step reobserves; the probe receipt itself supplies no calibrated route.
    assert obj.step(ctx, 2.1, fresh, heading).tap is None and obj.pending is not None


@pytest.mark.parametrize('height,flags', [(1., 0), (.33, TravelFlags.FALLING)])
def test_calibration_unsafe_height_or_falling_does_not_continue(tmp_path, monkeypatch, height, flags):
    obj, ctx, nav, _ = controller(tmp_path, monkeypatch)
    obj.calibration_pulse = (1, nav.position, nav.sequence, 'w', nav.height, 1.5*np.pi)
    fresh = replace(nav, sequence=nav.sequence+1, flags=nav.flags|flags,
                    position=(nav.position[0]+.00055, nav.position[1]), height=nav.height+height)
    cmd = obj.step(ctx, 1.7, fresh, 1.5*np.pi)
    assert cmd.outcome == ('probe_interrupted' if flags else 'probe_height_drop')
    assert not cmd.tap and not cmd.keys and not obj.motion_history and obj.total == 0


def test_measured_slope_continues_prefix_then_reobserves_without_extra_model_call(inclined, tmp_path, monkeypatch):
    obj, ctx, nav, heading, requests = prepared(tmp_path, monkeypatch)
    requests[0].result['registration']['surface_height'] = inclined[2]['surface_height']
    origin, height = nav.position, nav.height
    assert obj.step(ctx, 1.3, nav, heading).tap == 'w'
    first = replace(nav, sequence=3, position=(origin[0]+.0005, origin[1]), height=height+.3)
    assert obj.step(ctx, 1.6, first, heading).tap is None and obj.total == 0
    first = replace(first, sequence=4)
    assert not obj.step(ctx, 1.7, first, heading).outcome
    assert obj.step(ctx, 1.75, first, heading).tap == 'w'
    last = replace(first, sequence=5, position=(origin[0]+.0007, origin[1]), height=height+.42)
    assert not obj.step(ctx, 2.05, last, heading).outcome
    last = replace(last, sequence=6)
    assert not obj.step(ctx, 2.15, last, heading).outcome
    assert obj.plan is None and obj.total == pytest.approx(.7) and len(requests) == 1
    assert obj.step(ctx, 2.4, last, heading).tap is None and obj.pending is not None


@pytest.mark.parametrize('kind', ['legacy', 'invalid_model', 'falling', 'drop'])
def test_route_height_needs_valid_current_slope_and_ground_state(inclined, tmp_path, monkeypatch, kind):
    obj, ctx, nav, heading, requests = prepared(tmp_path, monkeypatch)
    proof = dict(inclined[2]['surface_height'])
    if kind == 'invalid_model': proof['validation_movements'] = 0
    if kind != 'legacy': requests[0].result['registration']['surface_height'] = proof
    assert obj.step(ctx, 1.3, nav, heading).tap == 'w'
    fresh = replace(nav, sequence=3, position=(nav.position[0]+.0005, nav.position[1]),
                    height=nav.height+(-1 if kind == 'drop' else .3),
                    flags=nav.flags|(TravelFlags.FALLING if kind == 'falling' else 0))
    cmd = obj.step(ctx, 1.6, fresh, heading)
    assert cmd.outcome == ('probe_interrupted' if kind == 'falling' else 'probe_height_drop')
    assert not cmd.tap and not cmd.keys and obj.total == 0


def test_prefix_cannot_accumulate_height_error_across_individually_small_steps(inclined, tmp_path, monkeypatch):
    obj, ctx, nav, heading, requests = prepared(tmp_path, monkeypatch)
    requests[0].result['registration']['surface_height'] = inclined[2]['surface_height']
    origin, height = nav.position, nav.height
    obj.step(ctx, 1.3, nav, heading)
    first = replace(nav, sequence=3, position=(origin[0]+.0003, origin[1]), height=height+.28)
    obj.step(ctx, 1.6, first, heading)
    first = replace(first, sequence=4)
    assert not obj.step(ctx, 1.7, first, heading).outcome
    assert obj.step(ctx, 1.75, first, heading).tap == 'w'
    second = replace(first, sequence=5, position=(origin[0]+.0006, origin[1]), height=height+.56)
    assert not obj.step(ctx, 2.05, second, heading).tap
    # A second distinct sample confirms the bounded position, then invalidates
    # the prefix. No third step can use its progressively diverging height fit.
    cmd = obj.step(ctx, 2.15, replace(second, sequence=6), heading)
    assert not cmd.outcome and not cmd.tap and obj.plan is None
    assert obj.total == pytest.approx(.6) and not obj.motion_history


def test_bounded_surface_change_remeasures_without_resetting_navigation_budget(inclined, tmp_path, monkeypatch):
    obj, ctx, nav, heading, requests = prepared(tmp_path, monkeypatch)
    requests[0].result['registration']['surface_height'] = inclined[2]['surface_height']
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    old_session, deadline = obj.session_id, obj.deadline
    assert obj.step(ctx, 1.3, nav, heading).tap == 'w'
    # Horizontal feedback follows the planned route, but no longer the fitted
    # rise. This can be a landing; it cannot identify a floor or certify stairs.
    fresh = replace(nav, sequence=3, position=(nav.position[0]+.0005, nav.position[1]))
    assert not obj.step(ctx, 1.6, fresh, heading).outcome
    fresh = replace(fresh, sequence=4)
    cmd = obj.step(ctx, 1.7, fresh, heading)
    assert not cmd.outcome and not cmd.tap
    assert obj.plan is None and obj.window is None and obj.session_id != old_session
    assert obj.deadline == deadline and obj.total == pytest.approx(.5) and obj.pulses == 1
    assert not obj.step(ctx, 2, fresh, heading).tap and obj.pending is None
    assert not obj.step(ctx, 2.3, fresh, heading).tap and obj.pending is not None
    changes = [data for name, data in events if name == 'quest_segmented_surface_changed']
    assert len(changes) == 1 and changes[0]['requires_new_measurement'] is True
    assert changes[0]['level_id'] is None


def test_malformed_height_model_rejected_before_first_input(tmp_path, monkeypatch):
    obj, ctx, nav, requests = controller(tmp_path, monkeypatch)
    nav, heading = ready(obj, ctx, nav)
    requests[0].result['registration']['surface_height'] = {'gradient': [0, 0]}
    cmd = obj.step(ctx, 1.2, nav, heading)
    assert cmd.outcome == 'segmentation_plan_invalid' and not cmd.tap and obj.pulses == 0


def test_unknown_height_model_cannot_relax_the_legacy_bound():
    assert height_matches([.5, 0], .2, None)
    assert not height_matches([.5, 0], .3, None)
    assert not height_matches([.5, 0], .2, {})


def test_building_worker_returns_a_measured_route_on_the_new_local_slope(inclined, monkeypatch):
    from experiments.route_models import segmentation_worker as worker
    cal = AutomaticGround()
    # Known simulation labels test the measurement/planning handoff. They are
    # not predictions from SAM or evidence of real scene recognition.
    scene = dict(schema=1, surface='unknown', executable=False, doorways=[],
        layout=dict(schema=1, coordinate_space='current_image', size=[800, 600],
                    floor_regions=[{'box': [0, 0, 800, 600]}], exterior_regions=[],
                    connectors=[], portals=[]))
    job = dict(goal_mode='building_search', building_scene=scene, quest_id=12345,
               phase='READY', destination=[.41, .5], convention='ccw_west', strafe_verified=True)
    for frame, rgb, masks, _ in inclined[0]:
        monkeypatch.setattr(worker.time, 'monotonic', lambda: frame.captured_at+.1)
        result = worker.building_job(cal, frame, rgb, masks, job)
    assert result['kind'] == 'route' and result['calibration_source'] == 'automatic_native_motion'
    assert result['registration']['surface_height']['gradient'] == pytest.approx([.6, 0])
    assert result['automatic_goal']['basis'] == 'building_task_search_region'
    assert result['automatic_goal']['level_id'] is None and result['executable'] is False
    assert result['movement_budget_yards'] <= 2
    assert result['next_segment_world'][0] == pytest.approx(frame.player_position)
