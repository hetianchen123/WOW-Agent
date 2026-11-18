from dataclasses import replace
from types import SimpleNamespace

import pytest

from bot.modules import quest
from bot.control.quest_travel import TravelCommand
from bot.perception.quest_channel import QuestState, Dialogue
from bot.perception.travel_channel import TaskFlags, TravelFlags, TravelSample
from test_quest import make_run


def npc(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    run.phase = QuestState.READY
    ctx.input.name = "helper"
    monkeypatch.setattr(quest, "local_vision_available", lambda: True)
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: True)
    events = []
    ctx.log_event = lambda event, data: events.append((event, data))
    nav = TravelSample(12345, 2248, 2248, (.4, .492), (.4, .5), (1000, 1000), 10,
                       QuestState.READY, TravelFlags.KEYS_VERIFIED, 1, interact_verified=True,
                       task=TaskFlags.PREPARED | TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN,
                       npc_range_supported=True)
    return run, ctx, tick, nav, events


def test_npc_falling_releases_forward_then_two_ground_frames_start_visual_probe(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.interact_quest_target(ctx, 1, nav, 0)
    assert "w" in ctx.input._held_keys
    falling = replace(nav, flags=nav.flags | TravelFlags.FALLING, sequence=2)
    run.interact_quest_target(ctx, 1.1, falling, 0)
    assert not ctx.input._held_keys and run.OUTCOME is None and run.terrain is None
    diagnostic = next(data for event, data in events if event == "quest_npc_approach_state")
    assert diagnostic["unsafe"] == ["FALLING"] and diagnostic["keys_verified"]
    grounded = replace(nav, sequence=3)
    run.interact_quest_target(ctx, 1.3, grounded, 0)
    run.interact_quest_target(ctx, 1.7, grounded, 0)  # duplicate cannot confirm landing
    assert run.terrain is None
    run.interact_quest_target(ctx, 1.8, replace(nav, sequence=4), 0)
    assert run.terrain is not None and run.npc_detouring and run.OUTCOME is None
    assert not ctx.input._held_keys
    assert events[-1][0] == "quest_npc_visual_probe"
    assert events[-1][1]["trigger"] == "falling_during_approach"


@pytest.mark.parametrize("flags", [TravelFlags(0), TravelFlags.KEYS_VERIFIED | TravelFlags.MOUNTED,
                                   TravelFlags.KEYS_VERIFIED | TravelFlags.FLYING,
                                   TravelFlags.KEYS_VERIFIED | TravelFlags.SWIMMING])
def test_npc_unsafe_input_or_travel_state_never_becomes_visual_movement(tmp_path, monkeypatch, flags):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.set_travel_keys(ctx, frozenset({"w"}))
    run.interact_quest_target(ctx, 1, replace(nav, flags=flags), 0)
    assert run.OUTCOME == "stopped:quest_turn_in_approach_unsafe"
    assert not ctx.input._held_keys and run.terrain is None
    assert any(name == "quest_npc_approach_state" for name, _ in events)
    assert not any(name == "quest_npc_visual_probe" for name, _ in events)


def test_npc_unsettled_fall_has_budget_and_never_blindly_jumps(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    falling = replace(nav, flags=nav.flags | TravelFlags.FALLING)
    run.interact_quest_target(ctx, 0, falling, 0)
    run.interact_quest_target(ctx, 3.1, replace(falling, sequence=2), 0)
    assert run.OUTCOME == "stopped:quest_npc_ground_not_stable"
    assert not ctx.input.count("key_down") and run.terrain is None


def test_npc_no_displacement_enters_visual_and_rejoin_cannot_skip_path_for_selected_npc(tmp_path, monkeypatch):
    run, ctx, tick, nav, events = npc(tmp_path, monkeypatch)
    run.interact_quest_target(ctx, 1, nav, 0)
    run.interact_quest_target(ctx, 1.6, replace(nav, sequence=2), 0)
    assert run.npc_detouring and run.trip.walk_only and run.trip.router.cell_size == 2
    assert run.terrain.phase == "contact_release"  # Actual contact probes S before waiting for images.
    assert run.trip.router.blocked
    run.terrain.close()
    run.terrain = SimpleNamespace(step=lambda *_: TravelCommand(outcome="detoured"), close=lambda: None)
    moved = replace(nav, position=(.4, .507), sequence=3)
    run.interact_quest_target(ctx, 2, moved, 0)
    assert not run.npc_detouring and run.npc_rejoin and not run.arrived
    assert run.npc_steps == 0 and not ctx.input.count("key_down", "f")
    # Rejoin must traverse the known local route even while the NPC stays selected.
    run.objective_index = None
    run.phase_started = run.last_progress = 100
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(moved, sequence=ctx.frame_sequence()))
    travel = []
    monkeypatch.setattr(run, "travel", lambda *args: travel.append(args[2]))
    tick(QuestState.READY)
    assert travel and not ctx.input.count("key_down", "f")
    assert any(name == "quest_npc_visual_result" for name, _ in events)
    moved = replace(moved, task=moved.task | TaskFlags.NPC_WITHIN_5_YARDS)
    tick(QuestState.READY)
    assert not run.npc_rejoin and ctx.input.count("key_down", "f") == 1


def test_npc_same_failed_point_does_not_repeat_model_probe(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.begin_npc_detour(ctx, 0, nav, "approach_no_displacement")
    run.terrain.close()
    run.terrain, run.npc_detouring = None, False
    run.begin_npc_detour(ctx, 10, replace(nav, sequence=2), "approach_no_displacement")
    assert run.OUTCOME == "stopped:quest_npc_route_unconfirmed"
    assert sum(name == "quest_npc_visual_probe" for name, _ in events) == 1


def test_npc_visual_failure_uses_task_suspend_instead_of_ending_round(tmp_path, monkeypatch):
    from bot.perception.travel_channel import RoundStage
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    nav = replace(nav, suspend_ready=True, suspend_epoch=0, round_stage=RoundStage.TURN_IN)
    run.round_navigation, run.round_stage = nav, RoundStage.TURN_IN
    run.begin_npc_detour(ctx, 0, nav, "approach_no_displacement")
    run.terrain.close()
    run.terrain = SimpleNamespace(step=lambda *_: TravelCommand(outcome="no_passage"), close=lambda: None)
    run.interact_quest_target(ctx, 1, replace(nav, sequence=2), 0)
    assert run.OUTCOME is None and run.suspend_request[3] == "quest_npc_route_unconfirmed"
    assert not ctx.input._held_keys


def test_npc_probe_closes_on_dialogue_and_on_task_phase_change(tmp_path, monkeypatch):
    run, ctx, tick, nav, _ = npc(tmp_path, monkeypatch)
    run.begin_npc_detour(ctx, 0, nav, "approach_no_displacement")
    closed = []
    run.terrain.close()
    run.terrain = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav, sequence=ctx.frame_sequence()))
    tick(QuestState.READY, Dialogue.REWARD, 12345)
    assert closed and not run.npc_detouring and run.terrain is None
    assert ctx.input.count("key_down", "9") == 1


def test_wrong_heading_turns_then_requires_fresh_settled_receipt(tmp_path, monkeypatch):
    import math
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.interact_quest_target(ctx, 1, nav, math.pi)
    assert not ctx.input._held_keys and not ctx.input.count('key_down', 'w')
    assert any(name == 'quest_npc_align' for name, _ in events)
    run.interact_quest_target(ctx, 1.7, nav, 0)  # Same nav sequence cannot confirm turn.
    assert not ctx.input.count('key_down', 'w')
    run.interact_quest_target(ctx, 1.8, replace(nav, sequence=2), 0)
    assert ctx.input._held_keys == {'w'}


def test_latest_run_heading_does_not_repeat_three_wrong_forward_steps(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    nav = replace(nav, position=(.24719357222897742, .3694490141382352),
                  destination=(.24580025272393485, .37070023603461844), world_size=(3477.1, 2318.8))
    run.interact_quest_target(ctx, 1, nav, 4.254368133628447)
    assert ctx.input.count('key_down', 'w') == 0 and run.npc_step is None
    assert not run.terrain and any(name == 'quest_npc_align' for name, _ in events)


def test_npc_step_away_from_marker_replans_without_claiming_collision(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.interact_quest_target(ctx, 1, nav, 0)
    moved_away = replace(nav, position=(.4, .502), sequence=2)
    run.interact_quest_target(ctx, 1.6, moved_away, 0)
    assert not ctx.input._held_keys and run.npc_rejoin and not run.arrived
    assert run.terrain is None and not run.trip.router.blocked
    assert any(name == 'quest_npc_approach_replan' for name, _ in events)
    assert ctx.input.count('key_down', 'w') == 1


def test_npc_progress_toward_marker_can_continue_but_is_not_arrival(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.interact_quest_target(ctx, 1, nav, 0)
    run.interact_quest_target(ctx, 1.3, nav, 0)
    run.interact_quest_target(ctx, 1.6, replace(nav, position=(.4, .498), sequence=2), 0)
    assert ctx.input.count('key_down', 'w') == 2 and not ctx.input.count('key_down', 'f')
    assert not run.npc_rejoin and run.terrain is None


def test_near_marker_without_npc_range_does_not_walk_past_it(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    nav = replace(nav, destination=(.4, .4995))
    run.interact_quest_target(ctx, 1, nav, 0)
    assert run.OUTCOME == 'stopped:quest_npc_marker_range_unconfirmed'
    assert not ctx.input._held_keys and not ctx.input.count('key_down', 'w')


def test_range_confirmation_overrides_marker_distance_without_extra_movement(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    nav = replace(nav, destination=(.9, .9), task=nav.task | TaskFlags.NPC_WITHIN_5_YARDS)
    run.interact_quest_target(ctx, 1, nav, 0)
    run.interact_quest_target(ctx, 2.3, replace(nav, sequence=2), 0)
    assert ctx.input.count('key_down', 'f') == 2
    assert not ctx.input.count('key_down', 'w') and not ctx.input._held_keys


def test_npc_alignment_stale_heading_and_timeout_never_advance(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    ctx.heading_age = lambda: 1
    run.interact_quest_target(ctx, 1, nav, 0)
    assert not ctx.input.count('key_down')
    run.interact_quest_target(ctx, 17, replace(nav, sequence=2), 0)
    assert run.OUTCOME == 'stopped:quest_npc_alignment_timeout'
    assert not ctx.input._held_keys


def test_npc_direction_replanning_has_task_budget(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    for i in range(3):
        run.replan_npc_approach(ctx, i, nav, 5, 7)
    assert run.OUTCOME == 'stopped:quest_npc_approach_direction_unconfirmed'
    assert run.terrain is None and not ctx.input._held_keys


def test_focus_loss_records_application_after_releasing_input(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.set_travel_keys(ctx, frozenset({'w', 'f6'}))
    def foreground():
        assert not ctx.input._held_keys
        return {'application': 'Test app', 'bundle_id': 'test.app'}
    monkeypatch.setattr(quest, 'foreground_app_details', foreground)
    run.stop(ctx, 'game_not_frontmost')
    assert run.OUTCOME == 'stopped:game_not_frontmost'
    assert ('quest_focus_lost', {'application': 'Test app', 'bundle_id': 'test.app'}) in events


def suspend_after_probe(tmp_path, monkeypatch):
    from bot.perception.travel_channel import CameraSample, CameraScene as Scene, CameraResult as Result, RoundStage
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    nav = replace(nav, camera_restore_available=True, suspend_ready=True, suspend_epoch=0,
        round_stage=RoundStage.TURN_IN,
        camera=CameraSample(24, 2, Scene.INTERACT, Scene.PROBE, 3, Result.DISTANCE_VERIFIED))
    run.round_stage, run.round_navigation = RoundStage.TURN_IN, nav
    run.camera_restore_reason = 'terrain_finished'
    run.stop(ctx, 'quest_npc_camera_composition_unresolved')
    assert run.suspend_camera is not None
    return run, ctx, nav, events


def test_normal_suspend_restores_camera_before_sending_f5(tmp_path, monkeypatch):
    from bot.perception.travel_channel import CameraScene as Scene
    run, ctx, nav, events = suspend_after_probe(tmp_path, monkeypatch)
    run.suspend_task(ctx, 1, nav, False)
    run.suspend_task(ctx, 1.7, replace(nav, sequence=2), False)
    assert ctx.input._held_keys == {'f8'} and not ctx.input.count('key_down', 'f5')
    receipt = replace(nav, sequence=3, camera=replace(nav.camera, applied=Scene.INTERACT, request=4, zoom=32))
    run.suspend_task(ctx, 2, receipt, False)
    assert not ctx.input._held_keys and not ctx.input.count('key_down', 'f5')
    run.suspend_task(ctx, 2.1, receipt, False)
    assert ctx.input.count('key_down', 'f5') == 1
    result = next(d for n, d in events if n == 'quest_suspend_camera_result')
    assert result['result'] == 'DISTANCE_VERIFIED' and not result['composition_verified']
    # F5 acknowledgement retains its full budget after cleanup.
    assert run.suspend_request[4] == 2


def test_suspend_camera_timeout_is_bounded_and_not_a_success(tmp_path, monkeypatch):
    run, ctx, nav, events = suspend_after_probe(tmp_path, monkeypatch)
    run.suspend_task(ctx, 1, nav, False)
    run.suspend_task(ctx, 1.7, replace(nav, sequence=2), False)
    run.suspend_task(ctx, 11.1, replace(nav, sequence=3), False)
    assert not ctx.input._held_keys and run.suspend_camera is None
    assert any(n == 'quest_suspend_camera_result' and d['result'] == 'deadline' for n,d in events)
    run.suspend_task(ctx, 11.2, nav, False)
    assert ctx.input.count('key_down', 'f5') == 1


def test_focus_loss_during_suspend_cleanup_never_adds_recovery_input(tmp_path, monkeypatch):
    run, ctx, nav, _ = suspend_after_probe(tmp_path, monkeypatch)
    run.suspend_task(ctx, 1, nav, False)
    run.suspend_task(ctx, 1.7, replace(nav, sequence=2), False)
    before = ctx.input.count('key_down')
    run.stop(ctx, 'game_not_frontmost')
    assert not ctx.input._held_keys and run.suspend_camera is None
    assert ctx.input.count('key_down') == before and run.OUTCOME == 'stopped:game_not_frontmost'


def test_manual_camera_pause_skips_suspend_cleanup_without_overriding_user(tmp_path, monkeypatch):
    run, ctx, nav, events = suspend_after_probe(tmp_path, monkeypatch)
    run.suspend_task(ctx, 1, replace(nav, camera=replace(nav.camera, paused=True)), False)
    assert not ctx.input.count('key_down', 'f8') and run.suspend_camera is None
    assert any(n == 'quest_suspend_camera_result' and d['result'] == 'unavailable_or_unsafe' for n,d in events)
