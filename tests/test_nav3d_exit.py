"""Split-policy exit handoff checks; not live game success evidence.

The 2026-11-18 user decision: Nav3D owns mesh-covered scenes, open-field legs
reuse the legacy straight-line stack. These cover hull exit candidates, the
coordinator exit segment (fresh budget, phase release, arrival outcome) and the
module-level release to the legacy tick path.
"""
from dataclasses import replace
from types import SimpleNamespace as NS

import json
import math
import time

import pytest

from bot.core.config import Nav3DConfig
from bot.nav3d import Frame, NavMesh, NavigationError
from bot.nav3d.demo import scene, Geometry
from bot.nav3d.exit import mesh_exit_candidates
from bot.nav3d.runtime import Nav3DCoordinator
from bot.control.quest_travel import TravelCommand
from bot.perception.nav3d_channel import Nav3DSample
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import TaskFlags


@pytest.fixture
def door_mesh(tmp_path):
    g, start, goal = scene('door')
    frame = Frame('12.1.5.70961', 2927, 'quest:90896:2', 'east_north_up')
    m = NavMesh.build(*g.arrays(), frame=frame)
    m.save(tmp_path/'mesh')
    return m, start, goal


def coordinator(door_mesh, tmp_path, **cfg):
    m, start, goal = door_mesh
    events = []
    rt = Nav3DCoordinator(Nav3DConfig(mode='nav3d', mesh=str(tmp_path/'mesh'),
        supervised_trial_mesh_id=m.mesh_id, **cfg), now=0,
        log_event=lambda *e: events.append(e))
    return rt, events, start, goal


def exit_sample(start, goal_xy, sequence, phase=3):
    return Nav3DSample(sequence, 2927, 70961, 2451, start[:2], 0, 0, 90896,
                       phase, 2927, goal_xy, True, True, False, 4096)


def exit_nav(sequence, phase=3):
    return NS(sequence=sequence, quest_id=90896, phase=phase, map_id=2451, destination=(.9, .5))


def names(events):
    return [name for name, _ in events]


def test_hull_candidates_order_by_goal_distance_over_eroded_boundary(tmp_path):
    g = Geometry()
    g.floor(0, 0, 24, 20)
    g.box((20, 16, 0), (24, 20, 4))  # raised block on the hull corner footprint
    m = NavMesh.build(*g.arrays(), frame=Frame('test', 0, 'test', 'east_north_up'))
    candidates = mesh_exit_candidates(m, (100, 20))
    assert len(candidates) >= 2
    distances = [math.dist(c[:2], (100, 20)) for c in candidates]
    assert distances == sorted(distances)
    # debug_triangles is the Recast-baked walkable surface, eroded by the agent
    # radius: exits sit strictly inside the raw geometry bounds.
    assert all(0 < x < 24 and 0 < y < 20 for x, y, _ in candidates)


def test_exit_segment_releases_phase_and_reaches_exit(door_mesh, tmp_path):
    from bot.control.quest_travel import TravelCommand
    rt, events, start, goal = coordinator(door_mesh, tmp_path)
    rt.begin_exit((100, 8.5), now=0)
    assert 'nav3d_exit_begin' in names(events) and rt.deadline == 120
    s, n = exit_sample(start, (100, 8.5), 2), exit_nav(2)
    command = rt.step(.1, s, n)
    assert command.outcome is None and command.tap  # supervised-trial input toward the exit
    assert 'nav3d_exit_phase_released' in names(events)  # READY phase no longer stops the frame check
    assert rt.goals[0][0] == pytest.approx(23.4, abs=.2)  # east boundary toward the off-mesh goal
    assert math.dist(rt.goals[0][:2], (100, 8.5)) == min(
        math.dist(goal[:2], (100, 8.5)) for goal in rt.goals)
    rt.follower = NS(index=0, step=lambda *a, **kw: TravelCommand(outcome='nav3d_route_arrived'))
    command = rt.step(.2, exit_sample(start, (100, 8.5), 3), exit_nav(3))
    assert command.outcome == 'nav3d_exit_reached'
    assert 'nav3d_exit_arrived' in names(events)
    assert not any(data.get('reason') == 'target_receipt_missing' for _, data in events)


def test_exit_segment_gets_fresh_budget_after_long_spawn_wait(door_mesh, tmp_path):
    rt, events, start, goal = coordinator(door_mesh, tmp_path, budget_s=10)
    rt.begin_exit((100, 8.5), now=300)
    assert rt.deadline == 310
    command = rt.step(300.5, exit_sample(start, (100, 8.5), 2), exit_nav(2))
    assert command.outcome != 'nav3d_budget_exhausted' and command.tap


def test_explicit_outdoor_handoff_accepts_one_mesh_surface(door_mesh, tmp_path):
    rt, _, start, goal = coordinator(door_mesh, tmp_path)
    with pytest.raises(NavigationError, match="exit_goal_on_mesh"):
        rt.begin_exit(goal[:2], now=0)
    rt.begin_exit(goal[:2], now=0, handoff=goal[:2])
    assert len(rt.exit_candidates) == 1
    command = rt.step(.1, exit_sample(start, goal[:2], 2), exit_nav(2))
    assert command.outcome is None and rt.route is not None
    assert rt.route.requested_goal[:2] == goal[:2]
    with pytest.raises(NavigationError, match="handoff_support_unconfirmed"):
        rt.begin_exit(goal[:2], now=0, handoff=(150., 100.))


def test_exit_keeps_world_and_build_checks_strict(door_mesh, tmp_path):
    rt, events, start, goal = coordinator(door_mesh, tmp_path)
    rt.begin_exit((100, 8.5), now=0)
    s, n = exit_sample(start, (100, 8.5), 2), exit_nav(2)
    assert rt.step(.1, replace(s, world_id=0), n).outcome == 'nav3d_world_frame_changed'
    rt, events, start, goal = coordinator(door_mesh, tmp_path)
    rt.begin_exit((100, 8.5), now=0)
    assert rt.step(.1, replace(exit_sample(start, (100, 8.5), 2), client_build=70783),
                   exit_nav(2)).outcome == 'nav3d_world_frame_changed'


def test_begin_exit_rejects_on_mesh_or_invalid_goals(door_mesh, tmp_path):
    rt, events, start, goal = coordinator(door_mesh, tmp_path)
    with pytest.raises(NavigationError, match='on_mesh'):
        rt.begin_exit(goal[:2], now=0)
    with pytest.raises(NavigationError, match='invalid'):
        rt.begin_exit((float('nan'), 8.5), now=0)
    with pytest.raises(NavigationError, match='invalid'):
        rt.begin_exit((1, 2, 3), now=0)


def test_exit_unreachable_when_no_hull_candidate_plans(door_mesh, tmp_path, monkeypatch):
    rt, events, start, goal = coordinator(door_mesh, tmp_path)
    rt.begin_exit((100, 8.5), now=0)
    def no_path(query):
        raise NavigationError('no_complete_path')
    monkeypatch.setattr(rt.mesh, 'plan', no_path)
    command = rt.step(.1, exit_sample(start, (100, 8.5), 2), exit_nav(2))
    assert command.outcome == 'nav3d_exit_unreachable'
    finished = [data for name, data in events if name == 'nav3d_candidate_finished']
    assert finished and len(finished) == len(rt.exit_candidates)
    assert not any(data['reached_in_game'] for data in finished)


def test_released_run_restores_legacy_tick_path(tmp_path, monkeypatch):
    from test_npc_obstacle import npc
    from bot.modules import quest
    from bot.perception.quest_channel import QuestState
    run, ctx, tick, nav, _ = npc(tmp_path, monkeypatch)
    ctx.cfg.nav3d.mode = 'nav3d'
    run.nav3d_released = True
    run.nav3d_coordinator = NS(step=lambda *a, **kw: pytest.fail('released run must not call nav3d'))
    monkeypatch.setattr(quest, 'decode_travel', lambda _: nav)
    recovers = []
    monkeypatch.setattr(run, 'recover', lambda *a: recovers.append(1))
    monkeypatch.setattr(run, 'interact_quest_target', lambda *a, **kw: None)
    tick(QuestState.READY)
    assert recovers and run.OUTCOME is None


def test_released_travel_dispatches_to_legacy_destination(tmp_path, monkeypatch):
    from test_npc_obstacle import npc
    from bot.control.quest_travel import TravelCommand
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    ctx.cfg.nav3d.mode = 'nav3d'
    run.nav3d_released = True
    called = []
    monkeypatch.setattr(run, 'travel_to_destination',
                        lambda *a: (called.append(a), TravelCommand())[1])
    run.travel(ctx, 1, nav, 0)
    assert called and not run.nav3d_enabled(ctx)


def terminal_encounter_run(tmp_path, monkeypatch, *, tracker_candidates, goal_xy):
    from test_npc_obstacle import npc
    from bot.nav3d.encounter import RespawnEncounter, MESH_ID
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    ctx.cfg.nav3d.mode = 'nav3d'
    run.nav3d_coordinator = NS(tracker=NS(candidates=tracker_candidates),
        begin_exit=lambda goal, *, now: pytest.fail('no exit segment expected'))
    encounter = RespawnEncounter(MESH_ID)
    encounter.terminal = 'quest_encounter_objective_complete_ready_for_turn_in'
    run.nav3d_encounter = encounter
    run.nav3d_sample = NS(position_xy=(2118.2, -864.6), goal_xy=goal_xy,
                          goal_world_id=2927, world_id=2927)
    return run, ctx, events, NS(quest_id=90896, phase=3, map_id=2451, sequence=2)


def test_kill_credit_outside_mesh_releases_without_exit_segment(tmp_path, monkeypatch):
    run, ctx, events, nav = terminal_encounter_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [], goal_xy=(2133, -854))
    run.travel_nav3d(ctx, 1, nav)
    assert run.nav3d_released and not run.nav3d_exit_started and run.OUTCOME is None
    assert 'nav3d_exit_skipped_outside_mesh' in [name for name, _ in events]


def test_kill_credit_without_goal_receipt_keeps_old_stop(tmp_path, monkeypatch):
    run, ctx, events, nav = terminal_encounter_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [(2118.2, -864.6, 67.6)], goal_xy=None)
    run.travel_nav3d(ctx, 1, nav)
    assert run.OUTCOME == 'stopped:quest_encounter_objective_complete_ready_for_turn_in'
    assert not run.nav3d_released and 'nav3d_exit_goal_unavailable' in [name for name, _ in events]


def stacked_coordinator(tmp_path, **kw):
    g, start, goal = scene('stacked')
    frame = Frame('12.1.5.70961', 2927, 'quest:90896:2', 'east_north_up')
    m = NavMesh.build(*g.arrays(), frame=frame)
    m.save(tmp_path/'mesh')
    events = []
    rt = Nav3DCoordinator(Nav3DConfig(mode='nav3d', mesh=str(tmp_path/'mesh'),
        supervised_trial_mesh_id=m.mesh_id), now=0,
        log_event=lambda *e: events.append(e), **kw)
    return rt, events, m


def prior_run(tmp_path, mesh_id, *, control=None, encounter_xy=None):
    folder = tmp_path/'runs'/'prior'
    folder.mkdir(parents=True, exist_ok=True)
    rows = [dict(kind='nav3d_plan', data=dict(route=dict(mesh_id=mesh_id)))]
    if control is not None:
        rows.append(dict(kind='nav3d_control', data=dict(position=control)))
    if encounter_xy is not None:
        rows.append(dict(kind='quest_encounter_stop',
                         data=dict(last_state=dict(position_xy=encounter_xy))))
    rows.append(dict(kind='module_end', ts=time.time(), data=dict(status='stopped:record')))
    (folder/'events.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    (folder/'summary.json').write_text('{}')
    return tmp_path/'runs'


def test_exit_seeds_start_support_from_recent_tracking_record(tmp_path):
    rt, events, m = stacked_coordinator(tmp_path)
    rt.resume_runs = prior_run(tmp_path, m.mesh_id,
                               control=[20.2, 8.1, 4.05], encounter_xy=(20.1, 8.0))
    stacked = (20., 8., 0.)  # stacked scene: floor z=0 and slab top z=4 share this XY
    rt.begin_exit((100, 8), now=0, sample=exit_sample(stacked, (100, 8), 1))
    seeded = [data for name, data in events if name == 'nav3d_exit_support_seeded']
    assert seeded and seeded[0]['native_altitude_verified'] is False
    assert abs(rt.tracker.last[2]-4) < .5  # inferred continuity, not a new altitude claim
    command = rt.step(.1, exit_sample(stacked, (100, 8), 2), exit_nav(2))
    assert command.outcome != 'nav3d_support_ambiguous' and command.tap


@pytest.mark.parametrize('encounter_xy', [(5., 5.), (20.1, 8.0)])
def test_exit_without_seed_evidence_starts_current_state_floor_trials(tmp_path, encounter_xy):
    # 用户要求 2026-11-18：每次启动按当前情况自处理；无历史证据时叠层
    # 起始层从低到高试错，不再以支撑歧义直接停机。
    rt, events, m = stacked_coordinator(tmp_path)
    rt.resume_runs = prior_run(tmp_path, m.mesh_id, control=None, encounter_xy=encounter_xy)
    stacked = (20., 8., 0.)
    rt.begin_exit((100, 8), now=0, sample=exit_sample(stacked, (100, 8), 1))
    assert not any(name == 'nav3d_exit_support_seeded' for name, _ in events)
    command = rt.step(.1, exit_sample(stacked, (100, 8), 2), exit_nav(2))
    trials = [data for name, data in events if name == 'nav3d_exit_floor_hypothesis']
    assert trials and trials[0]['reason'] == 'stacked_start' and trials[0]['native_altitude_verified'] is False
    assert command.outcome != 'nav3d_support_ambiguous'


def fresh_ready_run(tmp_path, monkeypatch, *, tracker_candidates, goal_xy, phase):
    from test_npc_obstacle import npc
    from bot.nav3d.encounter import RespawnEncounter, MESH_ID
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    ctx.cfg.nav3d.mode = 'nav3d'
    exits, coordinator_steps = [], []
    run.nav3d_coordinator = NS(tracker=NS(candidates=tracker_candidates),
        begin_exit=lambda goal, *, now, sample=None: exits.append(goal),
        step=lambda *a, **kw: (coordinator_steps.append(a), TravelCommand())[1])
    run.nav3d_encounter = RespawnEncounter(MESH_ID)  # inactive outside the holding region
    run.nav3d_sample = NS(position_xy=(2097.2, -879.4), goal_xy=goal_xy,
                          goal_world_id=2927, world_id=2927, quest_id=90896,
                          encounter_target_matches=False)
    run.observation = NS(combat=NS(value=None))
    navigation = NS(quest_id=90896, phase=phase, map_id=2451, sequence=2,
                    task=TaskFlags(0), objective_action=0, objective_target=0)
    return run, ctx, events, navigation, exits, coordinator_steps


def test_fresh_ready_run_outside_region_begins_mesh_exit(tmp_path, monkeypatch):
    run, ctx, events, nav, exits, steps = fresh_ready_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [(2097.2, -879.4, 62.2)],
        goal_xy=(1981.75, -1001.23), phase=QuestState.READY)
    run.travel_nav3d(ctx, 1, nav)
    assert exits == [(1981.75, -1001.23)] and run.nav3d_exit_started and run.OUTCOME is None
    assert not steps and 'nav3d_exit_goal_unavailable' not in [name for name, _ in events]


def test_fresh_ready_run_outside_mesh_releases_to_legacy(tmp_path, monkeypatch):
    run, ctx, events, nav, exits, _ = fresh_ready_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [], goal_xy=(1981.75, -1001.23),
        phase=QuestState.READY)
    run.travel_nav3d(ctx, 1, nav)
    assert run.nav3d_released and not exits and run.OUTCOME is None
    assert 'nav3d_exit_skipped_outside_mesh' in [name for name, _ in events]


def test_fresh_ready_run_waits_for_goal_receipt_then_begins(tmp_path, monkeypatch):
    run, ctx, events, nav, exits, _ = fresh_ready_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [(2097.2, -879.4, 62.2)], goal_xy=None,
        phase=QuestState.READY)
    run.travel_nav3d(ctx, 1, nav)
    run.travel_nav3d(ctx, 1.2, NS(**{**vars(nav), 'sequence': 3}))
    assert run.OUTCOME is None and not exits and not run.nav3d_released
    assert [name for name, _ in events].count('nav3d_exit_goal_unavailable') == 1
    run.nav3d_sample.goal_xy = (1981.75, -1001.23)
    run.travel_nav3d(ctx, 1.4, NS(**{**vars(nav), 'sequence': 4}))
    assert exits == [(1981.75, -1001.23)]


def test_fresh_active_run_outside_region_stays_with_coordinator(tmp_path, monkeypatch):
    run, ctx, events, nav, exits, steps = fresh_ready_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [(2097.2, -879.4, 62.2)],
        goal_xy=(2133, -854), phase=QuestState.ACTIVE)
    run.travel_nav3d(ctx, 1, nav)
    assert steps and not exits and not run.nav3d_exit_started


def test_seeding_uses_chronologically_last_position_not_per_kind_last(tmp_path):
    rt, events, m = stacked_coordinator(tmp_path)
    folder = tmp_path/'runs'/'walked_out'
    folder.mkdir(parents=True)
    rows = [  # encounter terminal early in the run (far XY), then tracking walked near
        dict(kind='quest_encounter_stop', data=dict(last_state=dict(position_xy=[5., 5.]))),
        dict(kind='nav3d_plan', ts=time.time(), data=dict(route=dict(mesh_id=m.mesh_id))),
        dict(kind='nav3d_control', ts=time.time(), data=dict(position=[20.2, 8.1, 4.05])),
        dict(kind='nav3d_pose', ts=time.time(), data=dict(position_xy=[20.15, 8.05])),
        dict(kind='module_end', ts=time.time(), data=dict(status='stopped:record'))]
    (folder/'events.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    (folder/'summary.json').write_text('{}')
    rt.resume_runs = tmp_path/'runs'
    stacked = (20., 8., 0.)
    rt.begin_exit((100, 8), now=0, sample=exit_sample(stacked, (100, 8), 1))
    seeded = [data for name, data in events if name == 'nav3d_exit_support_seeded']
    assert seeded and abs(seeded[0]['seeded'][2]-4) < .5
    # A newer run whose LAST position is far aborts the chain instead.
    moved = tmp_path/'runs'/'zz-moved'
    moved.mkdir()
    rows2 = [dict(kind='nav3d_pose', ts=time.time(), data=dict(position_xy=[3., 3.])),
             dict(kind='module_end', ts=time.time(), data=dict(status='stopped:record'))]
    (moved/'events.jsonl').write_text('\n'.join(json.dumps(row) for row in rows2))
    (moved/'summary.json').write_text('{}')
    rt2, _, m2 = stacked_coordinator(tmp_path/'second')
    rt2.resume_runs = tmp_path/'runs'
    assert not rt2.begin_exit((100, 8), now=0, sample=exit_sample(stacked, (100, 8), 1))
    assert rt2.tracker.last is None


def test_exit_without_any_prior_evidence_tries_floors_bottom_up(tmp_path):
    rt, events, m = stacked_coordinator(tmp_path)  # no resume_runs: no history at all
    stacked = (20., 8., 0.)
    rt.begin_exit((100, 8), now=0, sample=exit_sample(stacked, (100, 8), 1))
    real_plan = rt.mesh.plan
    def no_exit_path(query):  # long exit-goal plans fail; short support checks stay real
        if any(query.goal[:2] == candidate[:2] for candidate in rt.exit_candidates):
            raise NavigationError('no_complete_path')
        return real_plan(query)
    rt.mesh.plan = no_exit_path
    command = rt.step(.1, exit_sample(stacked, (100, 8), 2), exit_nav(2))
    trials = [d for name, d in events if name == 'nav3d_exit_floor_hypothesis']
    assert trials[0]['reason'] == 'stacked_start' and trials[0]['levels'][0] < 1 < trials[0]['levels'][1]
    # ground floor first, then candidate exhaustion advances to the upper floor
    assert len(trials) == 2 and trials[1]['reason'] == 'exit_candidates_exhausted'
    assert abs(trials[1]['support'][2]-4) < 1 and trials[1]['native_altitude_verified'] is False
    # the last floor is bounded too: stop, never guessing past the mesh
    assert command.outcome == 'nav3d_exit_unreachable'


def test_exit_floor_trial_advances_on_deviation_without_burning_replans(tmp_path):
    rt, events, m = stacked_coordinator(tmp_path)
    stacked = (20., 8., 0.)
    rt.begin_exit((100, 8), now=0, sample=exit_sample(stacked, (100, 8), 1))
    assert rt.step(.1, exit_sample(stacked, (100, 8), 2), exit_nav(2)).outcome is None
    assert rt.follower is not None
    rt.follower = NS(index=0, step=lambda *a, **kw: TravelCommand(outcome='nav3d_route_deviation'))
    command = rt.step(.2, exit_sample(stacked, (100, 8), 3), exit_nav(3))
    trials = [d for name, d in events if name == 'nav3d_exit_floor_hypothesis']
    assert len(trials) == 2 and trials[1]['reason'] == 'nav3d_route_deviation'
    assert command.outcome is None and rt.replans == 0  # floor trials have their own budget
    assert abs(rt.tracker.last[2]-4) < 1
    # the upper-floor hypothesis plans on the next receipt and issues input again
    command = rt.step(.3, exit_sample(stacked, (100, 8), 4), exit_nav(4))
    assert command.outcome is None and rt.follower is not None


def test_route_deviation_replans_from_verified_support(door_mesh, tmp_path):
    from bot.control.quest_travel import TravelCommand
    rt, events, start, goal = coordinator(door_mesh, tmp_path)
    rt.begin_exit((100, 8.5), now=0)
    rt.step(.1, exit_sample(start, (100, 8.5), 2), exit_nav(2))
    assert rt.follower is not None
    rt.follower = NS(index=0, step=lambda *a, **kw: TravelCommand(outcome='nav3d_route_deviation'))
    command = rt.step(.2, exit_sample(start, (100, 8.5), 3), exit_nav(3))
    assert command.outcome is None and rt.replans == 1 and rt.follower is None
    replanned = [d for n, d in events if n == 'nav3d_candidate_replanned']
    assert replanned and replanned[0]['reason'] == 'nav3d_route_deviation'
    assert rt.goal_index == 0  # same exit goal; drift replans, never rotates to another corner
    assert not any(n == 'nav3d_candidate_finished' for n, _ in events)


def test_released_run_skips_legacy_visual_building_access(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = fresh_ready_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [], goal_xy=(1981.75, -1001.23),
        phase=QuestState.READY)[:5]
    run.nav3d_released = True
    assert run.process_building_access(ctx, 1, nav, 0) is False
    assert run.building_access is None


def foreign_quest_run(tmp_path, monkeypatch, *, tracker_candidates, goal_xy):
    from test_npc_obstacle import npc
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    ctx.cfg.nav3d.mode = 'nav3d'
    exits = []
    mesh_frame = Frame('12.1.5.70961', 2927, 'quest:90896:2', 'east_north_up')
    run.nav3d_coordinator = NS(mesh=NS(frame=mesh_frame), sequence=None,
        tracker=NS(candidates=tracker_candidates),
        begin_exit=lambda goal, *, now, sample=None: exits.append(goal),
        step=lambda *a, **kw: pytest.fail('mismatch branch must decide before the coordinator'))
    run.nav3d_sample = replace(exit_sample((2069.5, -919.0), goal_xy, 2), quest_id=90898)
    navigation = NS(quest_id=90898, phase=QuestState.READY, map_id=2451, sequence=2,
                    task=TaskFlags(0), objective_action=0, objective_target=0)
    return run, ctx, events, navigation, exits


def test_foreign_quest_off_mesh_releases_to_legacy(tmp_path, monkeypatch):
    run, ctx, events, nav, exits = foreign_quest_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [], goal_xy=(1981.75, -1001.23))
    run.travel_nav3d(ctx, 1, nav)
    assert run.nav3d_released and not exits and run.OUTCOME is None
    assert 'quest_encounter_handoff_legacy' in [name for name, _ in events]


def test_foreign_quest_on_mesh_with_on_mesh_goal_stops_clearly(tmp_path, monkeypatch):
    run, ctx, events, nav, exits = foreign_quest_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: [(float(xy[0]), float(xy[1]), 61.5)],
        goal_xy=(2133, -854))
    run.travel_nav3d(ctx, 1, nav)
    assert run.OUTCOME == 'stopped:nav3d_scene_quest_mismatch' and not exits


def test_foreign_quest_on_mesh_with_off_mesh_goal_exits_mesh_first(tmp_path, monkeypatch):
    run, ctx, events, nav, exits = foreign_quest_run(tmp_path, monkeypatch,
        tracker_candidates=lambda xy, **kw: ([(float(xy[0]), float(xy[1]), 61.5)]
            if tuple(xy) != (1981.75, -1001.23) else []),
        goal_xy=(1981.75, -1001.23))
    run.travel_nav3d(ctx, 1, nav)
    assert exits == [(1981.75, -1001.23)] and run.nav3d_exit_started and run.OUTCOME is None
    run.nav3d_sample = replace(run.nav3d_sample, sequence=3)
    nav.sequence = 3
    steps = []

    def follow_exit(now, pose, navigation, **kwargs):
        steps.append((pose.sequence, navigation.sequence))
        return TravelCommand(outcome='nav3d_exit_reached')

    run.nav3d_coordinator.step = follow_exit
    run.travel_nav3d(ctx, 1.2, nav)
    assert steps == [(3, 3)] and len(exits) == 1
    assert run.nav3d_released and run.OUTCOME is None


@pytest.mark.parametrize('already_following', [False, True])
def test_foreign_world_never_compares_positions_against_current_mesh(tmp_path, monkeypatch, already_following):
    run, ctx, events, nav, exits = foreign_quest_run(tmp_path, monkeypatch,
        tracker_candidates=lambda *a, **kw: pytest.fail('coordinates are from a different world'),
        goal_xy=(2069.5, -919.0))
    run.nav3d_sample = replace(run.nav3d_sample, world_id=2858, goal_world_id=2858, quest_id=86769, ui_map_id=2432)
    nav.quest_id, nav.map_id = 86769, 2432
    if already_following:
        run.nav3d_coordinator.sequence = 1
    run.travel_nav3d(ctx, 1, nav)
    assert not exits and not ctx.input.count('key_down')
    if already_following:
        assert run.OUTCOME == 'stopped:nav3d_world_frame_changed' and not run.nav3d_released
    else:
        assert run.OUTCOME is None and run.nav3d_released
        assert ('quest_encounter_handoff_legacy', {'reason': 'outside_mesh_world'}) in events


def test_foreign_quest_waits_for_coherent_exit_telemetry_with_a_deadline(tmp_path, monkeypatch):
    run, ctx, _, nav, exits = foreign_quest_run(tmp_path, monkeypatch,
        tracker_candidates=lambda *a, **kw: pytest.fail('stale pose reached the mesh'), goal_xy=(.5, .5))
    nav.sequence = 3
    run.set_travel_keys(ctx, frozenset({'w'}))
    run.travel_nav3d(ctx, 1, nav)
    assert run.OUTCOME is None and not run.nav3d_released and not exits
    assert not ctx.input._held_keys
    run.travel_nav3d(ctx, 9.1, nav)
    assert run.OUTCOME == 'stopped:nav3d_handoff_telemetry_unavailable'


def test_exit_phase_relax_spans_foreign_quests(door_mesh, tmp_path):
    rt, events, start, goal = coordinator(door_mesh, tmp_path)
    rt.begin_exit((100, 8.5), now=0)
    sample = replace(exit_sample(start, (100, 8.5), 2), quest_id=90898)
    nav = NS(sequence=2, quest_id=90898, phase=3, map_id=2451, destination=(.9, .5))
    command = rt.step(.1, sample, nav)
    assert command.outcome != 'nav3d_world_frame_changed'
    assert 'nav3d_exit_phase_released' in names(events)


@pytest.mark.parametrize("released", [False, True])
def test_choice_quest_waits_without_input(tmp_path, monkeypatch, released):
    from test_npc_obstacle import npc
    from bot.perception.travel_channel import ObjectiveAction, TaskFlags
    run, ctx, tick, nav, events = npc(tmp_path, monkeypatch)
    ctx.cfg.nav3d.mode = 'nav3d'
    run.nav3d_released = released
    choice = replace(nav, phase=QuestState.ACTIVE, task=TaskFlags.PREPARED, objective_action=ObjectiveAction.CHOICE)
    monkeypatch.setattr("bot.modules.quest.decode_travel", lambda _: choice)
    tick(QuestState.ACTIVE)
    assert run.OUTCOME is None and run.choice_wait is not None
    assert any(name == 'quest_choice_waiting' for name, _ in events)
    assert not ctx.input.count('key_down')


def test_choice_handoff_waits_for_exact_followup_then_resumes(tmp_path, monkeypatch):
    from test_npc_obstacle import npc
    from bot.perception.travel_channel import ChoiceStep, ObjectiveAction, RoundStage, TaskFlags

    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.map_id = 2451
    choice = replace(nav, quest_id=90911, map_id=2451, phase=QuestState.ACTIVE,
                     task=TaskFlags.PREPARED, objective_action=ObjectiveAction.CHOICE, addon_build=283,
                     choice_step=ChoiceStep.APPROACH)
    frame = NS(quest_id=90911, map_id=2451, state=QuestState.ACTIVE, tracking=True)
    assert not run.wait_for_choice_handoff(ctx, 1, frame, choice)
    assert run.choice_wait is None and not ctx.input.count('key_down', '9')
    opener = replace(choice, choice_step=ChoiceStep.OPEN_PANEL, sequence=2)
    assert run.wait_for_choice_handoff(ctx, 2, frame, opener)
    assert run.wait_for_choice_handoff(ctx, 2.1, frame, opener)
    assert ctx.input.count('key_down', '9') == 1
    assert run.wait_for_choice_handoff(ctx, 3, frame, replace(choice, choice_step=ChoiceStep.SELECT, sequence=3))
    assert run.wait_for_choice_handoff(ctx, 3.1, frame, opener)
    assert ctx.input.count('key_down', '9') == 2
    assert run.wait_for_choice_handoff(ctx, 4, None, None)  # Loading screen.
    frame = NS(quest_id=99900, map_id=85, state=QuestState.PENDING, tracking=True)
    next_nav = replace(nav, map_id=85, quest_id=99900, phase=QuestState.PENDING,
                       task=TaskFlags.PREPARED, round_stage=RoundStage.PICKUP)
    assert run.wait_for_choice_handoff(ctx, 5, frame, next_nav)
    assert run.map_id == 2451 and run.OUTCOME is None
    frame.quest_id = 91281
    assert not run.wait_for_choice_handoff(ctx, 6, frame, replace(next_nav, quest_id=91281))
    assert run.map_id == 85 and run.nav3d_released and run.choice_wait is None
    assert ('quest_choice_handoff', {'id': 90911, 'map': 85, 'next_id': 91281}) in events
    assert ctx.stats.get('quests_done', 0) == 0 and ctx.input.count('key_down') == 2


def test_choice_approach_reuses_verified_npc_interaction(tmp_path, monkeypatch):
    from test_npc_obstacle import npc
    from bot.modules.quest import QuestStep
    from bot.perception.quest_channel import Dialogue, Objective
    from bot.perception.travel_channel import ChoiceStep, ObjectiveAction, RoundStage, TaskFlags

    run, ctx, tick, nav, events = npc(tmp_path, monkeypatch)
    run.steps[0] = QuestStep(90911, '9', None, None, None, None, frozenset())
    run.map_id = 2451
    run.require_prepared = True
    ready = nav.task | TaskFlags.NPC_WITHIN_5_YARDS
    choice = replace(nav, quest_id=90911, map_id=2451, destination_map=2451, phase=QuestState.ACTIVE,
                     task=ready, objective_action=ObjectiveAction.CHOICE, addon_build=283,
                     choice_step=ChoiceStep.APPROACH, round_stage=RoundStage.OBJECTIVES)
    monkeypatch.setattr('bot.modules.quest.decode_travel', lambda _: replace(choice, sequence=ctx.frame_sequence()))
    monkeypatch.setattr('bot.modules.quest.decode_quest', lambda _: NS(
        quest_id=choice.quest_id, map_id=choice.map_id, state=choice.phase, tracking=True,
        sequence=ctx.frame_sequence(), kind=next(iter(run.kinds)), objective=Objective.COUNTED,
        progress=(0, 1), dialogue=Dialogue.CLOSED, dialogue_id=0))
    tick(QuestState.ACTIVE)
    assert run.hunt is None and run.choice_wait is None
    assert ctx.input.count('key_down', 'f') == 1 and not ctx.input.count('key_down', '9')
    assert any(name == 'quest_phase' and payload['action'] == 'CHOICE' for name, payload in events)


def test_verified_transport_option_is_sent_once_then_resumes_turn_in(tmp_path, monkeypatch):
    from test_npc_obstacle import npc
    from bot.perception.travel_channel import ChoiceStep, ObjectiveAction, RoundStage, TaskFlags, TravelFlags

    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    run.map_id = 85
    transport = replace(nav, quest_id=88719, map_id=85, phase=QuestState.ACTIVE,
                        task=TaskFlags.PREPARED, objective_action=ObjectiveAction.CHOICE, choice_step=ChoiceStep.SELECT)
    frame = NS(quest_id=88719, map_id=85, state=QuestState.ACTIVE, tracking=True)
    assert run.wait_for_choice_handoff(ctx, 1, frame, replace(transport, flags=TravelFlags.CASTING))
    assert ctx.input.count('key_down', '9') == 0
    assert run.wait_for_choice_handoff(ctx, 2, frame, transport)
    assert run.wait_for_choice_handoff(ctx, 3, frame, transport)
    assert ctx.input.count('key_down', '9') == 1
    frame.state = QuestState.READY
    assert run.wait_for_choice_handoff(ctx, 3.5, frame, replace(transport, phase=QuestState.READY))
    assert run.map_id == 85 and run.choice_wait is not None  # Source READY can precede the zone change.
    frame.state = QuestState.ACTIVE
    frame.map_id = 2432
    landed = replace(transport, map_id=2432, round_stage=RoundStage.TURN_IN, choice_step=ChoiceStep.WAIT)
    assert run.wait_for_choice_handoff(ctx, 5, frame, landed)
    frame.state = QuestState.READY
    assert not run.wait_for_choice_handoff(ctx, 6, frame, replace(landed, phase=QuestState.READY))
    assert run.map_id == 2432 and run.choice_wait is None


@pytest.mark.parametrize('gossip_closed', [False, True])
def test_transport_retries_after_verified_standing_error(tmp_path, monkeypatch, gossip_closed):
    from test_npc_obstacle import npc
    from bot.perception.travel_channel import ChoiceStep, ObjectiveAction, TaskFlags, TravelFlags

    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.map_id = 85
    transport = replace(nav, quest_id=88719, map_id=85, phase=QuestState.ACTIVE, addon_build=284,
                        task=TaskFlags.PREPARED, objective_action=ObjectiveAction.CHOICE, choice_step=ChoiceStep.SELECT)
    frame = NS(quest_id=88719, map_id=85, state=QuestState.ACTIVE, tracking=True)
    assert run.wait_for_choice_handoff(ctx, 1, frame, transport)
    standing = replace(transport, choice_step=ChoiceStep.STAND, sequence=2)
    assert run.wait_for_choice_handoff(ctx, 2, frame, replace(standing, flags=TravelFlags(0)))
    assert ctx.input.count('key_down', 'w') == 0
    assert run.wait_for_choice_handoff(ctx, 2.2, frame, standing)
    assert run.wait_for_choice_handoff(ctx, 2.4, frame, standing)
    assert ctx.input.count('key_down', 'w') == 1
    assert 'quest_transport_stand' in names(events)
    if gossip_closed:
        run.npc_press_at = 1
        assert not run.wait_for_choice_handoff(ctx, 4, frame, replace(transport, choice_step=ChoiceStep.APPROACH))
        assert run.npc_press_at is None
    assert run.wait_for_choice_handoff(ctx, 5, frame, replace(transport, sequence=3))
    assert run.wait_for_choice_handoff(ctx, 6, frame, replace(transport, sequence=3))
    assert ctx.input.count('key_down', '9') == 2
