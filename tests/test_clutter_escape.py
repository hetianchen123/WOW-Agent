"""Escape has its own evidence, movement budget and route rejoin contract."""
import json
import math
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.control.ground_trace import GroundTrace
from bot.control.quest_terrain import TerrainDetour
from bot.control.quest_travel import QuestTrip
from bot.perception.travel_channel import RoundStage, LifeState, RecoverySample
from experiments.qwen3_vl.run import parse_near_observation
from test_quest_travel import sample, Flags
from test_terrain_retreat import setup
from test_npc_obstacle import npc


def traced():
    trace = GroundTrace()
    nav = sample(flags=Flags.KEYS_VERIFIED)
    # Continuous northward walking, every half yard correlated to actual W input.
    for i in range(7):
        trace.note_input({'w'})
        nav = replace(nav, position=(.2, .206 - i * .001), sequence=i + 1)
        trace.observe(nav, i * .2)
    trip = QuestTrip(nav.world_size, nav.destination, 'ccw_west')
    return trace, nav, trip


def inject(probe, views, captured_at):
    future = Future()
    future.set_result((views, None))
    closed = []
    probe.request = SimpleNamespace(future=future, close=lambda: closed.append(True))
    probe.captured_at = captured_at
    return closed


def test_recent_walk_backtracks_with_no_visual_feet_and_waits_for_new_observation(monkeypatch):
    _, _, ctx, events, _ = setup(monkeypatch)
    trace, nav, trip = traced()
    probe = TerrainDetour(1.3, nav, 0, trip, None, ground_trace=trace)
    assert probe.phase == 'backtrack'
    trip.router.block_ahead(nav.position, (.2, .19))
    blocked = trip.router.blocked.copy()
    for i in range(4):
        at = 1.4 + i * .9
        assert probe.step(ctx, at, nav, 0).keys == {'s'}
        moved = replace(nav, position=(.2, nav.position[1] + .001), sequence=nav.sequence + 1)
        assert not probe.step(ctx, at + .2, moved, 0).keys
        command = probe.step(ctx, at + .75, moved, 0)
        assert not command.keys and command.outcome is None
        nav = moved
    assert probe.phase == 'clearance_check' and not probe.escape_checked
    assert trip.router.blocked == blocked
    assert not probe.observations and probe.anchor.position == nav.position
    result = next(d for n, d in events if n == 'quest_terrain_retreat_result')
    assert result['displacement_verified'] and not result['clearance_verified']
    assert sum(n == 'quest_escape_step' for n, _ in events) == 4
    assert not any(n == 'quest_terrain_request' for n, _ in events)  # no model on known reverse segments
    inject(probe, {'composition': {'usable': True, 'view': 'normal'}}, 6)
    assert not probe.step(ctx, 6.1, nav, 0).keys
    assert probe.phase == 'scan' and not probe.escape_checked
    observation = parse_near_observation(json.dumps(region()))
    # A sideways passage becomes visible from the new viewpoint. Do not walk back north.
    probe.angles['right'] = 3 * math.pi / 2
    probe.observations = {'right': observation}
    assert not probe.choose_or_scan(ctx, 7, nav, 0, {'recommendation': 'right'}).keys
    assert probe.escape_checked and probe.phase == 'confirm'
    for i in range(4):
        at = 8 + i * 3
        inject(probe, {'confirm': observation}, at)
        assert probe.step(ctx, at + .1, nav, probe.angles['right']).keys == {'w'}
        moved = replace(nav, position=(nav.position[0] + .0008, nav.position[1]), sequence=nav.sequence + 1)
        assert not probe.step(ctx, at + .4, moved, probe.angles['right']).keys
        command = probe.step(ctx, at + .8, moved, probe.angles['right'])
        assert not command.keys
        nav = moved
        assert command.outcome == ('detoured' if i == 3 else None)
    assert trip.router.blocked == blocked
    assert nav.position[1] > probe.backtrack.origin[1]  # rejoin did not run back into the contact


@pytest.mark.parametrize('change', [
    {'map_id': 999}, {'flags': Flags.KEYS_VERIFIED | Flags.FLYING},
    {'flags': Flags.KEYS_VERIFIED | Flags.FALLING}, {'flags': Flags.KEYS_VERIFIED | Flags.IN_COMBAT},
    {'position': (.7, .7)}, {'height': 80}, {'recovery': RecoverySample(LifeState.DEAD)},
])
def test_discontinuous_or_unsafe_trajectory_never_authorizes_backtrack(change):
    trace, nav, trip = traced()
    new = replace(nav, sequence=nav.sequence + 1, **change)
    trace.observe(new, 1.4)
    assert not trace.backtrack(new, 1.4, trip.router)


def test_no_input_or_missing_frames_cannot_join_ground_points():
    trace, nav, trip = traced()
    new = replace(nav, position=(.2, .199), sequence=nav.sequence + 1)
    trace.observe(new, 1.4)  # motion without a script action
    assert not trace.backtrack(new, 1.4, trip.router)
    trace, nav, trip = traced()
    trace.note_input({'w'})
    trace.observe(replace(nav, sequence=99), 3.0)
    assert not trace.backtrack(nav, 3.0, trip.router)


def test_stale_trace_and_new_collision_override_historical_passage():
    trace, nav, trip = traced()
    assert trace.backtrack(nav, 1.3, trip.router)
    assert not trace.backtrack(nav, 33, trip.router)
    trip.router.cell_size = .4
    target = trace.backtrack(nav, 1.3, trip.router)[0]
    trip.router.blocked.add(trip.router.cell(target.position))
    assert not trace.backtrack(nav, 1.3, trip.router)


def test_blocked_backtrack_stops_after_one_pulse_then_observes(monkeypatch):
    _, _, ctx, events, _ = setup(monkeypatch)
    trace, nav, trip = traced()
    probe = TerrainDetour(1.3, nav, 0, trip, None, ground_trace=trace)
    assert probe.step(ctx, 1.4, nav, 0).keys == {'s'}
    assert not probe.step(ctx, 1.6, nav, 0).keys
    assert not probe.step(ctx, 2.2, replace(nav, sequence=20), 0).keys
    assert probe.phase == 'scan' and probe.retreat_pulses == 1
    assert events[-1][1]['reason'] == 'escape_no_displacement'
    assert 'back' in probe.failed and trip.router.blocked


def test_real_normal_framing_with_unknown_feet_goes_to_side_observation(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    evidence = json.loads((Path(__file__).parent / 'fixtures/clutter-20261116.json').read_text())
    probe.phase = 'view_check'
    inject(probe, evidence['cases'][0]['views'], 1)
    assert not probe.step(ctx, 1.1, nav, 0).keys
    assert probe.phase == 'retreat_observe'
    inject(probe, evidence['cases'][1]['views'], 2)
    command = probe.step(ctx, 2.1, nav, math.pi)
    assert not command.keys and not command.outcome and probe.phase == 'scan'
    assert not probe.view_recovery_used and not probe.retreat_pulses
    # Two supplementary side observations exhaust evidence, never six camera resets.
    probe.observations = {name: evidence['cases'][1]['views']['retreat'] for name in ('left', 'right')}
    command = probe.choose_or_scan(ctx, 3, nav, 0, {'recommendation': 'stop'})
    assert command.outcome == 'escape_evidence_insufficient'
    assert not any(n == 'quest_terrain_composition' and d.get('usable') is False for n, d in events)


def region(**changes):
    return dict(schema=3, feet=None, player_box=[430, 500, 570, 800], ground_origin=[500, 790],
                origin_basis='visible_ground_at_player', front='open', left='unknown', right='unknown',
                next_view='front', feature='open_ground', landmark=[500, 680], evidence='base and connected ground') | changes


def test_visible_ground_at_identified_player_can_authorize_one_step_without_exact_feet(monkeypatch):
    observation = parse_near_observation(json.dumps(region()))
    assert observation['origin_verified'] and observation['feet'] is None and observation['front'] == 'open'
    probe, nav, ctx, _, _ = setup(monkeypatch)
    probe.phase, probe.direction = 'confirm', 'left'
    probe.angles['left'] = 0
    inject(probe, {'confirm': observation}, 1)
    assert probe.step(ctx, 1.1, nav, 0).keys == {'w'}
    assert not probe.step(ctx, 1.4, nav, 0).keys  # schema 3 retains the short pulse limit
    assert probe.escape_checked


@pytest.mark.parametrize('changes', [
    {'origin_basis': 'unknown'}, {'player_box': None}, {'ground_origin': None},
    {'ground_origin': [200, 790]}, {'ground_origin': [500, 600]},
    {'player_box': [0, 0, 1000, 1000]}, {'feature': 'drop'},
])
def test_player_box_or_unknown_ground_alone_cannot_authorize_motion(changes):
    assert parse_near_observation(json.dumps(region(**changes)))['front'] == 'unknown'


def test_escape_deadline_cancels_model_and_releases_input(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    closed = []
    probe.request = SimpleNamespace(future=Future(), close=lambda: closed.append(True))
    result = probe.step(ctx, 60, nav, 0)
    assert result.outcome == 'probe_budget_exhausted' and not result.keys and closed


def test_initial_camera_wait_does_not_consume_all_escape_time_or_reset_total_deadline(monkeypatch):
    from bot.perception.travel_channel import CameraSample, CameraScene, CameraResult
    _, nav, ctx, _, _ = setup(monkeypatch)
    nav = replace(nav, probe_camera_available=True,
                  camera=CameraSample(32, 2, CameraScene.INTERACT, CameraScene.INTERACT, 1,
                                      CameraResult.DISTANCE_VERIFIED))
    probe = TerrainDetour(0, nav, 0, QuestTrip(nav.world_size, nav.destination, 'ccw_west'), None)
    probe.phase = 'view_check'
    inject(probe, {'composition': {'usable': True}}, 17)
    assert not probe.step(ctx, 18, nav, 0).keys
    assert probe.escape_started == 18
    # Another framing check cannot renew the escape budget.
    probe.phase = 'clearance_check'
    inject(probe, {'composition': {'usable': True}}, 29)
    probe.step(ctx, 30, nav, 0)
    assert probe.escape_started == 18
    assert probe.step(ctx, 48, nav, 0).outcome is None
    assert probe.step(ctx, 60, nav, 0).outcome == 'probe_budget_exhausted'


def test_stale_heading_or_duplicate_receipt_cannot_extend_a_backtrack_pulse(monkeypatch):
    _, _, ctx, _, _ = setup(monkeypatch)
    trace, nav, trip = traced()
    probe = TerrainDetour(1.3, nav, 0, trip, None, ground_trace=trace)
    assert probe.step(ctx, 1.4, nav, 0).keys == {'s'}
    ctx.heading_age = lambda: .7
    assert not probe.step(ctx, 1.5, nav, 0).keys
    ctx.heading_age = lambda: 0
    moved = replace(nav, position=(.2, .201))
    assert not probe.step(ctx, 1.7, moved, 0).keys
    assert probe.step(ctx, 3, moved, 0).outcome == 'probe_telemetry_stale'


def test_new_position_discards_pending_visual_result(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    probe.phase, probe.direction = 'confirm', 'left'
    closed = inject(probe, {'confirm': parse_near_observation(json.dumps(region()))}, 1)
    command = probe.step(ctx, 2, replace(nav, position=(.2, .201), sequence=2), probe.angles['left'])
    assert command.outcome == 'probe_pose_changed' and not command.keys and closed


@pytest.mark.parametrize('changes', [
    {'flags': Flags.KEYS_VERIFIED | Flags.IN_COMBAT}, {'objective_target': 99},
    {'destination': (.1, .1)}, {'map_id': 999}, {'recovery': RecoverySample(LifeState.GHOST)},
])
def test_backtrack_interruptions_release_and_discard_pending_work(monkeypatch, changes):
    _, _, ctx, _, _ = setup(monkeypatch)
    trace, nav, trip = traced()
    probe = TerrainDetour(1.3, nav, 0, trip, None, ground_trace=trace)
    assert probe.step(ctx, 1.4, nav, 0).keys == {'s'}
    command = probe.step(ctx, 1.5, replace(nav, **changes), 0)
    assert not command.keys and command.outcome == 'probe_interrupted'


def test_npc_owns_escape_and_increasing_target_distance_does_not_replan(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    trace, ground, _ = traced()
    nav = replace(nav, position=ground.position, world_size=ground.world_size, sequence=ground.sequence,
                  destination=(.2, .19))
    # same map: trace's own identity is required, not a coincident coordinate
    trace.identity = nav.map_id, nav.world_size
    run.ground_trace = trace
    ctx.heading = lambda: 0
    run.begin_npc_detour(ctx, 1.3, nav, 'approach_no_displacement')
    assert run.terrain.phase == 'backtrack'
    run.tick_npc_detour(ctx, 1.4, nav, 0)
    assert ctx.input._held_keys == {'s'}
    moved = replace(nav, position=(.2, .201), sequence=20)
    run.tick_npc_detour(ctx, 1.6, moved, 0)
    run.tick_npc_detour(ctx, 2.2, moved, 0)
    assert run.npc_detouring and run.OUTCOME is None and not run.npc_rejoin
    assert not any(n == 'quest_npc_approach_replan' for n, _ in events)
    run.stop(ctx, 'game_not_frontmost')
    assert not ctx.input._held_keys and run.terrain is None and not run.ground_trace.points


@pytest.mark.parametrize('reason', ['escape_evidence_insufficient', 'escape_time_budget'])
def test_escape_exhaustion_suspends_only_current_task(tmp_path, monkeypatch, reason):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    nav = replace(nav, suspend_ready=True, suspend_epoch=0, round_stage=RoundStage.TURN_IN)
    run.round_stage, run.round_navigation = RoundStage.TURN_IN, nav
    run.stop(ctx, 'quest_npc_' + reason)
    assert run.OUTCOME is None and run.suspend_request[3] == 'quest_npc_' + reason
    assert not ctx.input._held_keys
