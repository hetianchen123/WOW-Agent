from dataclasses import replace
import math

from bot.control.quest_terrain import TerrainDetour
from bot.control.quest_travel import QuestTrip
from bot.perception.travel_channel import CameraSample, CameraScene as Scene, CameraResult as Result
from test_terrain_retreat import setup
from test_quest_travel import sample, Flags


def test_probe_view_waits_for_new_applied_receipt_then_releases_f6(monkeypatch):
    _, nav, ctx, events, _ = setup(monkeypatch)
    camera = CameraSample(12, 2, Scene.INTERACT, Scene.INTERACT, 1, Result.DISTANCE_VERIFIED)
    nav = replace(nav, probe_camera_available=True, camera=camera)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    probe = TerrainDetour(0, nav, 0, trip, None)
    assert probe.step(ctx, 0, nav, 0).keys == {"f6"}
    receipt = replace(nav, camera=replace(camera, applied=Scene.PROBE, request=2, zoom=24))
    assert probe.step(ctx, 1, receipt, 0).keys == {"f6"}  # duplicate nav frame is not a receipt
    assert not probe.step(ctx, 2, replace(receipt, sequence=2), 0).keys
    assert probe.phase == "view_check" and probe.probe_view_used
    assert events[-1][1]["feet_verified"] is False


def test_probe_view_timeout_releases_and_does_not_repeat(monkeypatch):
    _, nav, ctx, _, _ = setup(monkeypatch)
    nav = replace(nav, probe_camera_available=True,
        camera=CameraSample(32, 2, Scene.INTERACT, Scene.INTERACT, 0, Result.IDLE))
    probe = TerrainDetour(0, nav, 0, QuestTrip(nav.world_size, nav.destination, "ccw_west"), None)
    probe.step(ctx, 0, nav, 0)
    assert not probe.step(ctx, 8.7, nav, 0).keys
    assert probe.phase != "view_prepare"


def test_side_gap_only_authorizes_stationary_look(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    probe.observations = {"left": {"schema": 2, "feet": [500, 800], "front": "blocked", "left": "open"}}
    probe.capture_poses = {"left": {"heading": 1.0}}
    command = probe.choose_or_scan(ctx, 1, nav, 0, {"recommendation": "stop"})
    assert not command.keys and probe.direction == "gap_left"
    assert math.isclose(probe.angles["gap_left"], 1 + math.radians(25))
    assert events[-1][1]["movement_authorized"] is False


def walking_probe(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    probe.phase = "walk"
    probe.direction = "left"
    probe.angles["left"] = 0
    probe.confirmed_scene = {"schema": 2, "feet": [500, 800], "front": "open"}
    probe.walk_at, probe.walk_origin, probe.walk_sequence = 1, nav.position, nav.sequence
    probe.short_probe_origin, probe.short_probe_steps = nav.position, 1
    return probe, nav, ctx, events


def test_short_corridor_releases_then_requires_fresh_image_after_moving(monkeypatch):
    probe, nav, ctx, events = walking_probe(monkeypatch)
    assert probe.step(ctx, 1.1, nav, 0).keys == {"w"}
    moved = replace(nav, position=(.2, .198), sequence=2)  # one yard north
    assert not probe.step(ctx, 1.3, moved, 0).keys
    command = probe.step(ctx, 1.6, moved, 0)
    assert not command.keys and command.outcome is None and probe.phase == "confirm"
    assert probe.anchor.position == moved.position and not probe.observations


def test_short_step_duplicate_position_sequence_never_counts_as_clearance(monkeypatch):
    probe, nav, ctx, _ = walking_probe(monkeypatch)
    moved = replace(nav, position=(.2, .196))
    assert probe.step(ctx, 1.6, moved, 0).outcome is None
    assert probe.step(ctx, 2.6, moved, 0).outcome == "probe_telemetry_stale"


def test_probe_camera_combat_interrupt_returns_no_held_key(monkeypatch):
    _, nav, ctx, _, _ = setup(monkeypatch)
    nav = replace(nav, probe_camera_available=True,
        camera=CameraSample(32, 2, Scene.INTERACT, Scene.INTERACT, 0, Result.IDLE))
    probe = TerrainDetour(0, nav, 0, QuestTrip(nav.world_size, nav.destination, "ccw_west"), None)
    probe.step(ctx, 0, nav, 0)
    result = probe.step(ctx, .1, replace(nav, flags=nav.flags | Flags.IN_COMBAT), 0)
    assert not result.keys and result.outcome == "probe_interrupted"


def test_no_identified_player_is_reported_separately_from_blocked_passage(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    probe.observations = {name: {"schema": 2, "feet": None, "front": "unknown"}
                          for name in probe.angles}
    result = probe.choose_or_scan(ctx, 1, nav, 0, {"recommendation": "stop"})
    assert not result.keys and result.outcome == "escape_evidence_insufficient"


def test_one_near_rear_observation_does_not_authorize_four_yards(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    probe.step(ctx, 1, nav, math.pi)
    views, _ = probe.request.future.result()
    views["retreat"].update(schema=2, feet=[500, 800])
    probe.step(ctx, 1.2, nav, math.pi)
    assert probe.retreat_pulse_limit == 8
    probe.step(ctx, 1.8, nav, math.pi)
    assert probe.step(ctx, 2.5, nav, 0).keys == {"s"}
    moved = replace(nav, position=(.2, .201), sequence=2)
    assert not probe.step(ctx, 2.8, moved, 0).keys
    assert not probe.step(ctx, 3.4, moved, 0).keys
    assert probe.phase == "retreat_observe" and probe.retreat_pulses == 1
    assert events[-1][0] == "quest_terrain_retreat_step"
    assert probe.anchor.position == moved.position and not probe.observations


def composition_probe(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    nav = replace(nav, camera_restore_available=True,
        camera=CameraSample(24, 2, Scene.INTERACT, Scene.PROBE, 3, Result.DISTANCE_VERIFIED))
    probe.phase, probe.probe_view_used = 'view_check', True
    return probe, nav, ctx, events


def give_composition(probe, ctx, nav, time, feet):
    from concurrent.futures import Future
    from types import SimpleNamespace
    probe.captured_at = time
    future = Future()
    future.set_result(({'composition': {'usable': feet is not None, 'view': 'normal' if feet else 'occluded'}}, None))
    probe.request = SimpleNamespace(future=future, close=lambda: None)
    return probe.step(ctx, time + .1, nav, 0)


def test_good_composition_permits_observation_not_forward_movement(monkeypatch):
    probe, nav, ctx, events = composition_probe(monkeypatch)
    command = give_composition(probe, ctx, nav, 1, [500, 700])
    assert not command.keys and not command.tap and command.outcome is None
    assert probe.phase == 'retreat_observe'
    assert events[-1][1]['usable'] and not events[-1][1]['movement_authorized']


def test_bad_composition_restores_once_rechecks_then_stops_without_six_view_scan(monkeypatch):
    probe, nav, ctx, events = composition_probe(monkeypatch)
    command = give_composition(probe, ctx, nav, 1, None)
    assert not command.keys and not command.tap and probe.phase == 'view_restore'
    assert probe.step(ctx, 1.2, nav, 0).keys == {'f8'}
    receipt = replace(nav, camera=replace(nav.camera, applied=Scene.INTERACT, request=4, zoom=32))
    assert probe.step(ctx, 2, receipt, 0).keys == {'f8'}  # duplicate sequence
    receipt = replace(receipt, sequence=2)
    assert not probe.step(ctx, 2.1, receipt, 0).keys and probe.phase == 'view_recheck'
    assert not probe.probe_view_used  # baseline restoration acknowledged
    command = give_composition(probe, ctx, receipt, 3, None)
    assert not command.keys and not command.tap and command.outcome == 'camera_composition_unresolved'
    assert not probe.views and not probe.observations
    assert sum(name == 'quest_terrain_view_requested' for name, _ in events) == 1


def test_restored_good_frame_resumes_observation_with_old_views_discarded(monkeypatch):
    probe, nav, ctx, _ = composition_probe(monkeypatch)
    probe.observations['left'] = {'front': 'open'}
    give_composition(probe, ctx, nav, 1, None)
    probe.step(ctx, 1.2, nav, 0)
    receipt = replace(nav, sequence=2, camera=replace(nav.camera, applied=Scene.INTERACT, request=4))
    probe.step(ctx, 2, receipt, 0)
    command = give_composition(probe, ctx, receipt, 3, [500, 700])
    assert command.outcome is None and not command.keys and probe.phase == 'retreat_observe'
    assert not probe.observations


def test_composition_recovery_timeout_releases_and_cannot_move(monkeypatch):
    probe, nav, ctx, _ = composition_probe(monkeypatch)
    give_composition(probe, ctx, nav, 1, None)
    probe.step(ctx, 1.2, nav, 0)
    command = probe.step(ctx, 10, nav, 0)
    assert not command.keys and command.outcome == 'camera_restore_unconfirmed'


def test_composition_recovery_combat_interrupt_releases_f8(monkeypatch):
    probe, nav, ctx, _ = composition_probe(monkeypatch)
    give_composition(probe, ctx, nav, 1, None)
    probe.step(ctx, 1.2, nav, 0)
    command = probe.step(ctx, 1.3, replace(nav, flags=nav.flags | Flags.IN_COMBAT), 0)
    assert not command.keys and command.outcome == 'probe_interrupted'
