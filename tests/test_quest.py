import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.input_mock import MockInput
from bot.control.quest_travel import TravelCommand
from bot.core.config import BotConfig
from bot.modules import quest
from bot.perception import channel_spec as spec
from bot.perception.combat_channel import CombatSample, TargetDanger, combat_region
from bot.perception.cv import hostile_nameplate_points
from bot.perception.quest_channel import Dialogue, Objective, QuestKind, QuestState, decode_quest
from bot.perception.travel_channel import (
    CameraSample, CameraScene, CameraResult,
    CorpseLootReceipt, CorpseSource, ForeverStep, LifeState, MinimapGeometry, ObjectiveAction, PetState, RecoverySample, RoundStage,
    PlayerVitals, SupplyAction, TaskFlags, TravelFlags, TravelSample,
)


def image(sequence, state, dialogue=Dialogue.CLOSED, dialogue_id=0, done=0, kind=QuestKind.CAMPAIGN,
          quest_id=12345, required=3, objective=None):
    progress = objective if objective is not None else (
        Objective.COUNTED if state == QuestState.ACTIVE else Objective.UNKNOWN)
    if progress == Objective.RESOURCE:
        progress = Objective.NON_COUNTED  # Extended wire code
    packed = int(kind) | (int(state) << 4) | (int(dialogue) << 7)
    packed |= (int(progress) << 10) | (sequence << 12) | (1 << 19)
    canvas = np.zeros((80, 142, 3), dtype=np.uint8)
    for row, value in enumerate([quest_id, packed, done * 1024 + required, 2248, dialogue_id]):
        spec.render_frame(canvas, 0, row * 16, spec.encode_frame(0xA + row, value))
    return canvas


def test_quest_channel_rejects_misaligned_rows():
    canvas = image(3, QuestState.ACTIVE, done=2)
    sample = decode_quest(canvas)
    assert sample.kind == QuestKind.CAMPAIGN
    assert sample.progress == (2, 3)
    assert sample.tracking and sample.sequence == 3
    canvas[32:48] = canvas[16:32]
    assert decode_quest(canvas) is None


def test_native_travel_pulse_continues_through_idle_frames_and_cancels_before_handoff(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    calls = []
    events = []

    def start(key, hold_s):
        calls.append(("start", key, hold_s))
        return True

    ctx.input.start_navigation_pulse = start
    ctx.input.cancel_navigation_pulse = lambda: calls.append(("cancel",))
    ctx.log_event = lambda name, payload: events.append((name, payload))
    pulse = TravelCommand(tap="left", hold_s=.07, precise=True)
    run.apply_travel_command(ctx, pulse)
    run.apply_travel_command(ctx, TravelCommand())
    assert calls == [("start", "left", .07)]
    assert events[-1][1]["dispatch"] == "native_timer"

    run.set_travel_keys(ctx, frozenset({"w"}))
    assert calls[-1] == ("cancel",)
    assert ctx.input.count("key_down", "w") == 1
    assert ctx.input.count("key_down", "left") == 0

    run.apply_travel_command(ctx, pulse)
    run.apply_travel_command(ctx, TravelCommand(outcome="detoured"))
    assert calls[-2:] == [("start", "left", .07), ("cancel",)]


def test_precise_travel_pulse_preserves_legacy_execution(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.input.start_navigation_pulse = lambda *_: False
    run.apply_travel_command(ctx, TravelCommand(tap="right", hold_s=.07, precise=True))
    assert ctx.input.count("key_down", "right") == ctx.input.count("key_up", "right") == 1


def test_resource_strip_is_distinct_from_counted_and_legacy_non_counted():
    assert decode_quest(image(1, QuestState.ACTIVE, done=680, required=1000,
                              objective=Objective.RESOURCE)).objective == Objective.RESOURCE
    assert decode_quest(image(1, QuestState.ACTIVE, done=680, required=1000)).objective == Objective.COUNTED
    assert decode_quest(image(1, QuestState.ACTIVE, required=0,
                              objective=Objective.NON_COUNTED)).objective == Objective.NON_COUNTED
    assert decode_quest(image(1, QuestState.ACTIVE, done=1001, required=1000,
                              objective=Objective.RESOURCE)) is None


def test_prepared_round_suspends_after_ack_then_advances_without_model(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                        QuestState.PENDING, TravelFlags.KEYS_VERIFIED, 0,
                        task=TaskFlags.PREPARED, round_stage=RoundStage.PICKUP, objective_index=0,
                        suspend_ready=True, suspend_epoch=7)]
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    run.planner = SimpleNamespace(poll=lambda *_: pytest.fail("prepared phases must use game state"))
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav[0], sequence=ctx.frame_sequence()))
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert ctx.input.count("key_down", "9") == 1
    run.stop(ctx, "quest_route_blocked")
    tick(QuestState.PENDING)
    assert ctx.input.count("key_down", "f5") == 1
    nav[0] = replace(nav[0], suspend_epoch=0, current_suspended=True)
    tick(QuestState.PENDING)
    tick(QuestState.PENDING)
    assert ctx.input.count("key_down", "9") == 1 and ctx.stats["quests_suspended"] == 1
    nav[0] = replace(nav[0], round_advance=True)
    tick(QuestState.PENDING)
    assert ctx.input.count("key_down", "9") == 2
    nav[0] = replace(nav[0], quest_id=12346, current_suspended=False, round_advance=False)
    tick(QuestState.PENDING, Dialogue.DETAIL, 12346, quest_id=12346)
    assert ctx.input.count("key_down", "9") == 3 and run.OUTCOME is None
    assert [event["id"] for name, event in events if name == "quest_suspended"] == [12345]


@pytest.mark.parametrize("inherited", [0, 1, 2])
def test_round_summary_includes_saved_suspensions_not_observed_this_run(tmp_path, monkeypatch, inherited):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.READY, TravelFlags.KEYS_VERIFIED, 0,
                       task=TaskFlags.PREPARED, round_stage=RoundStage.TURN_IN,
                       round_pending_count=inherited, addon_build=269, extension_schema=6)]
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav[0], sequence=ctx.frame_sequence()))
    tick(QuestState.READY, Dialogue.REWARD, 12345)
    assert ctx.input.count("key_down", "9") == 1
    nav[0] = replace(nav[0], phase=QuestState.TURNED_IN, round_advance=True)
    tick(QuestState.TURNED_IN)
    nav[0] = replace(nav[0], round_stage=RoundStage.DONE, round_advance=False)
    tick(QuestState.TURNED_IN)
    summary = next(data for name, data in events if name == "quest_round_summary")
    assert summary["pending_count"] == summary["pending_inherited_count"] == inherited
    assert summary["pending_count_verified"] and summary["quests_done"] == 1
    assert summary["pending"] == []  # Do not invent identities/reasons absent from live telemetry.
    assert run.OUTCOME == ("completed_with_pending" if inherited else "completed")
    assert next(data for name, data in events if name == "quest_addon_loaded")["extension_schema"] == 6


def test_legacy_addon_does_not_wait_for_model_when_resuming_prepared_resource_hunt(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(90893, "9", None, None, None, None)]
    nav = TravelSample(90893, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 0,
                       task=TaskFlags.PREPARED, round_stage=RoundStage.OBJECTIVES,
                       objective_action=ObjectiveAction.COMBAT, objective_target=244669)
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav, sequence=ctx.frame_sequence()))
    run.planner = SimpleNamespace(poll=lambda *_: pytest.fail("this reproduces the no-server prepared launch"))
    for _ in range(4):
        tick(QuestState.ACTIVE, done=940, required=1000, quest_id=90893, target=244669, objective=Objective.RESOURCE)
    assert ctx.input.count("key_down", "4") >= 1 and run.OUTCOME is None


def test_no_pet_ordinary_loot_waits_for_matching_corpse_receipt(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244669}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244669, state_epoch=2)
    corpse = SimpleNamespace(target_dead=True, target_id=244669, state_epoch=2, unsafe=False)
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1,
                       task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE, corpse_lootable=True, loot_epoch=10)
    events = []
    ctx.log_event = lambda name, data: events.append(name)
    assert run.loot_corpse(ctx, 0, 0, corpse, nav)
    assert run.loot_corpse(ctx, 1, 1, corpse, nav)
    assert run.loot_corpse(ctx, 3, 1, corpse, nav)
    assert ctx.input.count("key_down", "f") == 2 and not ctx.input.count("key_down", "l")
    assert not run.loot_corpse(ctx, 4, 1, corpse, replace(nav, loot_epoch=11))
    assert events == ["quest_loot", "quest_loot", "quest_loot_confirmed"]


@pytest.mark.parametrize('same_corpse', [True, False])
def test_required_loot_blocks_turn_in_until_same_corpse_is_empty(tmp_path, monkeypatch, same_corpse):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.phase = QuestState.ACTIVE
    run.hunt = quest.QuestHunt(frozenset({244669}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244669, state_epoch=1)
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.READY, TravelFlags.KEYS_VERIFIED, 1, addon_build=323, loot_epoch=4,
        task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE, corpse_lootable=True)]
    monkeypatch.setattr(quest, 'decode_travel', lambda *_, **__: replace(nav[0], sequence=ctx.frame_sequence()))
    tick(QuestState.READY, done=3, target=244669, dead=True)
    assert ctx.input.count('key_down', 'f') == 1
    nav[0] = replace(nav[0], loot_epoch=5)
    tick(QuestState.READY, done=3, target=244669, dead=True, elapsed=.5)
    assert run.phase == QuestState.ACTIVE and run.required_loot
    nav[0] = replace(nav[0], corpse_lootable=False)
    tick(QuestState.READY, done=3, target=244669, dead=True, epoch=1 if same_corpse else 2, elapsed=.5)
    assert bool(run.required_loot) is not same_corpse
    if not same_corpse:
        tick(QuestState.READY, done=3, target=244669, dead=True, epoch=2, elapsed=19)
        assert run.OUTCOME == 'stopped:quest_loot_unconfirmed'
    assert run.phase == QuestState.ACTIVE and not ctx.input.count('key_down', '9')


def test_required_loot_holds_self_defense_corpses_until_combat_ends(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244669}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244669, state_epoch=1)
    run.defense = SimpleNamespace(fight=SimpleNamespace(presses=1, target_id=244669, state_epoch=1))
    frame = SimpleNamespace(quest_id=12345, map_id=2248, state=QuestState.ACTIVE, dialogue=Dialogue.CLOSED)
    corpse = SimpleNamespace(target_id=244669, target_dead=True, state_epoch=1, unsafe=False)
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED | TravelFlags.IN_COMBAT, 1, addon_build=323,
        task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE, corpse_lootable=True, loot_epoch=7)
    assert not run.process_required_loot(ctx, 1, frame, nav, corpse, None)
    assert len(run.required_loot) == 1 and not ctx.input.actions
    nav = replace(nav, flags=TravelFlags.KEYS_VERIFIED, sequence=2)
    assert run.process_required_loot(ctx, 2, frame, nav, corpse, None)
    assert ctx.input.count('key_down', 'f') == 1
    assert run.process_required_loot(ctx, 2.2, frame, replace(nav, corpse_lootable=False), corpse, None)
    assert len(run.required_loot) == 1  # A repeated navigation frame proves nothing.
    assert run.process_required_loot(ctx, 2.4, frame, replace(nav, corpse_lootable=False, sequence=3), corpse, None)
    assert not run.required_loot


def test_required_loot_accepts_fresh_corpse_receipt_after_target_clears(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244669}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244669, state_epoch=4)
    frame = SimpleNamespace(quest_id=12345, map_id=2248, state=QuestState.ACTIVE, dialogue=Dialogue.CLOSED)
    corpse = SimpleNamespace(target_id=244669, target_dead=True, state_epoch=4, unsafe=False)
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, addon_build=324,
        task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE, corpse_lootable=True, loot_epoch=0,
        corpse_loot_receipt=CorpseLootReceipt(1, 4))
    assert run.process_required_loot(ctx, 1, frame, nav, corpse, None)
    assert ctx.input.count('key_down', 'f') == 1
    cleared = SimpleNamespace(target_id=0, target_dead=False, state_epoch=7, unsafe=False)
    nav = replace(nav, task=TaskFlags.PREPARED, corpse_lootable=None, loot_epoch=5)
    for tick, receipt in enumerate((None, CorpseLootReceipt(1, 4), CorpseLootReceipt(2, 7)), start=2):
        assert run.process_required_loot(ctx, tick, frame, replace(nav, sequence=tick, corpse_loot_receipt=receipt),
                                         cleared, None)
        assert len(run.required_loot) == 1
    assert run.process_required_loot(ctx, 5, frame, replace(nav, sequence=5, corpse_loot_receipt=CorpseLootReceipt(2, 4)),
                                     cleared, None)
    assert not run.required_loot and not ctx.input.count('key_down', 'p')


@pytest.mark.parametrize('started_selected', [True, False])
def test_prepared_task_loots_a_nearby_corpse_before_first_combat(tmp_path, monkeypatch, started_selected):
    run, ctx, _ = make_run(tmp_path, monkeypatch, combat=True)
    frame = SimpleNamespace(quest_id=12345, map_id=2248, state=QuestState.ACTIVE, dialogue=Dialogue.CLOSED)
    corpse = SimpleNamespace(target_id=475, target_dead=True, state_epoch=7, previous_target_epoch=None, unsafe=False,
                             facing=0, turn_verified=True, sequence=2)
    no_target = SimpleNamespace(target_id=0, target_dead=False, state_epoch=6, unsafe=False,
                                 facing=0, turn_verified=True, sequence=3)
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, addon_build=325, task=TaskFlags.PREPARED,
        nearby_lootable_corpse=True, loot_epoch=0)
    if not started_selected:
        assert not run.process_required_loot(ctx, 0, frame, replace(nav, nearby_lootable_corpse=False), no_target, None)
        assert not run.process_required_loot(ctx, 0, frame, replace(nav, flags=TravelFlags.IN_COMBAT), no_target, None)
        assert run.process_required_loot(ctx, 1, frame, nav, no_target, None)
        assert ctx.input.count('key_down', 'k') == 1
        assert run.process_required_loot(ctx, 1.2, frame, nav, no_target, None)
        assert not ctx.input.count('key_down', 'f')
    selected = replace(nav, sequence=2, nearby_lootable_corpse=False, corpse_lootable=True,
                       task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE)
    assert run.process_required_loot(ctx, 1.4, frame, selected, corpse, None)
    assert len(run.required_loot) == ctx.input.count('key_down', 'f') == 1
    receipt = replace(nav, sequence=3, nearby_lootable_corpse=False, loot_epoch=1,
                      corpse_loot_receipt=CorpseLootReceipt(1, 7))
    assert run.process_required_loot(ctx, 2, frame, receipt, no_target, None)
    assert not run.required_loot and not ctx.input.count('key_down', 'p')
    hidden = replace(receipt, sequence=4)
    assert run.process_required_loot(ctx, 2.2, frame, hidden, no_target, None)
    assert run.process_required_loot(ctx, 2.4, frame, hidden, no_target, None)
    assert ctx.input.count('key_down', 'left') == 1
    assert ctx.input.count('key_down', 'f') == 1
    assert not run.process_required_loot(ctx, 3, frame, replace(nav, flags=TravelFlags.IN_COMBAT), no_target, None)
    assert run.nearby_loot_scan is None


def test_nearby_loot_checks_another_heading_when_soft_target_disappears(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch, combat=True)
    frame = SimpleNamespace(quest_id=12345, map_id=2248, state=QuestState.ACTIVE, dialogue=Dialogue.CLOSED)
    combat = CombatSample(3, 1, 18, 70959, 1485, 5, 0, 0, False, True, False, 4, 6, 1,
                          facing=0, turn_verified=True)
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, addon_build=325, task=TaskFlags.PREPARED,
        nearby_lootable_corpse=True, loot_epoch=0)
    assert run.process_required_loot(ctx, 1, frame, nav, combat, None)
    lost = replace(nav, nearby_lootable_corpse=False, sequence=2)
    assert run.process_required_loot(ctx, 2.3, frame, lost, combat, None)
    assert run.process_required_loot(ctx, 2.4, frame, lost, combat, None)
    assert ctx.input.count('key_down', 'left') == 1
    aligned = replace(combat, sequence=2, facing=math.radians(80))
    assert run.process_required_loot(ctx, 3, frame, replace(nav, sequence=3), aligned, None)
    assert ctx.input.count('key_down', 'k') == 2 and not ctx.input.count('key_down', 'f')


def test_fresh_v14_objective_replaces_older_quest_strip_progress(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None)]
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 0, task=TaskFlags.PREPARED,
                        objective_index=1, objective_action=ObjectiveAction.COMBAT,
                        objective_target=244669, objective_progress=(0, 3))]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav[0], sequence=ctx.frame_sequence()))
    tick(QuestState.ACTIVE, done=1, required=1, target=244669)
    assert run.phase == QuestState.ACTIVE and run.objective_index == 1 and run.progress == (0, 3)
    nav[0] = replace(nav[0], objective_index=2, objective_progress=(1, 4),
                     objective_action=ObjectiveAction.INTERACT, objective_target=0)
    tick(QuestState.ACTIVE, done=1, required=1)
    assert run.objective_index == 2 and run.progress == (1, 4) and run.hunt is None


def test_run_status_reports_activity_only_when_native_chord_is_verified(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.require_prepared = True
    sent = []
    ctx.input.publish_run_status = lambda code: sent.append(code) or True
    nav = SimpleNamespace(run_status_ready=False, sequence=1)
    run.publish_activity(ctx, 1, 'combat_target_search', nav, None)
    assert not sent
    nav.run_status_ready = True
    run.publish_activity(ctx, 2, 'combat_target_search', nav, None)
    run.publish_activity(ctx, 3, 'npc_search', nav, None)
    run.required_loot.append(object())
    run.publish_activity(ctx, 4, 'combat', nav, None)
    assert sent == [3, 6, 9] and run.status_wire_at == 4


def test_timeout_uses_startlux_without_dropping_a_pending_corpse(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.continuous = True
    run.phase = QuestState.ACTIVE
    corpse = quest.RequiredCorpseLoot(475, 7)
    run.required_loot.append(corpse)
    requests = []
    decisions = iter(('next_task', 'retry'))

    def decide(signature, state, options, **kwargs):
        requests.append((signature, state, options))
        return {'recommendation': next(decisions)}

    run.planner = SimpleNamespace(poll=decide)
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED, suspend_ready=True,
        suspend_epoch=2, round_advance=True)
    frame = SimpleNamespace(quest_id=12345, map_id=2248, state=QuestState.ACTIVE, dialogue=Dialogue.CLOSED)
    run.stop(ctx, 'quest_loot_unconfirmed')
    assert run.timeout_review is not None and run.OUTCOME is None
    assert run.review_timeout(ctx, 100, frame, nav, None, None)
    assert 'next_task' not in requests[0][2] and run.suspend_request is None
    assert run.review_timeout(ctx, 104, frame, nav, None, None)
    assert corpse.started == 104 and run.timeout_review is None
    assert run.required_loot == [corpse] and run.OUTCOME is None
    assert not [action for action in ctx.input.actions if action[0] == 'key_down']


def test_focus_loss_releases_held_movement_without_suspending(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = run.execution_started = True
    ctx.input.name = "helper"
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: False)
    run.set_travel_keys(ctx, frozenset({"w"}))
    tick(QuestState.ACTIVE)
    assert run.OUTCOME == "stopped:game_not_frontmost" and run.suspend_request is None
    assert ctx.input.count("key_down", "w") == ctx.input.count("key_up", "w") == 1


@pytest.mark.parametrize("interruption", ["focus", "capture"])
def test_online_controller_waits_for_fresh_frames_after_interruption(tmp_path, monkeypatch, interruption):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    state = quest.NavigateCurrentQuest(None, run.kinds, False, True)
    state.runner = run
    ctx.input.supports_focus_pause = True
    ctx.input.focus_epoch = None
    ctx.invalidate_game_observation = lambda: None
    corpse = quest.RequiredCorpseLoot(475, 7)
    run.required_loot.append(corpse)
    phase = QuestState.ACTIVE
    frame = [SimpleNamespace(quest_id=12345, map_id=2248, state=phase, sequence=1, tracking=True,
                             dialogue=Dialogue.CLOSED, dialogue_id=0)]
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                        phase, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED)]
    focused = [True]
    resumed = []
    fail_capture = [interruption == "capture"]
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: focused[0])
    monkeypatch.setattr(quest, "decode_travel", lambda _: nav[0])
    monkeypatch.setattr(quest, "decode_quest", lambda _: frame[0])

    def task_tick(_):
        if fail_capture[0]:
            raise quest.CaptureError("window moved")
        resumed.append("task")

    def resume_foreground(epoch):
        assert epoch == 4
        ctx.input.focus_epoch = None
        resumed.append("foreground")

    monkeypatch.setattr(state, "tick_task", task_tick)
    ctx.input.resume_foreground = resume_foreground
    run.set_travel_keys(ctx, frozenset({"w"}))
    if interruption == "focus":
        focused[0], ctx.input.focus_epoch = False, 3
    state.tick(ctx)
    assert state.online_wait and state.OUTCOME is None and not ctx.input._held_keys
    assert run.required_loot == [corpse]
    fail_capture[0] = False
    if interruption == "focus":
        focused[0], ctx.input.focus_epoch = True, 4
        state.tick(ctx)
        assert resumed == ["foreground"]
    state.tick(ctx)
    state.tick(ctx)
    assert "task" not in resumed and state.online_wait
    nav[0] = replace(nav[0], sequence=2)
    state.tick(ctx)
    assert "task" not in resumed and state.online_wait
    frame[0].sequence = 2
    state.tick(ctx)
    assert state.online_wait is None and state.runner.required_loot == [corpse]
    state.tick(ctx)
    assert resumed[-1] == "task"


def test_online_controller_waits_for_changed_task_context_without_restarting_failed_step(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    state = quest.NavigateCurrentQuest(None, run.kinds, False, True)
    state.runner = run
    ctx.input.supports_focus_pause = True
    ctx.input.focus_epoch = None
    ctx.invalidate_game_observation = lambda: None
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED,
                        objective_progress=(0, 1))]
    frame = SimpleNamespace(quest_id=12345, map_id=2248, state=QuestState.ACTIVE, sequence=1,
                            tracking=True, dialogue=Dialogue.CLOSED, dialogue_id=0)
    run.observation = run.observations.snapshot(100, frame, nav[0], None)
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: True)
    monkeypatch.setattr(quest, "decode_travel", lambda _: nav[0])
    monkeypatch.setattr(quest, "decode_quest", lambda _: frame)
    attempts = []

    def failed_step(_):
        attempts.append(True)
        state.OUTCOME = "stopped:quest_route_blocked"

    monkeypatch.setattr(state, "tick_task", failed_step)
    state.tick(ctx)
    for sequence in range(2, 6):
        frame.sequence = sequence
        nav[0] = replace(nav[0], sequence=sequence)
        state.tick(ctx)
    assert state.OUTCOME is None and state.online_wait and attempts == [True]
    nav[0] = replace(nav[0], objective_progress=(1, 1), sequence=6)
    frame.sequence = 6
    state.tick(ctx)
    nav[0] = replace(nav[0], sequence=7)
    frame.sequence = 7
    state.tick(ctx)
    assert state.online_wait is None and attempts == [True]


def test_probe_quests_waypoints_reads_live_position_frames(monkeypatch, capsys):
    from bot import cli

    clock = [0.0]
    pos_frame = np.zeros((16, 142, 3), dtype=np.uint8)
    spec.render_frame(pos_frame, 0, 0, spec.encode_frame(spec.MSG_POS, spec.pack_pos(.4, .5)))
    quest_frame = image(1, QuestState.ACTIVE, done=2)
    capture_frame = np.vstack((quest_frame, pos_frame))

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(cli, "ScreenCapture",
                        lambda cfg: SimpleNamespace(grab=lambda *rect: capture_frame))
    assert cli.main(["probe-quests", "--waypoints", "--duration", ".1"]) == 0
    assert "progress=(2, 3) map=2248 dialogue=CLOSED:0 waypoint=[0.3998, 0.5005]" in capsys.readouterr().out


def make_run(tmp_path, monkeypatch, *, combat=False):
    path = tmp_path / "plan.yaml"
    plan = """
map_id: 2248
quests:
  - id: 12345
    dialog_key: '9'
    pickup: [[0.4, 0.5]]
    objective: [[0.4, 0.5]]
    objective_key: '8'
    turn_in: [[0.4, 0.5]]
"""
    if combat:
        plan = plan.replace("objective_key: '8'", "combat_targets: [244669]")
    path.write_text(plan)
    clock = [100.0]
    sequence = 0
    monkeypatch.setattr(quest, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    frame = [None]
    combat_frame = [None]
    flags = [spec.StatusFlags.HP_UNKNOWN]
    ctx = SimpleNamespace(
        cfg=BotConfig(), input=MockInput(), stats={}, frame_sequence=lambda: sequence,
        capture=SimpleNamespace(grab=lambda *region: combat_frame[0] if region[-1] == combat_region(0, 0)[-1] else frame[0]),
        channel=SimpleNamespace(status=lambda **_: (flags[0], 0)),
        pos=lambda: (0.4, 0.5), heading=lambda: 0.0, heading_age=lambda: 0.0, log_event=lambda *_: None,
    )
    run = quest.RunQuests(*quest.load_quest_plan(path), {QuestKind.CAMPAIGN})
    run.on_enter(ctx)

    def tick(state, dialogue=Dialogue.CLOSED, dialogue_id=0, done=0, kind=QuestKind.CAMPAIGN,
             *, target=0, dead=False, fighting=False, epoch=1, verified=True, elapsed=2.1, quest_id=12345,
             required=3, engaged=False, facing=None, facing_error=0, target_health=None, objective=None,
             danger=TargetDanger(0)):
        nonlocal sequence
        clock[0] += elapsed
        sequence = (sequence + 1) % 128
        frame[0] = image(sequence, state, dialogue, dialogue_id, done, kind, quest_id, required, objective)
        if combat:
            flags[0] = spec.StatusFlags.HP_UNKNOWN | (spec.StatusFlags.IN_COMBAT if fighting else 0)
            combat_frame[0] = np.zeros((144, 142, 3), dtype=np.uint8)
            identity = 8 | (1 << 5) | (1 << 13)
            status = int(dead) | (int(verified) << 1) | (4 << 3) | (epoch << 7) | (sequence << 13)
            spec_level = 63 | (80 << 12) | (int(engaged) << 19)
            for row, value in enumerate([identity, 70783, spec_level, 123, target, status]):
                spec.render_frame(combat_frame[0], 0, (row + 3) * 16, spec.encode_frame(0xA + row, value))
            if facing is not None:
                angle = round(facing * 511 / math.tau) % 511 + 1
                facing_bits = angle | sequence << 9 | facing_error << 16 | 1 << 19
                spec.render_frame(combat_frame[0], 0, 32, spec.encode_frame(9, facing_bits))
            if target_health is not None or danger:
                feedback = (0 if target_health is None else target_health + 1) | sequence << 8 | int(danger) << 15
                spec.render_frame(combat_frame[0], 0, 0, spec.encode_frame(7, feedback))
        run.tick(ctx)

    return run, ctx, tick


@pytest.mark.parametrize("acknowledged", [True, False])
def test_prepared_quest_waits_for_camera_before_tab(tmp_path, monkeypatch, acknowledged):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1,
                        task=TaskFlags.PREPARED, camera_restore_available=True)]
    events = []
    ctx.log_event = lambda event, payload: events.append(event)
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav[0], sequence=ctx.frame_sequence()))
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE, elapsed=1.0)
    assert ctx.input.count("key_down", "f8") == 1 and ctx.input.count("key_down", "tab") == 0
    if acknowledged:
        nav[0] = replace(nav[0], camera_epoch=1)
        tick(QuestState.ACTIVE, elapsed=1.4)
        assert "quest_camera_view_reported" in events
        assert ctx.input.count("key_down", "tab") == 0
    else:
        tick(QuestState.ACTIVE, elapsed=2.9)
        assert "quest_camera_restore_unconfirmed" in events
    tick(QuestState.ACTIVE, elapsed=.5)
    tick(QuestState.ACTIVE, elapsed=.2)
    assert ctx.input.count("key_down", "tab") == 1


def test_camera_restores_on_safe_landing_and_once_per_zoom_obstruction(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, camera_restore_available=True)
    assert not run.restore_camera(ctx, 1, replace(nav, flags=TravelFlags.FLYING))
    assert not run.restore_camera(ctx, 2, replace(nav, flags=TravelFlags.FALLING))
    assert not run.restore_camera(ctx, 3, nav, in_combat=True)
    assert not run.restore_camera(ctx, 4, nav, dialogue_open=True)
    assert not ctx.input.count("key_down", "f8")
    assert run.restore_camera(ctx, 5, nav)
    nav = replace(nav, camera_epoch=1)
    assert run.restore_camera(ctx, 5.9, nav)
    close = replace(nav, camera_zoom_close=True)
    assert not run.restore_camera(ctx, 10, close)
    assert run.restore_camera(ctx, 10.6, close)
    close = replace(close, camera_epoch=0)
    assert run.restore_camera(ctx, 13.1, close)
    assert not run.restore_camera(ctx, 15, close)
    assert not run.restore_camera(ctx, 20, close)
    assert ctx.input.count("key_down", "f8") == 2


def test_scene_camera_is_in_prepared_task_flow_before_target_selection(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED,
                       camera_restore_available=True, protocol_version=15,
                       camera=CameraSample(8, 5, CameraScene.RANGED, CameraScene.UNKNOWN, 0, CameraResult.IDLE))]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav[0], sequence=ctx.frame_sequence()))
    tick(QuestState.ACTIVE, elapsed=.1)
    assert ctx.input.count("key_down", "tab") == 0
    tick(QuestState.ACTIVE, elapsed=.7)
    assert "f8" in ctx.input._held_keys and ctx.input.count("key_down", "tab") == 0
    nav[0] = replace(nav[0], camera=replace(nav[0].camera, zoom=26, applied=CameraScene.RANGED,
                                          request=1, result=CameraResult.DISTANCE_VERIFIED))
    tick(QuestState.ACTIVE, elapsed=.3)
    assert "f8" not in ctx.input._held_keys
    tick(QuestState.ACTIVE, elapsed=.5)
    tick(QuestState.ACTIVE, elapsed=.3)
    assert ctx.input.count("key_down", "tab") == 1


def test_scene_camera_yields_to_flight_pitch_and_focus_loss_releases_lease(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    run.trip = SimpleNamespace(phase="sky_glide", sky_pending_cast=None)
    nav = TravelSample(12345, 2248, 2248, (.6, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.FLYING, 1, camera_restore_available=True,
                       camera=CameraSample(20, 5, CameraScene.FLIGHT, CameraScene.UNKNOWN, 0, CameraResult.IDLE))
    run.set_travel_keys(ctx, frozenset({"w"}))
    assert not run.restore_camera(ctx, 1, nav)
    assert not run.restore_camera(ctx, 1.7, replace(nav, sequence=2))
    assert ctx.input._held_keys == {"w", "f8"}
    run.trip.phase = "sky_land"
    assert not run.restore_camera(ctx, 1.8, replace(nav, sequence=3))
    assert ctx.input._held_keys == {"w"}
    run.trip.phase = "sky_glide"
    run.camera.handled = None
    run.restore_camera(ctx, 7, replace(nav, sequence=4))
    assert "f8" in ctx.input._held_keys
    ctx.input.name = "helper"
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: False)
    run.tick(ctx)
    assert run.OUTCOME == "stopped:game_not_frontmost" and not ctx.input._held_keys


def test_scene_camera_invalidates_screen_candidates_but_preserves_map_goal(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    nav = TravelSample(12345, 2248, 2248, (.6, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, camera_restore_available=True,
                       camera=CameraSample(8, 5, CameraScene.GROUND, CameraScene.UNKNOWN, 0, CameraResult.IDLE))
    run.object_points, run.object_target = [(400, 300)], (.6, .5)
    run.restore_camera(ctx, 0, nav)
    run.restore_camera(ctx, .7, replace(nav, sequence=2))
    assert run.object_points is None and run.object_target == (.6, .5)
    run.object_click_at = .8
    assert not run.restore_camera(ctx, .9, replace(nav, sequence=3))
    assert "f8" not in ctx.input._held_keys


def test_scene_camera_recovers_after_confirmed_dismount_despite_flight_cooldown(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    run.trip = SimpleNamespace(phase="sky_dismount", motion_position=(.4, .5))
    run.camera_restore_reason, run.camera.cooldown = "landing", 30
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.READY, TravelFlags.MOUNTED, 1, camera_restore_available=True,
                       camera=CameraSample(28, 5, CameraScene.INTERACT, CameraScene.FLIGHT, 1,
                                           CameraResult.INTERRUPTED))
    assert not run.restore_camera(ctx, 1, nav)
    nav = replace(nav, flags=TravelFlags.KEYS_VERIFIED, sequence=2)
    assert run.restore_camera(ctx, 1.2, nav)
    assert "f8" not in ctx.input._held_keys
    assert run.restore_camera(ctx, 1.9, replace(nav, sequence=3))
    assert "f8" in ctx.input._held_keys and run.camera_restore_reason is None


def test_quest_progresses_from_offer_to_confirmed_turn_in(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    tick(QuestState.PENDING)
    tick(QuestState.PENDING)
    tick(QuestState.PENDING, Dialogue.OFFER, 12345)
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE, done=2)
    tick(QuestState.READY)
    tick(QuestState.READY)
    tick(QuestState.READY, Dialogue.REWARD, 12345)
    assert run.OUTCOME is None
    tick(QuestState.TURNED_IN)
    assert run.OUTCOME == "completed" and ctx.stats["quests_done"] == 1
    assert ctx.input.count("key_down", "f") == 2
    assert ctx.input.count("key_down", "9") == 3
    assert ctx.input.count("key_down", "8") == 2


def test_auto_turn_in_faces_marker_before_interacting(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = True
    east = 1.5 * math.pi
    heading = [east]
    position = [(.3975, .5)]  # Outside the 4-yard marker search area.
    interaction = [TaskFlags.PREPARED | TaskFlags.NPC_CONFIGURED]
    ctx.heading = lambda: heading[0]

    def navigation(_):
        return TravelSample(12345, 2248, 2248, (.403, .5), position[0], (1000, 1000),
                            0, QuestState.READY, TravelFlags.KEYS_VERIFIED,
                            ctx.frame_sequence(), interact_verified=True, task=interaction[0],
                            npc_range_supported=True)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.READY)
    assert not run.arrived and ctx.input.count("key_down", "w") == 1
    assert ctx.input.count("key_down", "f") == 0
    position[0] = (.3995, .5)  # Enter search area, without walking onto the NPC pin.
    tick(QuestState.READY)
    assert run.arrived
    heading[0] = 0
    tick(QuestState.READY)
    assert ctx.input.count("key_down", "right") or ctx.input.count("key_down", "left")
    assert ctx.input.count("key_down", "f") == 0
    heading[0] = east
    interaction[0] |= TaskFlags.LOOT_IN_RANGE
    tick(QuestState.READY)
    assert ctx.input.count("key_up", "right") or ctx.input.count("key_up", "left")
    assert ctx.input.count("key_down", "f") == 0
    interaction[0] |= TaskFlags.NPC_CANDIDATE
    tick(QuestState.READY)
    assert ctx.input.count("key_down", "k") == 1
    assert ctx.input.count("key_down", "f") == 0
    interaction[0] |= TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    tick(QuestState.READY)
    assert ctx.input.count("key_down", "f") == 1
    tick(QuestState.READY, Dialogue.REWARD, 12345)
    assert ctx.input.count("key_down", "9") == 1
    tick(QuestState.TURNED_IN)
    assert run.OUTCOME == "completed"


def test_forever_restores_verified_corpse_for_loot_after_kill_credit(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    run.hunt = quest.QuestHunt(frozenset({6}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=6, state_epoch=2)
    run.hunt.baseline = 8
    no_target = CombatSample(3, 1, 18, 70959, 1485, 2, 0, 0, False, False, False, 0, 3, 1)
    nav = TravelSample(7, 1429, 1429, (.5, .5), (.5, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1,
                       task=TaskFlags.PREPARED, last_target_verified=True)
    assert run.loot_corpse(ctx, 0, 9, no_target, nav)
    assert ctx.input.count("key_down", "p") == 1
    corpse = replace(no_target, target_dead=True, target_id=6, sequence=2, previous_target_epoch=2)
    assert run.loot_corpse(ctx, .2, 9, corpse, nav)
    assert ctx.input.count("key_down", "f") == 0
    nav = replace(nav, task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE, corpse_lootable=True, loot_epoch=5)
    assert run.loot_corpse(ctx, 1.6, 9, corpse, nav)
    assert ctx.input.count("key_down", "f") == 1


def test_pickup_resumes_search_when_candidate_disappears_after_selecting(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    east = 1.5 * math.pi
    heading = [east]
    task = [TaskFlags.PREPARED | TaskFlags.NPC_CANDIDATE]
    ctx.heading = lambda: heading[0]

    def navigation(_):
        return TravelSample(12345, 2248, 2248, (.403, .5), (.4, .5), (1000, 1000), 0,
                            QuestState.PENDING, TravelFlags.KEYS_VERIFIED, ctx.frame_sequence(),
                            interact_verified=True, task=task[0], npc_range_supported=True)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.PENDING)
    assert ctx.input.count("key_down", "k") == 1
    task[0] = TaskFlags.PREPARED
    tick(QuestState.PENDING, elapsed=.5)
    tick(QuestState.PENDING, elapsed=.6)
    tick(QuestState.PENDING, elapsed=3.5)
    assert run.OUTCOME is None
    assert ctx.input.count("key_down", "left") == 1
    assert ctx.input.count("key_down", "f") == 0
    heading[0] = east + math.radians(30)
    task[0] |= TaskFlags.NPC_CANDIDATE
    tick(QuestState.PENDING)
    assert ctx.input.count("key_down", "k") == 2
    task[0] |= TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    tick(QuestState.PENDING)
    assert ctx.input.count("key_down", "f") == 1
    tick(QuestState.PENDING, Dialogue.BLOCKED, elapsed=.4)
    assert ctx.input.count("key_down", "9") == 1 and run.OUTCOME is None
    tick(QuestState.PENDING, Dialogue.BLOCKED, elapsed=.4)
    assert ctx.input.count("key_down", "9") == 1
    task[0] = TaskFlags.PREPARED
    tick(QuestState.PENDING, elapsed=.5)
    task[0] |= TaskFlags.NPC_CANDIDATE | TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    tick(QuestState.PENDING)
    assert ctx.input.count("key_down", "f") == 2
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert ctx.input.count("key_down", "9") == 2


def test_prepared_start_waits_for_foreground_before_reading_quest(monkeypatch):
    now, focused, captures = [0.0], [False], []
    monkeypatch.setattr(quest, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: focused[0])
    monkeypatch.setattr(quest, "decode_travel", lambda _: None)
    start = quest.NavigateCurrentQuest(None, {QuestKind.CAMPAIGN}, False, require_prepared=True)
    ctx = SimpleNamespace(input=SimpleNamespace(name="helper"), cfg=BotConfig(),
                          capture=SimpleNamespace(grab=lambda *region: captures.append(region)),
                          log_event=lambda *_: None)
    start.on_enter(ctx)
    now[0] = 120
    start.tick(ctx)
    assert start.OUTCOME is None and not captures
    focused[0] = True
    now[0] = 121
    start.tick(ctx)
    assert start.OUTCOME is None and captures
    now[0] = 300
    start.tick(ctx)
    assert start.OUTCOME is None and start.runner is None


@pytest.mark.parametrize('prepared_in_combat', [True, False])
def test_prepared_start_in_combat_uses_current_bindings_or_waits_for_preparation(monkeypatch, prepared_in_combat):
    now = [10.0]
    events = []
    nav = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.ACTIVE, TravelFlags.IN_COMBAT | TravelFlags.KEYS_VERIFIED, 1,
        task=TaskFlags.PREPARED, npc_range_supported=True) if prepared_in_combat else None]
    monkeypatch.setattr(quest, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(quest, 'decode_travel', lambda _: nav[0])
    start = quest.NavigateCurrentQuest(None, {QuestKind.CAMPAIGN}, False, require_prepared=True)
    ctx = SimpleNamespace(input=MockInput(), cfg=BotConfig(), capture=SimpleNamespace(grab=lambda *_: None),
        log_event=lambda name, data: events.append(name))
    start.on_enter(ctx)
    start.tick(ctx)
    assert start.runner is None and start.OUTCOME is None
    if not prepared_in_combat:
        now[0] += 200
        start.tick(ctx)
        assert events == ['quest_start_waiting'] and start.OUTCOME is None
        now[0] += 6
        start.tick(ctx)
        assert start.runner is None and start.OUTCOME is None
        nav[0] = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
            QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED, npc_range_supported=True)
        start.tick(ctx)
    nav[0] = replace(nav[0], sequence=2)
    now[0] += .2
    start.tick(ctx)
    assert start.runner is not None and start.OUTCOME is None
    assert events[-1] == 'quest_navigation_start' and not ctx.input.actions


def test_cold_start_requests_preparation_once_after_fresh_matching_binding(monkeypatch):
    events, requests = [], []
    flags = quest.StatusFlags
    status = [flags.IN_COMBAT]
    nav = [None]
    monkeypatch.setattr(quest, "decode_travel", lambda _: nav[0])
    monkeypatch.setattr(quest, "decode_combat", lambda _: SimpleNamespace(build=70959))
    start = quest.NavigateCurrentQuest(None, {QuestKind.CAMPAIGN}, False, require_prepared=True)
    ctx = SimpleNamespace(
        input=MockInput(), cfg=BotConfig(game_version="1.60.1"),
        capture=SimpleNamespace(grab=lambda *_: None),
        channel=SimpleNamespace(status=lambda **_: (status[0], 15)),
        log_event=lambda name, data: events.append(name),
    )
    ctx.input.request_quest_preparation = lambda: requests.append(True) or True
    start.on_enter(ctx)
    start.tick(ctx)
    status[0] = flags.QUEST_PREPARE_KEY | flags.HEARTBEAT
    start.tick(ctx)
    assert not requests and start.OUTCOME is None
    status[0] = flags.QUEST_PREPARE_KEY | flags.QUEST_PREPARE_FOREVER | flags.MOUNTED
    start.tick(ctx)
    start.tick(ctx)
    assert not requests
    status[0] |= flags.HEARTBEAT
    start.tick(ctx)
    start.tick(ctx)
    assert requests == [True] and events.count("quest_start_prepare_requested") == 1
    nav[0] = TravelSample(12345, 1429, 1429, (.4, .5), (.4, .5), (1000, 1000), 0,
        QuestState.ACTIVE, TravelFlags.MOUNTED | TravelFlags.KEYS_VERIFIED, 1,
        task=TaskFlags.PREPARED, npc_range_supported=True)
    start.tick(ctx)
    assert start.runner is None
    nav[0] = replace(nav[0], sequence=2)
    start.tick(ctx)
    assert start.runner is not None and not ctx.input.actions


def test_prepared_runner_waits_on_focus_loss_during_initial_decision(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.require_prepared = True
    ctx.input.name = "helper"
    focused, decision_ready, requests = [True], [False], []
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: focused[0])

    def poll(signature, *_):
        requests.append(signature)
        return {"recommendation": "pick_up"} if decision_ready[0] else None

    run.planner = SimpleNamespace(poll=poll)
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    focused[0] = False
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345, elapsed=20)
    assert run.OUTCOME is None and len(requests) == 1 and not ctx.input.actions
    focused[0], decision_ready[0] = True, True
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert requests[0] != requests[1]
    assert ctx.input.count("key_down", "9") == 1
    focused[0] = False
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert run.OUTCOME == "stopped:game_not_frontmost"
    assert ctx.input.count("key_down", "9") == 1


@pytest.mark.parametrize("offer", [Dialogue.DETAIL, Dialogue.CLOSED])
@pytest.mark.parametrize("next_id", [90883, 67890])
def test_turn_in_hands_off_and_continues_after_acceptance(tmp_path, monkeypatch, offer, next_id):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    map_sample = [12345, QuestState.TURNED_IN]

    def navigation(_):
        quest_id, phase = map_sample
        return TravelSample(quest_id, 2248, 2248, (.404, .5), (.4, .5), (1000, 1000), 0,
                            phase, TravelFlags.KEYS_VERIFIED, ctx.frame_sequence(),
                            interact_verified=True, task=TaskFlags.PREPARED, npc_range_supported=True,
                            objective_action=ObjectiveAction.ARRIVE if quest_id == 67890 else ObjectiveAction.NONE)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.TURNED_IN, offer, next_id)
    assert run.OUTCOME is None and ctx.stats["quests_done"] == 1
    assert ctx.input.count("key_down", "9") == 1
    map_sample[:] = [next_id, QuestState.PENDING]
    if offer == Dialogue.CLOSED:
        tick(QuestState.PENDING, quest_id=next_id)
        assert ctx.input.count("key_down", "9") == 1
    tick(QuestState.PENDING, Dialogue.DETAIL, next_id, quest_id=next_id)
    assert ctx.input.count("key_down", "9") == 2
    map_sample[1] = QuestState.ACTIVE
    tick(QuestState.ACTIVE, quest_id=next_id)
    assert ctx.stats["quests_accepted"] == 1
    assert run.trip is not None and run.OUTCOME is None


def test_mount_tutorial_clicks_verified_icon_before_travel(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(90883, "9", None, None, None, None)]
    ctx.cfg.channel.x, ctx.cfg.channel.y = -1822, 982
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (-1920, 0, 1920, 1080))
    button, destination = [None], [None]

    def navigation(_):
        task = TaskFlags.PREPARED
        if not destination[0]:
            task |= TaskFlags.MOUNT_TUTORIAL
        return TravelSample(90883, 2248, 2248, destination[0], (.4, .5), (1000, 1000), 0,
                            QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, ctx.frame_sequence(),
                            task=task, mount_button=button[0], mount_button_key="2" if button[0] else None)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.ACTIVE, quest_id=90883, required=1)
    assert not ctx.input.actions and run.trip is None
    button[0] = (.4, .95)
    tick(QuestState.ACTIVE, quest_id=90883, required=1)
    tick(QuestState.ACTIVE, quest_id=90883, required=1)
    assert ctx.input.count("move", -1152, 1026) == 1
    assert ctx.input.count("mouse_down", "left") == 1
    assert ctx.input.count("key_down", "6") == 0 and run.trip is None
    tick(QuestState.ACTIVE, quest_id=90883, required=1)
    tick(QuestState.ACTIVE, quest_id=90883, required=1, elapsed=4.1)
    assert ctx.input.count("mouse_down", "left") == 1
    assert ctx.input.count("key_down", "2") == 1
    button[0], destination[0] = None, (.5, .5)
    tick(QuestState.ACTIVE, quest_id=90883, done=1, required=1)
    assert run.trip is not None and run.OUTCOME is None


def test_unknown_quest_accepts_interacts_and_turns_in_with_live_evidence(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(67890, "9", None, None, None, None)]
    ctx.cfg.channel.x, ctx.cfg.channel.y = -1822, 982
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (-1920, 0, 1920, 1080))
    phase, cursor, destination = [QuestState.PENDING], [(.52, .50)], [(.4, .5)]

    def navigation(_):
        task = TaskFlags.PREPARED
        if phase[0] == QuestState.READY:
            task |= TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
        return TravelSample(67890, 2248, 2248, destination[0], (.4, .5), (1000, 1000), 0, phase[0],
                            TravelFlags.KEYS_VERIFIED, ctx.frame_sequence(), interact_verified=True,
                            task=task, npc_range_supported=True, objective_action=ObjectiveAction.INTERACT,
                            objective_cursor=cursor[0], objective_pin=phase[0] == QuestState.ACTIVE)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.PENDING, Dialogue.DETAIL, 67890, quest_id=67890)
    assert ctx.input.count("key_down", "9") == 1
    phase[0] = QuestState.ACTIVE
    tick(QuestState.ACTIVE, quest_id=67890)
    tick(QuestState.ACTIVE, quest_id=67890)
    assert ctx.input.count("move") == 1 and not ctx.input.count("mouse_down")
    tick(QuestState.ACTIVE, quest_id=67890)
    assert ctx.input.count("mouse_down", "right") == 1
    cursor[0], destination[0] = None, (.43, .5)
    tick(QuestState.ACTIVE, quest_id=67890)
    assert ctx.input.count("move") == 1 and run.trip.destination == (.4, .5)
    tick(QuestState.ACTIVE, quest_id=67890, done=1)
    assert run.trip.destination == (.43, .5)
    assert not ctx.input.count("key_down", "tab")
    phase[0] = QuestState.READY
    tick(QuestState.READY, quest_id=67890)
    assert ctx.input.count("key_down", "f") == 1
    tick(QuestState.READY, Dialogue.REWARD, 67890, quest_id=67890)
    assert ctx.input.count("key_down", "9") == 2
    phase[0] = QuestState.TURNED_IN
    tick(QuestState.TURNED_IN, quest_id=67890)
    tick(QuestState.TURNED_IN, quest_id=67890, elapsed=4.1)
    assert run.OUTCOME == "completed" and ctx.stats == {"quests_accepted": 1, "quests_done": 1}


@pytest.mark.parametrize("quest_id,rotates,dot", [
    (90885, False, (.10, 0)), (90885, True, (0, -.10)), (67890, False, (.10, 0)),
])
def test_object_quest_walks_to_minimap_dot_before_clicking(tmp_path, monkeypatch, quest_id, rotates, dot):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(quest_id, "9", None, None, None, None)]
    ctx.cfg.channel.x, ctx.cfg.channel.y = -1822, 982
    ctx.heading = lambda: 1.5 * math.pi
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (-1920, 0, 1920, 1080))
    dots = [dot]
    monkeypatch.setattr(quest, "minimap_quest_dots", lambda _: tuple(dots))
    probe = (.52, .50)
    monkeypatch.setattr(quest, "pumpkin_probe_points", lambda *_: (probe,))

    position = [(.4, .5)]

    def navigation(_):
        return TravelSample(quest_id, 2248, 2248, (.4, .5), position[0], (1000, 1000), 0,
                            QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, ctx.frame_sequence(),
                            task=TaskFlags.PREPARED, objective_action=ObjectiveAction.INTERACT,
                            objective_cursor=probe, precise_object=True,
                            minimap=MinimapGeometry((.90, .15), (.1, .17), 100, rotates))

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.ACTIVE, quest_id=quest_id)
    tick(QuestState.ACTIVE, quest_id=quest_id)
    tick(QuestState.ACTIVE, quest_id=quest_id)
    assert run.object_target == pytest.approx((.41, .5))
    assert ctx.input.count("key_down", "w") == 1
    position[0] = (.406, .5)
    tick(QuestState.ACTIVE, quest_id=quest_id)
    assert not ctx.input.count("move") and not ctx.input.count("mouse_down")
    position[0] = (.409, .5)
    tick(QuestState.ACTIVE, quest_id=quest_id)
    assert ctx.input.count("key_up", "w") == 1
    tick(QuestState.ACTIVE, quest_id=quest_id)
    assert ctx.input.count("move") == 1 and not ctx.input.count("mouse_down")
    tick(QuestState.ACTIVE, quest_id=quest_id)
    assert ctx.input.count("mouse_down", "right") == 1
    dots[0] = (0, -.02) if rotates else (.02, 0)
    tick(QuestState.ACTIVE, quest_id=quest_id, elapsed=5.1)
    assert not run.arrived and run.object_approach_yards == 1.25
    assert ctx.input.count("mouse_down", "right") == 1


@pytest.mark.parametrize("player_x,keep_target", [(.413, False), (.427, True)])
def test_minimap_recheck_refuses_to_redirect_to_a_different_dot(tmp_path, monkeypatch, player_x, keep_target):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    run.object_target, run.object_check_origin = (.44, .5), (.4, .5)
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1920, 1080))
    nav = TravelSample(90885, 2248, 2248, (.4, .5), (player_x, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1)
    attempts = []

    def find_dot(*_, near):
        attempts.append(near)
        return None

    monkeypatch.setattr(run, "object_minimap_target", find_dot)
    assert not run.recheck_object_target(ctx, 10, nav, 0) and not attempts
    assert not run.recheck_object_target(ctx, 10.4, nav, 0) and not attempts
    assert run.recheck_object_target(ctx, 10.8, replace(nav, sequence=2), 0) == keep_target
    assert attempts == [(.44, .5)]
    assert run.object_target == ((.44, .5) if keep_target else None)
    assert run.object_targets_tried == ([] if keep_target else [(.44, .5)])


def test_catapult_uses_visible_long_range_dot_and_keeps_remaining_dot_after_credit(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(90895, "9", None, None, None, None)]
    ctx.cfg.channel.x, ctx.cfg.channel.y = 40, 980
    ctx.heading = lambda: 1.5 * math.pi
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1920, 1080))
    dots = [(.4, 0), (.5, 0)]  # 93 and 116 yards at the reported zoom.
    monkeypatch.setattr(quest, "minimap_quest_dots", lambda _: tuple(dots))
    nav = TravelSample(90895, 2248, 2248, (.43, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1,
                       task=TaskFlags.PREPARED, objective_action=ObjectiveAction.INTERACT,
                       precise_object=True, minimap=MinimapGeometry((.90, .15), (.1, .17), 233, False))
    current = [nav]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))

    tick(QuestState.ACTIVE, quest_id=90895, done=1, required=4)
    assert run.object_target == pytest.approx((.4932, .5))
    tick(QuestState.ACTIVE, quest_id=90895, done=1, required=4)
    current[0] = replace(nav, position=(.490, .5), objective_cursor=(.52, .50))
    dots.clear()
    for _ in range(4):
        tick(QuestState.ACTIVE, quest_id=90895, done=1, required=4)
    assert ctx.input.count("world_click") == 1
    tick(QuestState.ACTIVE, quest_id=90895, done=2, required=4)
    assert run.object_target == pytest.approx((.5165, .5))
    assert any(name == "quest_object_minimap_target" and data["remembered"] for name, data in events)
    assert all(data["destination"] != nav.destination for name, data in events if name == "quest_route")


def test_object_route_keeps_last_ground_target_during_flight(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    run.object_target = (.49, .5)
    east = 1.5 * math.pi
    nav = TravelSample(90895, 2248, 2248, (.48, .5), (.4, .5), (1000, 1000), 100,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED | TravelFlags.CAN_FLY, 1,
                       descent_key="x")
    rechecks = []
    monkeypatch.setattr(run, "recheck_object_target", lambda *args: rechecks.append(args[2].sequence) or True)
    run.travel(ctx, 0, nav, east)
    assert ctx.input.count("key_down", "6") == 1 and rechecks == [1]
    airborne = replace(nav, flags=nav.flags | TravelFlags.FLYING | TravelFlags.MOUNTED, sequence=2)
    run.travel(ctx, 2, airborne, east)
    assert run.object_target == (.49, .5) and rechecks == [1]


def test_ground_obstacle_tries_local_recovery_before_visual_detour(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    run.planner = SimpleNamespace(client=object())
    ctx.input.name = "helper"
    monkeypatch.setattr(quest, "local_vision_available", lambda: True)
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    nav = TravelSample(90895, 2248, 2248, (.50, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, precise_object=True)
    east = 1.5 * math.pi
    for seq, now in enumerate((0, 3.1, 3.8, 7, 7.2, 7.9, 11.1, 11.9, 12.4, 15.6), 1):
        heading = east if now < 12.4 else run.trip.turn_error(nav.position, run.trip.path[0], 0)
        run.travel(ctx, now, replace(nav, sequence=seq), heading)
    assert ctx.input.count("key_down", "space") == 2 and not run.travel_keys
    assert run.terrain is not None and run.trip.router.blocked
    assert any(name == "quest_terrain_probe" and data["contacts"] >= 3 for name, data in events)


def test_object_hover_miss_refines_approach_without_clicking(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = run.arrived = run.object_minimap_checked = True
    run.object_target = (.44, .5)
    run.object_points, run.object_probe, run.object_probe_sequence = ((.5, .6),), 1, 1
    ctx.cfg.channel.x, ctx.cfg.channel.y = 40, 980
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1920, 1080))
    monkeypatch.setattr(run, "object_minimap_target", lambda *args, **kwargs: None)
    nav = TravelSample(90885, 2248, 2248, (.4, .5), (.4376, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 2, precise_object=True)

    run.search_object(ctx, 10, nav, 0)

    assert not run.arrived and run.object_approach_yards == quest.OBJECT_REFINE_YARDS
    assert run.object_target == (.44, .5) and not ctx.input.count("mouse_down")


@pytest.mark.parametrize("quest_id,failed_capture", [(67890, False), (90885, False), (90885, True)])
def test_missing_object_detection_never_falls_back_to_mouse_grid(tmp_path, monkeypatch, quest_id, failed_capture):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = run.arrived = True
    ctx.cfg.channel.x, ctx.cfg.channel.y = 40, 980
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1920, 1080))

    def detect(*_):
        if failed_capture:
            raise quest.CaptureError("capture unavailable")
        return ()

    monkeypatch.setattr(quest, "pumpkin_probe_points", detect)
    nav = TravelSample(quest_id, 2451, 2451, (.44, .5), (.44, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, objective_pin=True)
    run.search_object(ctx, 10, nav, 0)
    assert run.OUTCOME == "stopped:quest_object_visual_target_required"
    assert not ctx.input.count("move") and not ctx.input.count("mouse_down")


def test_visual_candidate_requires_matching_hover_and_has_no_grid_tail(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = run.arrived = True
    ctx.cfg.channel.x, ctx.cfg.channel.y = 40, 980
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1920, 1080))
    candidates = ((.5, .55), (.52, .56))
    monkeypatch.setattr(quest, "pumpkin_probe_points", lambda *_: candidates)
    nav = TravelSample(90885, 2451, 2451, (.44, .5), (.44, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, objective_pin=True)
    run.search_object(ctx, 10, nav, 0)
    run.search_object(ctx, 11, replace(nav, sequence=2, objective_cursor=(.9, .9)), 0)
    assert run.object_points == candidates and ctx.input.count("move") == 2
    assert not ctx.input.count("mouse_down")
    run.search_object(ctx, 12, replace(nav, sequence=3, objective_cursor=candidates[1]), 0)
    assert ctx.input.count("mouse_down", "right") == 1


def test_failed_mob_search_remains_combat_search_without_mouse_fallback(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None)]
    run.hunt = quest.QuestHunt(frozenset())
    monkeypatch.setattr(run.hunt, "step", lambda *_: ("target_not_found", None))
    monkeypatch.setattr(quest, "decode_combat", lambda _: None)
    nav = TravelSample(12345, 2451, 2451, (.44, .5), (.44, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1)
    run.hunt_tick(ctx, 10, 0, False, nav)
    assert isinstance(run.hunt, quest.QuestHunt) and run.search_waypoint is not None
    assert not ctx.input.count("move")


def test_forever_blocked_outdoor_hunt_waypoint_tries_next_search_point(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    run.auto_route = True
    run.hunt = quest.QuestHunt(frozenset({6}))
    nav = TravelSample(7, 1429, 1429, (.5, .5), (.5, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED | TravelFlags.OUTDOORS, 1)
    assert run.search_quest_area(ctx, nav)
    waypoint = run.search_waypoint
    blocked = quest.QuestTrip(nav.world_size, waypoint, "ccw_west")
    run.trip = blocked
    monkeypatch.setattr(blocked, "step", lambda *_args, **_kwargs: TravelCommand(outcome="route_blocked"))
    monkeypatch.setattr(run, "start_detour", lambda *_: pytest.fail("optional search point needs no detour"))
    run.travel_to_destination(ctx, 10, nav, 0)
    assert blocked.router.blocked and run.search_waypoint != waypoint
    assert run.trip is None and not run.arrived and run.OUTCOME is None


@pytest.mark.parametrize("fighting, retry", [(False, True), (True, False)])
@pytest.mark.parametrize("failure", ["no_cast_or_damage_receipt", "approach_timeout"])
def test_forever_changes_search_point_after_no_opening_receipt(tmp_path, monkeypatch, fighting, retry, failure):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    run.auto_route = run.arrived = True
    run.hunt = quest.QuestHunt(frozenset({6}))
    sample = CombatSample(3, 1, 18, 70959, 1485, 1, 0, 6, False, True, False, 5, 8, 1)
    run.hunt.step(0, sample, 4, False)
    monkeypatch.setattr(run, "read_combat", lambda _: sample)
    monkeypatch.setattr(run.hunt, "step", lambda *_: (failure, None))
    nav = TravelSample(7, 1429, 1429, (.5, .5), (.5, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED | TravelFlags.OUTDOORS, 1)
    run.hunt_tick(ctx, 10, 4, fighting, nav)
    if retry:
        assert run.hunt.fight is None and run.search_waypoint and run.OUTCOME is None
    else:
        assert run.OUTCOME == f"stopped:quest_combat_{failure}"


def test_npc_search_releases_turn_between_pulses(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route, run.phase = True, QuestState.READY
    nav = TravelSample(12345, 2451, 2451, (.45, .5), (.44, .5), (1000, 1000), 0,
                       QuestState.READY, TravelFlags.KEYS_VERIFIED, 1)
    for now in (10, 10.1, 10.2, 10.3):
        run.search_quest_npc(ctx, now, nav, 0)
    assert ctx.input.count("key_down", "right") == 1
    assert ctx.input.count("key_up", "right") == 1 and not run.travel_keys
    run.search_quest_npc(ctx, 10.6, nav, 0)
    assert ctx.input.count("key_down", "right") == 2


@pytest.mark.parametrize("hint, source, target", [
    ((236693, (.4601, .4437)), "grail_cache", (.4601, .4437)),
    ((236693, (.43, .4437)), "quest_marker", (.46146, .44376)),
    (None, "quest_marker", (.46146, .44376)),
])
def test_missing_npc_reapproaches_live_marker_without_assuming_interaction(tmp_path, monkeypatch, hint, source, target):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route, run.phase = True, QuestState.READY
    nav = TravelSample(86769, 2432, 2432, (.46146, .44376), (.46235, .44249), (3200, 2133.4), 0,
                       QuestState.READY, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED,
                       interact_verified=True, npc_range_supported=True)
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    monkeypatch.setattr(quest, "cached_npc_location", lambda *_: hint)
    run.interact_quest_target(ctx, 100, nav, 0)
    probe = next(data for name, data in events if name == "quest_npc_marker_approach")
    assert probe["source"] == source and probe["destination"] == target
    assert not ctx.input.count("key_down", "f")
    range_receipt = TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    run.interact_quest_target(ctx, 101, replace(nav, position=target, sequence=2, task=nav.task | range_receipt), 0)
    assert run.npc_marker_approach is None and ctx.input.count("key_down", "f") == 1


def test_live_candidate_outside_range_keeps_the_same_map_location_hint(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route, run.phase = True, QuestState.READY
    hint = (236693, (.4601, .4437))
    monkeypatch.setattr(quest, "cached_npc_location", lambda *_: hint)
    marked = (.46146, .44376)
    task = TaskFlags.PREPARED | TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN
    nav = TravelSample(86769, 2432, 2432, marked, (.46141, .44374), (3200, 2133.4), 0,
                       QuestState.READY, TravelFlags.KEYS_VERIFIED, 1, task=task,
                       interact_verified=True, npc_range_supported=True)
    goals = []
    monkeypatch.setattr(run, "align_npc_approach", lambda _ctx, _now, n, _head: goals.append(n.destination))
    run.interact_quest_target(ctx, 100, nav, 0)
    assert goals == [hint[1]] and not ctx.input.count("key_down", "f")
    run.interact_quest_target(ctx, 101, replace(nav, task=task | TaskFlags.NPC_CONFIGURED), 0)
    assert goals == [hint[1], marked]


@pytest.mark.parametrize("needs_choice", [False, True])
def test_active_talk_uses_verified_npc_and_waits_for_quest_feedback(tmp_path, monkeypatch, needs_choice):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    task = TaskFlags.PREPARED | TaskFlags.NPC_CONFIGURED | TaskFlags.NPC_CANDIDATE
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, interact_verified=True,
                       task=task, npc_range_supported=True, objective_action=ObjectiveAction.NPC_TALK)
    current = [nav]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))
    tick(QuestState.ACTIVE)
    assert ctx.input.count("key_down", "k") == 1
    assert not ctx.input.count("key_down", "f")
    current[0] = replace(nav, task=task | TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN
                         | TaskFlags.NPC_WITHIN_5_YARDS)
    tick(QuestState.ACTIVE, elapsed=.4)
    assert ctx.input.count("key_down", "f") == 1
    tick(QuestState.ACTIVE, Dialogue.BLOCKED, elapsed=.4)
    assert run.OUTCOME is None and not ctx.input.count("key_down", "9")
    if needs_choice:
        tick(QuestState.ACTIVE, Dialogue.BLOCKED, elapsed=2.6)
        assert run.OUTCOME == "stopped:quest_objective_dialogue_choice_required"
        assert not ctx.input.count("key_down", "9")
    else:
        current[0] = replace(nav, phase=QuestState.READY, objective_action=ObjectiveAction.NONE)
        tick(QuestState.READY, Dialogue.REWARD, 12345, done=3, elapsed=2)
        assert ctx.input.count("key_down", "9") == 1
        assert run.OUTCOME is None


def test_legacy_route_turn_is_small_and_paced(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    turns = []
    monkeypatch.setattr(ctx.input, "rmb_turn", turns.append)
    run.turn(ctx, math.pi, 10)
    run.turn(ctx, -math.pi, 10.1)
    run.turn(ctx, math.pi, 10.3)
    assert len(turns) == 1 and abs(turns[0]) <= 40
    run.turn(ctx, -math.pi, 10.6)
    assert len(turns) == 2 and abs(turns[1]) <= 40


def test_quest_runs_to_corpse_confirms_popup_and_prepares_before_resuming(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None, frozenset({244669}))]
    ctx.heading = lambda: 1.5 * math.pi
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1600, 925))
    alive = RecoverySample(LifeState.ALIVE)
    release = RecoverySample(LifeState.DEAD, button=(.5, .4), ui_aspect=.5625, dialog=True)
    ghost = RecoverySample(LifeState.GHOST, corpse=(.43, .5), walk_verified=True, facing=1.5 * math.pi)
    current = [alive]
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED)
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(
        nav, sequence=ctx.frame_sequence(), recovery=current[0],
        flags=TravelFlags(0) if current[0].life == LifeState.GHOST else nav.flags))

    tick(QuestState.ACTIVE)
    current[0] = release
    tick(QuestState.ACTIVE)
    assert ctx.input.last("move")[1] == (800, 385)
    assert not ctx.input.count("mouse_down")
    current[0] = replace(release, hovered=True)
    tick(QuestState.ACTIVE, elapsed=.3)
    assert ctx.input.count("mouse_down", "left") == 1 and ctx.input.count("ui_click") == 1
    ctx.heading = lambda: None  # V11 ghost navigation has synchronized facing.
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    current[0] = replace(ghost, walk_verified=False, corpse=None)
    tick(QuestState.ACTIVE, elapsed=.3)
    assert not ctx.input.count("key_down", "w")
    assert events[-1] == ("quest_recovery_route_wait", {
        "map": 2248, "missing": ("corpse_position", "movement_bindings"),
    })
    current[0] = ghost
    tick(QuestState.ACTIVE, elapsed=.3)
    assert ctx.input.count("key_down", "w") == 1
    current[0] = replace(ghost, dialog=True)
    tick(QuestState.ACTIVE, elapsed=.3)
    assert ctx.input.count("key_up", "w") == 1 and ctx.input.count("mouse_down", "left") == 1
    current[0] = replace(ghost, dialog=True, button=(.5, .45), ui_aspect=.5625)
    tick(QuestState.ACTIVE)
    current[0] = replace(current[0], hovered=True)
    tick(QuestState.ACTIVE, elapsed=.3)
    assert ctx.input.count("mouse_down", "left") == 2 and ctx.input.count("ui_click") == 2

    prep = replace(alive, pet=PetState.ABSENT, pet_expected=True, follow=True, summon=True,
                   revive=True, buff_missing=True)
    current[0] = prep
    ctx.heading = lambda: 1.5 * math.pi
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE, elapsed=.7)
    assert ctx.input.count("key_down", "f10") == 1
    assert not ctx.input.count("key_down", "f11")
    tick(QuestState.ACTIVE, elapsed=7.1)
    assert ctx.input.count("key_down", "f11") == 1
    current[0] = replace(prep, pet=PetState.ALIVE)
    tick(QuestState.ACTIVE, elapsed=.4)
    assert ctx.input.count("key_down", "f9") == 1
    tick(QuestState.ACTIVE, elapsed=.7)
    assert ctx.input.count("key_down", "f12") == 1 and run.recovery is not None
    assert not ctx.input.count("key_down", "tab")
    for _ in range(2):
        tick(QuestState.ACTIVE, fighting=True, target=900, engaged=True, elapsed=.4)
    assert run.recovery is not None and ctx.input.count("key_down", "4") == 1
    current[0] = replace(current[0], buff_missing=False)
    tick(QuestState.ACTIVE, elapsed=.4)
    assert run.recovery is None and ctx.stats["quest_recoveries"] == 1
    tick(QuestState.ACTIVE)
    assert run.OUTCOME is None and run.arrived


def test_recovery_uses_the_reported_popup_key_once_before_the_hover_fallback():
    recovery = quest.QuestRecovery(0)
    keys = MockInput()
    events = []
    ctx = SimpleNamespace(input=keys, log_event=lambda kind, data: events.append((kind, data)))
    nav = TravelSample(47, 1429, 1429, None, None, None, None, QuestState.ACTIVE, TravelFlags(0), 0,
        recovery=RecoverySample(LifeState.GHOST, dialog=True, dialog_key="h",
                                button=(.5, .4), ui_aspect=.5625, hovered=True))

    def tick(at, sequence):
        return recovery.tick(ctx, at, replace(nav, sequence=sequence),
            None, lambda _: None, lambda: (0, 0, 1600, 925))

    assert tick(0, 1) is None
    assert keys.count("key_down", "h") == keys.count("key_up", "h") == 1
    assert keys.count("move") == keys.count("ui_click") == 0
    assert tick(.2, 2) is None
    assert tick(4.1, 3) is None
    assert keys.count("move") == 1 and keys.count("ui_click") == 0
    assert tick(4.3, 4) is None
    assert keys.count("key_down", "h") == keys.count("ui_click") == 1
    assert tick(8.5, 5) == "confirmation_unacknowledged"
    assert any(kind == "quest_recovery_dialog_key" and data["life"] == "GHOST" for kind, data in events)


@pytest.mark.parametrize("operation", ["hover", "click"])
def test_recovery_stops_when_helper_rejects_popup_input(monkeypatch, operation):
    from bot.control.input_helper import HelperActionRejected

    recovery = quest.QuestRecovery(0)
    keys = MockInput()
    events = []
    ctx = SimpleNamespace(input=keys, log_event=lambda kind, data: events.append((kind, data)))
    nav = TravelSample(47, 1429, 1429, None, None, None, None, QuestState.ACTIVE, TravelFlags(0), 1,
        recovery=RecoverySample(LifeState.GHOST, dialog=True, button=(.5, .4), ui_aspect=.5625))

    def reject(*args, **kwargs):
        raise HelperActionRejected("鼠标目标在魔兽窗口之外")

    def tick(at, sample):
        return recovery.tick(ctx, at, sample, None, lambda _: None, lambda: (0, 0, 1600, 925))

    if operation == "hover":
        monkeypatch.setattr(keys, "move_to", reject)
    else:
        assert tick(0, nav) is None
        monkeypatch.setattr(keys, "click_ui", reject)
        nav = replace(nav, sequence=2, recovery=replace(nav.recovery, hovered=True))

    assert tick(.3, nav) == "dialog_input_rejected"
    assert recovery.hover is None and recovery.clicks == 0
    assert keys.count("mouse_down") == 0
    assert events[-1] == ("quest_recovery_dialog_input_rejected", {"life": "GHOST", "operation": operation,
        "point": (800, 385), "reason": "鼠标目标在魔兽窗口之外"})


@pytest.mark.parametrize("consistent", [True, False])
def test_ghost_uses_native_range_when_corpse_coordinates_are_missing(tmp_path, monkeypatch, consistent):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    east = 1.5 * math.pi
    corpse = (.43, .52)
    ghost = RecoverySample(LifeState.GHOST, walk_verified=True, facing=east)
    nav = TravelSample(12345, 2248, 2248, None, (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags(0), 1, task=TaskFlags.PREPARED)
    current = [nav]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))

    def sample(position):
        distance = math.dist(position, corpse) * 1000 if consistent else 50
        current[0] = replace(nav, position=position, recovery=replace(ghost, corpse_distance=distance))
        tick(QuestState.ACTIVE, elapsed=.3)

    sample((.4, .5))
    assert ctx.input.count("key_down", "w") == 1
    assert run.recovery.route_source == "distance_probe"
    sample((.409, .5))
    sample((.4, .509))
    sample((.4, .509))
    if consistent:
        assert run.recovery.route_source == "distance_estimate"
        assert run.recovery.trip.destination == pytest.approx(corpse)
    else:
        assert run.OUTCOME == "stopped:quest_recovery_corpse_distance_search_unconfirmed"
        assert ctx.input.count("key_up", "w") == 1


def test_corpse_refinement_uses_new_distance_and_waits_for_delayed_popup(tmp_path, monkeypatch):
    from bot.modules.recovery import QuestRecovery

    _, ctx, _ = make_run(tmp_path, monkeypatch)
    recover = QuestRecovery(0)
    east = 1.5 * math.pi
    ghost = RecoverySample(LifeState.GHOST, corpse=(.403, .5), walk_verified=True, facing=east,
                           corpse_source=CorpseSource.CURRENT_MAP, corpse_distance=11)
    nav = TravelSample(12345, 2248, 2248, None, (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags(0), 0, recovery=ghost)
    held = []

    def step(now, sequence, pos, recovery):
        return recover.tick(ctx, now, replace(nav, position=pos, sequence=sequence, recovery=recovery),
                            east, held.append, lambda: (0, 0, 1600, 925))

    assert step(0, 1, (.4, .5), ghost) is None
    step(.4, 2, (.402, .5), ghost)
    step(1.7, 3, (.402, .5), ghost)
    assert recover.near_steps == 1
    step(2.4, 4, (.404, .5), replace(ghost, corpse_distance=9))
    assert recover.near_steps == 2
    step(3.1, 5, (.405, .5), replace(ghost, corpse_distance=9))
    step(4, 6, (.405, .5), replace(ghost, corpse_distance=9))
    assert recover.near_steps == 2 and recover.near_failed
    assert step(5, 7, (.405, .5), replace(ghost, dialog=True)) is None
    assert not held[-1] and not ctx.input.count("ui_click")


@pytest.mark.parametrize("initial_pet", [PetState.DEAD, PetState.ABSENT])
def test_live_player_restores_pet_then_continues_same_objective(tmp_path, monkeypatch, initial_pet):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None)]
    ctx.heading = lambda: 1.5 * math.pi
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    alive = RecoverySample(LifeState.ALIVE, pet=PetState.ALIVE, pet_expected=True, summon=True, revive=True, follow=True)
    nav = TravelSample(12345, 2248, 2248, (.46, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED,
                       objective_action=ObjectiveAction.INTERACT, recovery=alive)
    current = [nav]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))

    tick(QuestState.ACTIVE)
    assert run.travel_keys == {"w"}
    hidden_on_mount = replace(nav, flags=nav.flags | TravelFlags.MOUNTED,
                              recovery=replace(alive, pet=PetState.ABSENT))
    assert not run.maintain_pet(ctx, 104, hidden_on_mount, run.sample, False)
    assert not ctx.input.count("key_down", "8") and run.pet_preparation is None
    current[0] = replace(nav, recovery=replace(alive, pet=initial_pet))
    tick(QuestState.ACTIVE, fighting=True)
    assert not ctx.input.count("key_down", "f11") and not run.travel_keys
    tick(QuestState.ACTIVE)
    assert run.pet_preparation is not None and run.recovery is None
    tick(QuestState.ACTIVE, elapsed=.7)
    if initial_pet == PetState.ABSENT:
        assert ctx.input.count("key_down", "f10") == 1 and not ctx.input.count("key_down", "f11")
        tick(QuestState.ACTIVE, elapsed=2.6)
    assert ctx.input.count("key_down", "f11") == 1
    current[0] = nav
    tick(QuestState.ACTIVE, elapsed=.4)
    assert ctx.input.count("key_down", "f9") == 1 and not run.travel_keys
    tick(QuestState.ACTIVE, elapsed=.7)
    assert run.pet_preparation is None and run.phase == QuestState.ACTIVE
    tick(QuestState.ACTIVE)
    assert run.OUTCOME is None and run.trip.destination == nav.destination and run.travel_keys == {"w"}
    assert any(name == "quest_pet_ready" for name, _ in events)


def test_ghost_approaches_a_confirmed_minimap_tombstone(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1600, 925))
    from bot.modules import recovery
    readings = iter(((0., -.40), (0., -.40), None))
    monkeypatch.setattr(recovery, "minimap_corpse_pin", lambda _: next(readings))
    # A rotating map with the player facing east puts the tombstone at the top.
    ghost = RecoverySample(LifeState.GHOST, walk_verified=True, facing=1.5 * math.pi)
    nav = TravelSample(12345, 2248, 2248, None, (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags(0), 1, task=TaskFlags.PREPARED,
                       minimap=MinimapGeometry((.91, .11), (.1, .1 * 1600 / 900), 100, True))
    current = [nav]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(
        current[0], sequence=ctx.frame_sequence(), recovery=ghost))
    tick(QuestState.ACTIVE)
    assert not ctx.input.count("key_down", "w")
    tick(QuestState.ACTIVE, elapsed=.4)
    assert ctx.input.count("key_down", "w") == 1
    assert run.recovery.route_source == "minimap_pin"
    assert run.recovery.trip.destination == pytest.approx((.44, .5))
    current[0] = replace(nav, position=(.412, .5))
    tick(QuestState.ACTIVE, elapsed=.4)
    assert ctx.input.count("key_up", "w") == 1


def test_ghost_follows_edge_bearing_in_short_steps_then_switches_to_pin(tmp_path, monkeypatch):
    from bot.modules import recovery
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1600, 925))
    pin, arrow = [None], [(0., -1.)]
    monkeypatch.setattr(recovery, "minimap_corpse_pin", lambda _: pin[0])
    monkeypatch.setattr(recovery, "minimap_corpse_bearing", lambda _: arrow[0])
    ghost = RecoverySample(LifeState.GHOST, walk_verified=True, facing=0.)
    nav = TravelSample(12345, 2248, 2248, None, (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags(0), 1, task=TaskFlags.PREPARED,
                       minimap=MinimapGeometry((.91, .11), (.1, .1*1600/900), 233, False))
    current = [nav]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(
        current[0], sequence=ctx.frame_sequence(), recovery=ghost))
    tick(QuestState.ACTIVE)
    assert not ctx.input.count("key_down", "w")
    tick(QuestState.ACTIVE, elapsed=.4)
    assert run.recovery.route_source == "minimap_arrow"
    assert run.recovery.trip.destination == pytest.approx((.4, .488))
    assert ctx.input.count("key_down", "w") == 1
    current[0] = replace(nav, position=(.4, .491))
    tick(QuestState.ACTIVE, elapsed=.4)
    assert run.recovery.trip.destination == pytest.approx((.4, .479))
    # A real pin has distance information; do not keep following the edge waypoint.
    pin[0], arrow[0] = (0., -.1), None
    tick(QuestState.ACTIVE, elapsed=.4)
    assert ctx.input.count("key_up", "w") == 1
    tick(QuestState.ACTIVE, elapsed=.4)
    assert run.recovery.route_source == "minimap_pin"
    assert run.recovery.trip.destination == pytest.approx((.4, .4677))


def test_ghost_edge_bearing_requires_agreement_and_stops_when_lost(tmp_path, monkeypatch):
    from bot.modules import recovery
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1600, 925))
    monkeypatch.setattr(recovery, "minimap_corpse_pin", lambda _: None)
    readings = iter(((0., -1.), (0., 1.), (0., -1.), (0., -1.), None, None))
    monkeypatch.setattr(recovery, "minimap_corpse_bearing", lambda _: next(readings))
    nav = TravelSample(12345, 2248, 2248, None, (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags(0), 1, task=TaskFlags.PREPARED,
                       minimap=MinimapGeometry((.91, .11), (.1, .1*1600/900), 233, False))
    ghost = RecoverySample(LifeState.GHOST, walk_verified=True, facing=0.)
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(
        nav, sequence=ctx.frame_sequence(), recovery=ghost))
    for _ in range(3):
        tick(QuestState.ACTIVE, elapsed=.4)
        assert not ctx.input.count("key_down", "w")
    tick(QuestState.ACTIVE, elapsed=.4)
    assert ctx.input.count("key_down", "w") == 1
    tick(QuestState.ACTIVE, elapsed=.4)
    tick(QuestState.ACTIVE, elapsed=1.2)
    assert ctx.input.count("key_up", "w") == 1


def test_apr_search_point_can_pick_up_confirmed_object_without_minimap_dot(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.auto_route = run.arrived = True
    ctx.cfg.channel.x, ctx.cfg.channel.y = 40, 980
    probe = (.54, .55)
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1920, 1080))
    monkeypatch.setattr(quest, "pumpkin_probe_points", lambda *_: (probe,))
    nav = TravelSample(90885, 2451, 2451, (.44, .5), (.437, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, precise_object=True, apr_waypoint=True)

    run.search_object(ctx, 10, nav, 0)
    assert run.arrived and ctx.input.count("move") == 1 and not ctx.input.count("mouse_down")
    run.search_object(ctx, 11, replace(nav, sequence=2, objective_cursor=probe), 0)
    assert ctx.input.count("mouse_down", "right") == 1
    run.search_object(ctx, 16.1, replace(nav, sequence=3, objective_cursor=probe), 0)
    assert ctx.input.count("mouse_down", "right") == 2 and ctx.input.count("move") == 1
    run.search_object(ctx, 21.2, replace(nav, sequence=4, objective_cursor=probe), 0)
    assert run.OUTCOME == "stopped:quest_object_interaction_unconfirmed"
    assert ctx.input.count("move") == 1 and run.search_waypoint is None


@pytest.mark.parametrize("immediate", [False, True])
def test_round_collects_all_offers_before_objectives_and_turn_in(tmp_path, monkeypatch, immediate):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    reading = [12345, QuestState.PENDING, RoundStage.PICKUP, False, False]

    def navigation(_):
        quest_id, state, stage, advance, fast_turn_in = reading
        return TravelSample(quest_id, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                            state, TravelFlags.KEYS_VERIFIED, ctx.frame_sequence(), interact_verified=True,
                            task=TaskFlags.PREPARED, npc_range_supported=True, objective_action=ObjectiveAction.ARRIVE,
                            round_stage=stage, round_advance=advance, round_immediate=fast_turn_in)

    def update(quest_id, state, stage, *, advance=False, fast=False, dialogue=Dialogue.CLOSED, dialogue_id=0):
        reading[:] = [quest_id, state, stage, advance, fast]
        tick(state, dialogue, dialogue_id, quest_id=quest_id)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    pickup, objectives, turn_in, finished = RoundStage.PICKUP, RoundStage.OBJECTIVES, RoundStage.TURN_IN, RoundStage.DONE
    update(12345, QuestState.PENDING, pickup, dialogue=Dialogue.DETAIL, dialogue_id=12345)
    update(12345, QuestState.READY, pickup, fast=immediate, dialogue=Dialogue.REWARD, dialogue_id=12345)
    assert ctx.input.count("key_down", "9") == (2 if immediate else 1)
    if immediate:
        update(12345, QuestState.TURNED_IN, pickup, advance=True, fast=True)
    else:
        update(12345, QuestState.READY, pickup, advance=True)
    update(67890, QuestState.PENDING, pickup, dialogue=Dialogue.DETAIL, dialogue_id=67890)
    update(67890, QuestState.ACTIVE, pickup, advance=True)
    assert ctx.stats["quests_accepted"] == 2 and ctx.stats.get("quests_done", 0) == int(immediate)
    assert run.trip is None
    update(67890, QuestState.ACTIVE, objectives)
    assert run.trip is not None
    update(67890, QuestState.READY, objectives, advance=True)
    if not immediate:
        update(12345, QuestState.READY, turn_in, dialogue=Dialogue.REWARD, dialogue_id=12345)
        update(12345, QuestState.TURNED_IN, turn_in, advance=True, dialogue=Dialogue.DETAIL, dialogue_id=91234)
    update(67890, QuestState.READY, turn_in, dialogue=Dialogue.REWARD, dialogue_id=67890)
    update(67890, QuestState.TURNED_IN, turn_in, advance=True)
    update(67890, QuestState.TURNED_IN, finished)
    assert run.OUTCOME == "completed"
    assert ctx.stats == {"quests_accepted": 2, "quests_done": 2, "quest_rounds_done": 1, "quests_pending": 0}


@pytest.mark.parametrize("moved, range_known", [(True, True), (True, False), (False, True)])
def test_auto_turn_in_requires_five_yard_confirmation(tmp_path, monkeypatch, moved, range_known):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    position = [(.4, .5)]
    interaction = [TaskFlags.PREPARED | TaskFlags.NPC_CANDIDATE | TaskFlags.NPC_SELECTED]
    if range_known:
        interaction[0] |= TaskFlags.NPC_RANGE_KNOWN

    def navigation(_):
        return TravelSample(12345, 2248, 2248, (.4, .492), position[0], (1000, 1000),
                            0, QuestState.READY, TravelFlags.KEYS_VERIFIED,
                            ctx.frame_sequence(), interact_verified=True, task=interaction[0],
                            npc_range_supported=True)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.READY)
    if not range_known:
        assert ctx.input.count("key_down", "w") == 0
        tick(QuestState.READY, elapsed=1.05)
    assert ctx.input.count("key_down", "w") == 1
    assert ctx.input.count("key_down", "f") == 0
    tick(QuestState.READY, elapsed=.26)
    assert ctx.input.count("key_up", "w") == 1
    if moved:
        position[0] = (.4, .498)  # Native range, not marker distance, permits interaction.
        interaction[0] |= TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    tick(QuestState.READY, elapsed=.3)
    if not moved:
        assert run.OUTCOME == "stopped:quest_npc_visual_unavailable"
        assert ctx.input.count("key_down", "f") == 0
        return
    assert ctx.input.count("key_down", "f") == 1
    # The client can refuse interaction; retry F while stationary, never walk past the NPC.
    tick(QuestState.READY, elapsed=1.3)
    assert ctx.input.count("key_down", "w") == 1
    assert ctx.input.count("key_down", "f") == 2
    tick(QuestState.READY, elapsed=.56)
    assert ctx.input.count("key_down", "f") == 2
    tick(QuestState.READY, Dialogue.REWARD, 12345)
    assert ctx.input.count("key_down", "9") == 1
    tick(QuestState.TURNED_IN)
    assert run.OUTCOME == "completed"


def test_quest_refuses_other_dialogue_and_filtered_category(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    tick(QuestState.PENDING, Dialogue.DETAIL, 999)
    assert run.OUTCOME == "stopped:quest_dialogue_unexpected"
    assert not ctx.input.actions
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    tick(QuestState.PENDING, kind=QuestKind.SIDE)
    assert run.OUTCOME == "stopped:quest_kind_side"
    assert not ctx.input.actions


@pytest.mark.parametrize("accepted", [True, False])
def test_prepared_accept_waits_for_quest_log_after_detail_closes(tmp_path, monkeypatch, accepted):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = run.require_prepared = True
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.PENDING, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED)
    current = [nav]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert ctx.input.count("key_down", "9") == 1

    tick(QuestState.PENDING, Dialogue.BLOCKED, elapsed=.25)
    assert run.OUTCOME is None
    assert ctx.input.count("key_down", "9") == 1
    if accepted:
        current[0] = replace(nav, phase=QuestState.ACTIVE)
        tick(QuestState.ACTIVE, elapsed=.25)
        assert ctx.stats["quests_accepted"] == 1
        assert run.OUTCOME is None
    else:
        tick(QuestState.PENDING, Dialogue.BLOCKED, elapsed=2.3)
        assert run.OUTCOME == "stopped:quest_dialogue_unexpected"
        assert ctx.input.count("key_down", "9") == 1


def test_quest_waits_for_local_decision_before_pressing_a_key(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    decisions = []

    def poll(signature, state, criteria):
        decisions.append((signature, state, criteria))
        if len(decisions) == 1:
            return None
        return {"recommendation": "pick_up", "model": "StartLux-Decision-test"}

    run.planner = SimpleNamespace(poll=poll)
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert not ctx.input.actions
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert ctx.input.count("key_down", "9") == 1
    assert decisions[0][1]["quest_id"] == 12345
    assert set(decisions[0][2]) == {"pick_up", "wait"}
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert len(decisions) == 2


def test_quest_abstention_and_server_failure_stop_without_input(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.planner = SimpleNamespace(poll=lambda *_: {"recommendation": None, "reason": "low_confidence"})
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert run.OUTCOME == "stopped:quest_startlux_low_confidence" and not ctx.input.actions

    def unavailable(*_):
        raise OSError("local service unavailable")

    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.planner = SimpleNamespace(poll=unavailable)
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert run.OUTCOME == "stopped:quest_startlux_unavailable" and not ctx.input.actions


def test_accept_only_stops_on_confirmed_quest_log_state(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.accept_only = True
    tick(QuestState.PENDING, Dialogue.DETAIL, 12345)
    assert run.OUTCOME is None
    tick(QuestState.ACTIVE)
    assert run.OUTCOME == "completed" and ctx.stats["quests_accepted"] == 1


def test_combat_quest_requires_allowlisted_target_and_credit_before_turn_in(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE, target=900)
    assert ctx.input.count("key_down", "tab") == 1
    assert ctx.input.count("key_down", "4") == 0
    tick(QuestState.ACTIVE, target=244669, epoch=2)
    tick(QuestState.ACTIVE, target=244669, epoch=2, fighting=True)
    assert ctx.input.count("key_down", "4") == 1
    tick(QuestState.ACTIVE, target=244669, dead=True, epoch=2, fighting=True)
    tick(QuestState.ACTIVE, target=244669, dead=True, epoch=2, done=1)
    assert run.hunt.fight is None and run.OUTCOME is None
    tick(QuestState.READY, fighting=True)
    assert ctx.input.count("key_down", "f") == 0
    tick(QuestState.READY)
    tick(QuestState.READY, Dialogue.REWARD, 12345)
    assert ctx.input.count("key_down", "9") == 1
    tick(QuestState.TURNED_IN)
    assert run.OUTCOME == "completed" and ctx.stats["quests_done"] == 1


def test_combat_target_gap_before_leaving_combat_preserves_corpse_loot(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None, frozenset({244669}))]

    def navigation(_):
        return TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                            QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, ctx.frame_sequence(),
                            task=TaskFlags.PREPARED, objective_action=ObjectiveAction.COMBAT,
                            fetch_ready=True, corpse_lootable=True)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    for _ in range(3):
        tick(QuestState.ACTIVE, target=244669)
    assert ctx.input.count("key_down", "4") == 1
    tick(QuestState.ACTIVE, target=0, fighting=True, elapsed=.2)
    assert run.hunt.fight.target_id == 244669 and run.defense is None
    assert not ctx.input.count("key_down", "tab")
    tick(QuestState.ACTIVE, target=244669, dead=True, elapsed=.9)
    assert ctx.input.count("key_down", "l") == 1 and run.OUTCOME is None


@pytest.mark.parametrize("hover_verified", [True, False])
def test_same_npc_attacker_handoff_before_loot(tmp_path, monkeypatch, hover_verified):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None, frozenset({244669}))]
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (0, 0, 1000, 700))
    minimap = MinimapGeometry((.9, .1), (.1, 1/7), 100, False)
    recovery = RecoverySample(LifeState.ALIVE, attacker_scan_supported=True)
    base = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED,
                        recovery=recovery, fetch_ready=True, sweep_ready=True, loot_epoch=0, minimap=minimap)
    current = [base]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))
    for _ in range(3):
        tick(QuestState.ACTIVE, target=244669, epoch=1)
    assert ctx.input.count("key_down", "4") == 1
    current[0] = replace(base, recovery=replace(recovery, attacker_count=1, attacker_cursor=(.5, .4)))
    tick(QuestState.ACTIVE, target=244669, dead=True, epoch=1, fighting=True, elapsed=.2, done=1)
    assert run.defense and ctx.input.count("move", 500, 280) == 1
    assert not ctx.input.count("ui_click") and not ctx.input.count("key_down", "l")
    current[0] = replace(current[0], recovery=replace(current[0].recovery, attacker_hovered=hover_verified))
    tick(QuestState.ACTIVE, target=244669, dead=True, epoch=1, fighting=True, elapsed=.25, done=1)
    assert ctx.input.count("ui_click") == int(hover_verified)
    if not hover_verified:
        tick(QuestState.ACTIVE, target=244669, dead=True, epoch=1, fighting=True, elapsed=.5, done=1)
        assert not ctx.input.count("ui_click") and ctx.input.count("key_down", "tab") == 1
        return
    for _ in range(2):
        tick(QuestState.ACTIVE, target=244669, epoch=2, engaged=True, fighting=True, elapsed=.5, done=1)
    assert ctx.input.count("key_down", "4") == 2 and run.defense.fight.state_epoch == 2
    assert not ctx.input.count("key_down", "o")
    current[0] = base
    tick(QuestState.ACTIVE, done=2, target=244669, dead=True, epoch=2)
    assert run.defense is None and ctx.input.count("key_down", "o") == 1


def test_nameplate_image_ignores_action_buttons_and_clipped_hud():
    frame = np.zeros((700, 1000, 3), dtype=np.uint8)
    frame[220:231, 420:530] = (220, 15, 20)
    frame[465:475, 490:575] = (220, 15, 20)
    frame[225:260, 630:665] = (220, 15, 20)
    frame[305:316, 135:245] = (220, 15, 20)
    assert hostile_nameplate_points(frame) == ((475, 225),)


def test_handoff_image_requires_a_stable_bar_and_fresh_hover(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.input.name = "helper"
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (10, 20, 1000, 700))
    frame = np.zeros((700, 1000, 3), dtype=np.uint8)
    frame[220:231, 420:530] = (220, 15, 20)
    ctx.capture.grab = lambda *_: frame
    sample = RecoverySample(LifeState.ALIVE, attacker_scan_supported=True)
    nav = TravelSample(12345, 2248, 2248, None, None, None, None, QuestState.ACTIVE,
                       TravelFlags.IN_COMBAT, 1, recovery=sample)
    defense = quest.QuestDefense(0)
    assert run.handoff_attacker(ctx, 0, nav, defense)
    assert not ctx.input.count("move")
    assert run.handoff_attacker(ctx, .2, replace(nav, sequence=2), defense)
    assert ctx.input.count("move", 485, 245) == 1
    hover = replace(nav, sequence=2, recovery=replace(sample, attacker_hovered=True))
    assert run.handoff_attacker(ctx, .5, hover, defense)
    assert not ctx.input.count("ui_click")
    assert run.handoff_attacker(ctx, .6, replace(hover, sequence=3), defense)
    assert ctx.input.count("ui_click") == 1


def test_resource_hunt_uses_area_and_loot_feedback_until_ready(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None, frozenset())]
    current = [TravelSample(12345, 2248, 2248, (.53, .5), (.4, .5), (1000, 1000), 0,
                            QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1,
                            task=TaskFlags.PREPARED, objective_action=ObjectiveAction.COMBAT,
                            search_area=((.41, .5), (.44, .5)))]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))
    events = []
    ctx.log_event = lambda event, payload: events.append((event, payload))

    def resource_tick(done=680, **kwargs):
        tick(QuestState.ACTIVE, done=done, required=1000, objective=Objective.RESOURCE, **kwargs)

    resource_tick(target=900)
    assert run.arrived and run.trip is None  # Current position is inside the native footprint.
    resource_tick(target=900)
    assert ctx.input.count("key_down", "4") == 0  # Unverified creature.
    current[0] = replace(current[0], objective_target=244669)
    resource_tick(target=244669, epoch=2)
    # Tooltip can disappear during combat. The same locked creature stays valid.
    current[0] = replace(current[0], objective_target=0)
    resource_tick(target=244669, epoch=2, fighting=True)
    assert ctx.input.count("key_down", "4") == 1
    current[0] = replace(current[0], task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE, corpse_lootable=True)
    resource_tick(target=244669, dead=True, epoch=2)
    assert ctx.input.count("key_down", "f") == 1 and run.progress == (680, 1000)
    resource_tick(740, target=244669, dead=True, epoch=2)
    assert run.progress == (740, 1000) and run.hunt.fight is None
    assert ("quest_resource_progress", {"id": 12345, "percent": 74.0}) in events

    resource_tick(1000, target=244669, epoch=3)
    assert run.phase == QuestState.ACTIVE and ctx.input.count("key_down", "4") == 1
    assert ctx.input.count("key_down", "9") == 0
    current[0] = replace(current[0], phase=QuestState.READY)
    tick(QuestState.READY, Dialogue.REWARD, 12345, done=1000, required=1000, objective=Objective.RESOURCE)
    assert run.phase == QuestState.READY and ctx.input.count("key_down", "9") == 1


def test_resource_hunt_continues_after_no_drop_but_has_a_limit(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    events = []
    ctx.log_event = lambda event, payload: events.append((event, payload))

    def resource_tick(done, **kwargs):
        tick(QuestState.ACTIVE, done=done, required=1000, objective=Objective.RESOURCE, **kwargs)

    def defeat(epoch, done):
        resource_tick(done, target=244669, epoch=epoch)
        resource_tick(done, target=244669, epoch=epoch)
        resource_tick(done, target=244669, epoch=epoch, dead=True)

    resource_tick(680)
    defeat(2, 680)
    resource_tick(680, elapsed=5.1)
    assert run.hunt.fight is None and run.hunt.uncredited_kills == 1
    defeat(3, 680)
    resource_tick(710, elapsed=5.1)
    assert run.hunt.fight is None and run.hunt.uncredited_kills == 0
    for epoch in range(4, 8):
        defeat(epoch, 710)
        resource_tick(710, elapsed=5.1)
    assert run.OUTCOME == "stopped:quest_combat_resource_no_progress"
    assert [p["uncredited_kills"] for event, p in events if event == "quest_resource_try_next"] == [1, 1, 2, 3]


def test_counted_drop_quest_uses_loot_but_waits_for_native_item_count(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None, frozenset())]
    base = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                        QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED,
                        objective_action=ObjectiveAction.COMBAT, objective_target=475, counted_drop=True,
                        loot_epoch=0)
    nav = [base]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav[0], sequence=ctx.frame_sequence()))
    events = []
    ctx.log_event = lambda name, payload: events.append((name, payload))
    for _ in range(4):
        tick(QuestState.ACTIVE, target=475, epoch=2, done=2, required=10)
    assert run.hunt.counted_drop and ctx.input.count("key_down", "4") >= 1

    nav[0] = replace(base, task=base.task | TaskFlags.LOOT_IN_RANGE, corpse_lootable=True)
    tick(QuestState.ACTIVE, target=475, dead=True, epoch=2, done=2, required=10)
    assert ctx.input.count("key_down", "f") == 1
    nav[0] = replace(nav[0], loot_epoch=1, corpse_lootable=False)
    tick(QuestState.ACTIVE, target=475, dead=True, epoch=2, done=2, required=10)
    tick(QuestState.ACTIVE, target=475, dead=True, epoch=2, done=2, required=10, elapsed=5.1)
    assert ("quest_drop_try_next", {"id": 12345, "count": 2, "uncredited_kills": 1}) in events
    assert not any(name == "quest_combat_credit" for name, _ in events)
    assert run.hunt.fight is None
    assert ctx.input.count("key_down", "9") == 0


def test_forever_inn_uses_mesh_for_entry_and_keeps_exit_until_handoff(tmp_path, monkeypatch):
    from bot.core.config import Nav3DConfig

    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    ctx.cfg.nav3d = Nav3DConfig(forever_goldshire_mesh="local-mesh", forever_goldshire_mesh_id="digest")
    entry = TravelSample(60, 1429, 1429, (.433, .658), (.422, .659), (3470.8, 2314.6), 0,
                         QuestState.PENDING, TravelFlags.OUTDOORS, 1)
    assert run.forever_building_mode(ctx, entry) == ("goldshire", "enter")
    assert run.forever_building_mode(ctx, replace(entry, quest_id=47)) is None
    inside = replace(entry, position=entry.destination, phase=QuestState.ACTIVE, destination=(.405, .803),
                     flags=TravelFlags(0))
    assert run.forever_building_mode(ctx, inside) == ("goldshire", "exit")
    run.forever_building_segment = (60, QuestState.ACTIVE, "goldshire", "exit")
    outside = replace(inside, position=entry.position, flags=TravelFlags.OUTDOORS)
    assert run.forever_building_mode(ctx, outside) == ("goldshire", "exit")
    run.forever_building_complete.add((60, QuestState.ACTIVE, "goldshire"))
    assert run.forever_building_mode(ctx, outside) is None


def test_forever_abbey_perimeter_keeps_nav3d_after_client_reports_outdoors(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    outside = TravelSample(15, 1429, 1429, (.512, .409), (.503538, .426141), (3470.8, 2314.6), 0,
                           QuestState.ACTIVE, TravelFlags.OUTDOORS, 1, forever_step=ForeverStep.TRAVEL)
    assert run.forever_building_mode(ctx, outside) == ("abbey", "exit")
    assert run.forever_building_mode(ctx, replace(outside, position=(.53, .41))) is None
    run.forever_building_complete.add((15, QuestState.ACTIVE, "abbey_trainer"))
    assert run.forever_building_mode(ctx, outside) is None


def test_forever_gear_lua():
    lua = pytest.importorskip("lupa.lua51").LuaRuntime()
    lua.execute('dofile("tests/lua/forever_gear.lua")')


def test_forever_equips_at_rest_and_resumes_the_quest(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    nav = TravelSample(60, 1429, 1429, (.42, .66), (.4, .67), (3470.8, 2314.6), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, addon_build=328,
                       forever_step=ForeverStep.EQUIP, forever_value=6)
    run.set_travel_keys(ctx, frozenset({"w"}))
    assert run.maintain_forever_character(ctx, 100, nav, 0, Dialogue.CLOSED)
    assert ctx.input.count("key_up", "w") == 1
    assert ctx.input.count("key_down", "f4") == 1
    wait = replace(nav, sequence=2, forever_step=ForeverStep.EQUIP_WAIT)
    assert run.maintain_forever_character(ctx, 102, wait, 0, Dialogue.CLOSED)
    done = replace(nav, sequence=3, forever_step=ForeverStep.EQUIPPED)
    assert run.maintain_forever_character(ctx, 104, done, 0, Dialogue.CLOSED)
    assert ctx.input.count("key_down", "f4") == 1
    assert not run.maintain_forever_character(ctx, 106, replace(nav, sequence=4, forever_step=ForeverStep.NONE),
                                              0, Dialogue.CLOSED)


def test_forever_repair_interacts_only_with_verified_npc_and_bounds_retries(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    flags = TravelFlags.KEYS_VERIFIED | TravelFlags.OUTDOORS
    task = TaskFlags.PREPARED | TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    nav = TravelSample(60, 1429, 1429, (.417, .6586), (.417, .6586), (3470.8, 2314.6), 0,
                       QuestState.READY, flags, 1, addon_build=328, task=task,
                       forever_step=ForeverStep.REPAIR_TRAVEL, forever_value=2046, interact_verified=True)
    assert run.maintain_forever_character(ctx, 100, replace(nav, interact_verified=False), 0, Dialogue.CLOSED)
    assert ctx.input.count("key_down", "f") == 0
    for i in range(4):
        assert run.maintain_forever_character(ctx, 102 + 2 * i, replace(nav, sequence=i + 2), 0, Dialogue.CLOSED)
    assert ctx.input.count("key_down", "f") == 3
    assert ctx.input.count("key_down", "f4") == 1
    merchant = replace(nav, sequence=6, forever_step=ForeverStep.REPAIR, forever_value=35)
    assert run.maintain_forever_character(ctx, 110, merchant, 0, Dialogue.CLOSED)
    assert ctx.input.count("key_down", "f4") == 2
    assert run.maintain_forever_character(ctx, 112, replace(merchant, sequence=7, forever_step=ForeverStep.REPAIRED),
                                          0, Dialogue.CLOSED)
    assert ctx.input.count("key_down", "f4") == 2


def test_forever_repair_leaves_an_indoor_objective_before_entering_the_smithy(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    ctx.cfg.nav3d.forever_fargodeep_mesh = "mine-mesh"
    ctx.cfg.nav3d.forever_fargodeep_mesh_id = "mine"
    ctx.cfg.nav3d.forever_goldshire_mesh_id = "town"
    ctx.cfg.nav3d.forever_goldshire_mesh = "town-mesh"
    repair = TravelSample(62, 1429, 1429, (.417, .6586), quest.FARGODEEP_SOUTH_ENTRY, (3470.8, 2314.6), 0,
                          QuestState.ACTIVE, TravelFlags(0), 1,
                          forever_step=ForeverStep.REPAIR_TRAVEL, forever_value=2046)
    assert run.forever_building_mode(ctx, repair) == ("fargodeep", "exit")
    near_smithy = replace(repair, position=quest.GOLDSHIRE_SMITHY_APPROACH, flags=TravelFlags.OUTDOORS)
    assert run.forever_building_mode(ctx, near_smithy) == ("goldshire", "enter")
    run.forever_building_segment = (62, QuestState.ACTIVE, "goldshire_repair_enter", "enter")
    assert run.forever_building_mode(ctx, replace(near_smithy, position=repair.destination)) == ("goldshire", "enter")


def test_forever_corpse_route_uses_verified_ghost_walking_and_current_map(tmp_path, monkeypatch):
    from bot.perception.nav3d_channel import Nav3DSample

    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    corpse = (.482893, .292278)
    ghost = RecoverySample(LifeState.GHOST, corpse=corpse, corpse_source=CorpseSource.CURRENT_MAP,
                           walk_verified=True, facing=0.)
    nav = TravelSample(21, 1429, 1429, (.4896, .2776), (.496298, .421785), (3470.8, 2314.6), 0,
                       QuestState.ACTIVE, TravelFlags.OUTDOORS, 1, recovery=ghost)
    run.nav3d_sample = Nav3DSample(1, 0, 70959, 1429, (187.2, -8915.8), 0., 0.,
                                   21, int(QuestState.ACTIVE), None, None, False, False, False, int(nav.flags))
    calls = []
    monkeypatch.setattr(run, "approach_forever_building",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    assert run.forever_corpse_route(ctx, 100, nav, corpse, 0.)
    args, kwargs = calls[0]
    assert args[2].destination == corpse and args[2].flags & TravelFlags.KEYS_VERIFIED
    pose = kwargs["corpse_pose"]
    assert pose.grounded and pose.bindings_verified and pose.goal_xy == pytest.approx((140.674, -8616.04), abs=.08)
    assert run.forever_corpse_route(ctx, 100.2, replace(nav, position=(.49627, .42179)), corpse, 0.)
    assert calls[-1][1]["corpse_pose"].goal_xy == pose.goal_xy
    assert not run.forever_corpse_route(ctx, 100, replace(nav, recovery=replace(ghost, walk_verified=False)), corpse, 0.)


def test_forever_echo_ridge_uses_cave_mesh_only_for_the_matching_objective(tmp_path, monkeypatch):
    from bot.core.config import Nav3DConfig

    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    ctx.cfg.nav3d = Nav3DConfig(forever_echo_ridge_mesh="local-mesh", forever_echo_ridge_mesh_id="digest")
    outside = TravelSample(21, 1429, 1429, (.489628, .277552), (.48, .36), (3470.8, 2314.6), 0,
                           QuestState.ACTIVE, TravelFlags.OUTDOORS, 1)
    assert run.forever_building_mode(ctx, outside) is None
    entrance = replace(outside, position=quest.ECHO_RIDGE_EXTERIOR)
    assert run.forever_building_mode(ctx, entrance) == ("echo_ridge", "enter")
    assert run.forever_building_mode(ctx, replace(entrance, quest_id=15)) is None
    assert run.forever_building_mode(ctx, replace(entrance, destination=(.513, .35))) is None

    inside = replace(outside, position=quest.ECHO_RIDGE_HUNT)
    run.forever_building_complete.add((21, QuestState.ACTIVE, "echo_ridge"))
    assert run.forever_building_mode(ctx, inside) is None
    ready = replace(inside, phase=QuestState.READY, destination=(.489, .416))
    assert run.forever_building_mode(ctx, ready) == ("echo_ridge", "exit")
    run.forever_building_segment = (21, QuestState.READY, "echo_ridge", "exit")
    assert run.forever_building_mode(ctx, replace(ready, position=quest.ECHO_RIDGE_EXTERIOR)) == ("echo_ridge", "exit")
    run.forever_building_complete.add((21, QuestState.READY, "echo_ridge"))
    assert run.forever_building_mode(ctx, ready) is None
    abbey = replace(ready, position=quest.NORTHSHIRE_ABBEY_ENTRY)
    assert run.forever_building_mode(ctx, abbey) == ("abbey", "enter")


@pytest.mark.parametrize("roof_only", [False, True])
def test_forever_handoff_to_mesh_requires_ground_floor_route(tmp_path, monkeypatch, roof_only):
    from bot.nav3d import NavigationError

    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    nav = TravelSample(7, 1429, 1429, (.489, .416), (.489342, .400187), (3470.8, 2314.6), 0,
                       QuestState.READY, TravelFlags.OUTDOORS | TravelFlags.KEYS_VERIFIED, 1)
    run.forever_building_segment = (7, QuestState.READY, "abbey", "enter")
    rejoin_steps = []
    run.forever_entry_trip = SimpleNamespace(
        step=lambda *_args, **_kwargs: rejoin_steps.append(True) or TravelCommand())
    run.nav3d_sample = SimpleNamespace(position_xy=(162.2, -8870.1), goal_xy=(161.0, -8906.7),
        goal_world_id=0, grounded=True, bindings_verified=True, sequence=1, world_id=0, ui_map_id=1429,
        quest_id=7, phase=3, client_build=70959)
    mesh_poses = []

    def plan(_):
        if roof_only:
            raise NavigationError("no_complete_path")

    run.forever_entry = SimpleNamespace(sequence=None,
        tracker=SimpleNamespace(edge_tolerance=.14,
            candidates=lambda *_args, **_kwargs: [(162.2, -8870.1, 80.2)],
            mesh=SimpleNamespace(frame=None, plan=plan)),
        step=lambda _now, sample, _nav, **_kwargs: mesh_poses.append(sample.position_xy) or TravelCommand())
    run.set_travel_keys(ctx, frozenset({"w"}))
    assert run.approach_forever_building(ctx, 10, nav, 0)
    if roof_only:
        assert rejoin_steps == [True] and not mesh_poses and run.forever_entry_trip is not None
    else:
        assert mesh_poses == [(162.2, -8870.1)] and not rejoin_steps and run.forever_entry_trip is None
    assert not run.travel_keys


def test_forever_abbey_pickup_rejoins_lower_ground_before_trying_the_indoor_pin(tmp_path, monkeypatch):
    from bot.nav3d import NavigationError

    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    goal_xy = (161.8, -8902.34)
    position_xy = (143., -8917.6)
    lower_xy = (143., -8919.1)
    nav = TravelSample(96627, 1429, 1429, (.489, .416), (.48357, .42257), (3470.8, 2314.6), 0,
                       QuestState.PENDING, TravelFlags.OUTDOORS | TravelFlags.KEYS_VERIFIED, 1)
    run.forever_building_segment = (96627, QuestState.PENDING, "abbey", "enter")
    run.nav3d_sample = SimpleNamespace(position_xy=position_xy, goal_xy=goal_xy, goal_world_id=0,
        grounded=True, bindings_verified=True, sequence=1, world_id=0, ui_map_id=1429,
        quest_id=96627, phase=int(QuestState.PENDING), client_build=70959)
    heights = {position_xy: 98.41, lower_xy: 81.03, goal_xy: 82.02}
    mesh_steps = []

    def plan(query):
        if query.start[2] > 90:
            raise NavigationError("no_complete_path")

    run.forever_entry = SimpleNamespace(sequence=None,
        tracker=SimpleNamespace(edge_tolerance=.14,
            candidates=lambda point, **_: [(*point, heights[point])],
            mesh=SimpleNamespace(frame=None, plan=plan)),
        step=lambda _now, sample, _nav, **_: mesh_steps.append(sample.position_xy) or TravelCommand())

    assert run.approach_forever_building(ctx, 10, nav, 0)
    lower_map = (nav.position[0], nav.position[1] + 1.5 / nav.world_size[1])
    assert run.forever_entry_trip.destination == pytest.approx(lower_map)
    assert not mesh_steps
    run.nav3d_sample.position_xy = lower_xy
    assert run.approach_forever_building(ctx, 11, replace(nav, position=lower_map), 0)
    assert run.forever_entry_trip is None and mesh_steps == [lower_xy]


def test_goldshire_verified_npc_range_can_retry_from_connected_ground(tmp_path, monkeypatch):
    from bot.nav3d import NavMesh
    from bot.nav3d import runtime as nav3d_runtime
    from bot.nav3d.follower import WorldRouteFollower

    run, ctx, _ = make_run(tmp_path, monkeypatch)
    ctx.cfg.game_version = "1.60.1"
    ctx.cfg.nav3d.forever_goldshire_mesh_id = "verified-mesh"
    nav = TravelSample(54, 1429, 1429, (.422, .659), (.42285, .65871), (3470.8, 2314.6), 0,
                       QuestState.READY, TravelFlags.OUTDOORS | TravelFlags.KEYS_VERIFIED, 23)
    start_xy = (-67.73, -9464.19)
    goal_xy = quest.GOLDSHIRE_DUGHAN_APPROACH_XY
    run.nav3d_sample = SimpleNamespace(position_xy=start_xy, grounded=True, bindings_verified=True,
        sequence=23, quest_id=54, ui_map_id=1429, world_id=0, client_build=70959)

    def plan(query):
        return SimpleNamespace(points=(query.start, query.goal), length=4.7)

    monkeypatch.setattr(NavMesh, "load", lambda _: SimpleNamespace(mesh_id="verified-mesh", frame=None, plan=plan))
    monkeypatch.setattr(nav3d_runtime, "SurfaceTracker", lambda _: SimpleNamespace(candidates=lambda xy: [(*xy, 56.)]))
    monkeypatch.setattr(WorldRouteFollower, "validate_route", lambda _: None)

    assert not run.start_goldshire_npc_reposition(ctx, 10, replace(nav, sequence=22))
    assert run.start_goldshire_npc_reposition(ctx, 10, nav)
    assert run.goldshire_npc_trip.destination[0] < nav.position[0]
    assert not run.start_goldshire_npc_reposition(ctx, 11, nav)


def test_forever_receipt_is_logged_even_when_not_driving_progression(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    ctx.cfg.game_version = "1.60.1"
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0, QuestState.ACTIVE,
                       TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED, addon_build=297,
                       forever_step=ForeverStep.SPELL_LEARNED, forever_value=1494)
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav, sequence=ctx.frame_sequence()))
    events = []
    ctx.log_event = lambda name, data: events.append((name, data))
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE)
    assert [data for name, data in events if name == "forever_progression_receipt"] == [
        {"step": "SPELL_LEARNED", "value": 1494, "id": 12345, "sequence": 1}]


def test_consumable_recovery_waits_for_item_receipt_and_rest_cancels_in_combat(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    nav = TravelSample(21, 1429, 1429, (.48, .3), (.48, .3), (1000, 1000), 0, QuestState.ACTIVE,
                       TravelFlags.KEYS_VERIFIED, 1, recovery=RecoverySample(LifeState.ALIVE),
                       vitals=PlayerVitals(54, 100, SupplyAction.FOOD, 117))
    released, events = [], []
    ctx.log_event = lambda kind, data: events.append((kind, data))
    tick = run.consumables.tick
    assert tick(ctx, 100, nav, in_combat=False, can_start=True, release=lambda: released.append(True))
    assert ctx.input.count("key_down", "f3") == 1 and released
    assert tick(ctx, 101, nav, in_combat=False, can_start=True, release=lambda: None)
    assert not any(kind == "quest_consumable_receipt" for kind, _ in events)
    eaten = replace(nav, vitals=replace(nav.vitals, result="consumed"))
    assert tick(ctx, 102, eaten, in_combat=False, can_start=True, release=lambda: None)
    assert any(kind == "quest_consumable_receipt" for kind, _ in events)
    assert not tick(ctx, 103, eaten, in_combat=True, can_start=True, release=lambda: None)
    blocked = replace(nav, vitals=PlayerVitals(None, 80, SupplyAction.POTION, 118))
    assert not tick(ctx, 109, blocked, in_combat=True, can_start=True, release=lambda: None)
    injured = replace(blocked, vitals=replace(blocked.vitals, health=30))
    assert tick(ctx, 110, injured, in_combat=True, can_start=True, release=lambda: None)
    assert ctx.input.count("key_down", "f1") == 1
    assert not tick(ctx, 111, injured, in_combat=True, can_start=True, release=lambda: None)
    assert ctx.input.count("key_down", "f1") == 1


@pytest.mark.parametrize("confirmed", [False, True])
def test_forever_waits_for_verified_reward_without_guessing_a_slot(tmp_path, monkeypatch, confirmed):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    ctx.cfg.game_version = "1.60.1"
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0, QuestState.READY,
                       TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED, addon_build=298)
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav, sequence=ctx.frame_sequence()))
    tick(QuestState.READY, Dialogue.REWARD_CHOICE, 12345)
    assert run.OUTCOME is None and ctx.input.count("key_down", "9") == 0
    if confirmed:
        tick(QuestState.READY, Dialogue.REWARD, 12345)
        assert run.OUTCOME is None and ctx.input.count("key_down", "9") == 1
    else:
        for _ in range(5):
            tick(QuestState.READY, Dialogue.REWARD_CHOICE, 12345)
        assert run.OUTCOME == "stopped:quest_reward_choice_required"
        assert ctx.input.count("key_down", "9") == 0


@pytest.mark.parametrize("corpse_returns", [True, False])
def test_lost_corpse_reselects_once_then_tries_fetch(tmp_path, monkeypatch, corpse_returns):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244676}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244676, state_epoch=2)
    navigation = SimpleNamespace(phase=QuestState.ACTIVE, task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE,
                                 flags=TravelFlags.KEYS_VERIFIED, last_target_verified=True,
                                 fetch_ready=True, corpse_lootable=True)
    missing = SimpleNamespace(target_dead=False, target_id=0, unsafe=False, key_verified=True)
    assert run.loot_corpse(ctx, 0, 0, missing, navigation)
    assert run.loot_corpse(ctx, .3, 0, missing, navigation)
    assert ctx.input.count("key_down", "p") == 1
    corpse = SimpleNamespace(target_dead=True, target_id=244676, state_epoch=4,
                             previous_target_epoch=None, unsafe=False)
    assert run.loot_corpse(ctx, .6, 0, corpse, navigation)
    assert not ctx.input.count("key_down", "l") and not ctx.input.count("key_down", "f")
    if corpse_returns:
        corpse.previous_target_epoch = 2
        assert run.loot_corpse(ctx, .9, 0, corpse, navigation)
    else:
        navigation.corpse_lootable = None
        assert run.loot_corpse(ctx, 1.6, 0, missing, navigation)
        assert run.loot_corpse(ctx, 2, 0, missing, navigation)
    assert ctx.input.count("key_down", "l") == 1


def test_prepared_collection_defends_after_dismount_and_resumes(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None)]
    ctx.cfg.channel.x, ctx.cfg.channel.y = -1822, 982
    monkeypatch.setattr(quest, "frontmost_game_bounds", lambda: (-1920, 0, 1920, 1080))
    flags = [TravelFlags.KEYS_VERIFIED | TravelFlags.MOUNTED]

    def navigation(_):
        return TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                            QuestState.ACTIVE, flags[0], ctx.frame_sequence(), task=TaskFlags.PREPARED,
                            objective_action=ObjectiveAction.INTERACT, objective_cursor=(.5, .6))

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.ACTIVE)
    assert ctx.input.count("key_down", "8") == 1
    tick(QuestState.ACTIVE, fighting=True, target=900, engaged=True)
    assert ctx.input.count("key_down", "8") == 2 and ctx.input.count("key_down", "4") == 0
    flags[0] = TravelFlags.KEYS_VERIFIED | TravelFlags.IN_COMBAT
    tick(QuestState.ACTIVE, target=900)
    assert ctx.input.count("key_down", "tab") == 1
    tick(QuestState.ACTIVE, fighting=True, target=901, engaged=True, verified=False)
    assert ctx.input.count("key_down", "4") == 0
    tick(QuestState.ACTIVE, fighting=True, target=901, engaged=True, facing=0)
    tick(QuestState.ACTIVE, fighting=True, target=901, engaged=True, facing=0)
    assert ctx.input.count("key_down", "4") == 1 and not ctx.input.count("move")
    tick(QuestState.ACTIVE, fighting=True, target=901, engaged=True, facing=0, facing_error=1, elapsed=.3)
    assert ctx.input.count("key_down", "left") == 1 and ctx.input.count("key_down", "4") == 1
    tick(QuestState.ACTIVE, fighting=True, target=901, engaged=True, facing=math.radians(24), facing_error=1)
    assert ctx.input.count("key_down", "4") == 2
    flags[0] = TravelFlags.KEYS_VERIFIED
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE)
    assert run.OUTCOME is None and run.defense is None
    assert ctx.input.count("move") == 1


def test_defense_waits_for_landing_then_dismounts_and_retreats_if_attacks_stall(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None)]
    flags = TravelFlags.KEYS_VERIFIED | TravelFlags.IN_COMBAT
    base = TravelSample(12345, 2248, 2248, (.4, .7), (.4, .5), (1000, 1000), 0,
                        QuestState.READY, flags, 1, task=TaskFlags.PREPARED,
                        sky_dismount_key="z", strafe_verified=True)
    mounted = flags | TravelFlags.SKYRIDING | TravelFlags.MOUNTED
    nav = [replace(base, flags=mounted | TravelFlags.FLYING)]
    run.safe_ground.append((.4, .555))
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(nav[0], sequence=ctx.frame_sequence()))

    def fight(elapsed=.25):
        tick(QuestState.READY, fighting=True, target=141659, engaged=True, target_health=100, elapsed=elapsed)

    fight()
    nav[0] = replace(base, flags=mounted | TravelFlags.FALLING)
    fight()
    assert not ctx.input.count("key_down", "z") and not ctx.input.count("key_down", "4")
    nav[0] = replace(base, flags=mounted)
    fight()
    assert ctx.input.count("key_down", "z") == 1 and not ctx.input.count("key_down", "4")
    nav[0] = base
    for _ in range(25):
        fight(1)
        if run.defense.retreat is not None:
            break
    assert run.defense.retreat is not None and run.OUTCOME is None
    assert ctx.input.count("key_down", "q") == 0 and ctx.input.count("key_down", "4") < 20
    ctx.heading = lambda: math.pi
    fight()
    assert ctx.input.count("key_down", "w") == 1
    run.defense.started_at -= 20
    nav[0] = replace(base, flags=flags | TravelFlags.FALLING)
    fight()
    assert run.OUTCOME is None and not run.travel_keys
    nav[0] = replace(base, flags=TravelFlags.KEYS_VERIFIED, position=(.4, .53))
    tick(QuestState.READY)
    assert run.defense is None and run.OUTCOME is None
    assert run.trip.destination == base.destination


def test_patrol_retreats_before_aggro_and_mends_pet_in_combat(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    run.steps = [quest.QuestStep(12345, "9", None, None, None, None, frozenset({244695}))]
    flags = TravelFlags.KEYS_VERIFIED
    healthy = RecoverySample(LifeState.ALIVE, pet=PetState.ALIVE, follow=True, mend_verified=True, pet_health=100)
    current = [TravelSample(12345, 2248, 2248, (.5, .5), (.4, .54), (1000, 1000), 0,
                            QuestState.ACTIVE, flags, 1, task=TaskFlags.PREPARED,
                            objective_action=ObjectiveAction.COMBAT, recovery=healthy)]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))
    tick(QuestState.ACTIVE)
    patrol = replace(healthy, patrol_id=244695, patrol_danger=int(TargetDanger.ELITE))
    current[0] = replace(current[0], position=(.4, .5), recovery=patrol)
    tick(QuestState.ACTIVE, target=244695, danger=TargetDanger.ELITE)
    assert run.search_waypoint or run.avoidance_trip
    assert ctx.input.count("key_down", "4") == 0

    hurt = replace(healthy, pet_health=None)
    current[0] = replace(current[0], flags=flags | TravelFlags.IN_COMBAT, recovery=hurt)
    tick(QuestState.ACTIVE, target=244695, engaged=True, fighting=True, danger=TargetDanger.ELITE)
    assert ctx.input.count("key_down", "f7") == 1
    current[0] = replace(current[0], recovery=replace(hurt, mend_active=True, mend_epoch=1))
    tick(QuestState.ACTIVE, target=244695, engaged=True, fighting=True, danger=TargetDanger.ELITE)
    assert run.defense.retreat.destination == (.4, .54)
    assert ctx.input.count("key_down", "f9") == 1
    assert ctx.input.count("key_down", "4") == 0 and ctx.input.count("key_down", "f7") == 1


def test_combat_quest_stops_without_credit_or_verified_binding(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE, target=244669, verified=False)
    assert run.OUTCOME == "stopped:quest_combat_unverified_action_binding"
    assert not ctx.input.actions

    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    for _ in range(3):
        tick(QuestState.ACTIVE, target=244669)
    for _ in range(4):
        tick(QuestState.ACTIVE, target=244669, dead=True)
    assert run.OUTCOME == "stopped:quest_combat_no_quest_credit"
    assert ctx.input.count("key_down", "4") == 1


@pytest.mark.parametrize("looted", [False, True])
def test_fetch_is_bounded_and_falls_back_to_normal_loot(tmp_path, monkeypatch, looted):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244669}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244669, state_epoch=2)
    corpse = SimpleNamespace(target_dead=True, target_id=244669, state_epoch=2, unsafe=False)
    navigation = SimpleNamespace(task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE,
                                 flags=TravelFlags.KEYS_VERIFIED, fetch_ready=True, corpse_lootable=True)
    assert run.loot_corpse(ctx, 0, 0, corpse, navigation)
    assert ctx.input.count("key_down", "l") == 1
    assert run.loot_corpse(ctx, 4, 0, corpse, navigation)
    assert not ctx.input.count("key_down", "f")
    navigation.corpse_lootable = not looted
    assert run.loot_corpse(ctx, 8, 0, corpse, navigation) is not looted
    assert ctx.input.count("key_down", "f") == int(not looted)
    assert not run.loot_corpse(ctx, 9, 0, corpse, navigation)
    assert ctx.input.count("key_down", "l") == 1


@pytest.mark.parametrize("lootable", [False, None])
def test_fetch_attempts_despite_negative_loot_flag_and_requires_feedback(tmp_path, monkeypatch, lootable):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244676}))
    fight = run.hunt.fight = SimpleNamespace(presses=1, target_id=244676, state_epoch=2)
    # Replay the live failure: P restored the killed mob, Fetch ready, lootable=False.
    run.corpse_restore = fight, 0
    corpse = SimpleNamespace(target_dead=True, target_id=244676, state_epoch=4,
                             previous_target_epoch=2, unsafe=False)
    navigation = SimpleNamespace(task=TaskFlags.PREPARED, flags=TravelFlags.KEYS_VERIFIED,
                                 fetch_ready=True, corpse_lootable=lootable)
    assert run.loot_corpse(ctx, 2, 0, corpse, navigation)
    assert ctx.input.count("key_down", "l") == 1
    assert run.loot_corpse(ctx, 2.2, 0, corpse, navigation)
    assert run.loot_for_fight is None and run.fetch_attempt is not None
    assert not run.loot_corpse(ctx, 3, 1, corpse, navigation)
    assert run.loot_for_fight is fight and run.fetch_attempt is None
    assert ctx.input.count("key_down", "l") == 1


def test_fetch_negative_flag_without_feedback_times_out_without_retry(tmp_path, monkeypatch):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244676}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244676, state_epoch=2)
    corpse = SimpleNamespace(target_dead=True, target_id=244676, state_epoch=2, unsafe=False)
    navigation = SimpleNamespace(task=TaskFlags.PREPARED, flags=TravelFlags.KEYS_VERIFIED,
                                 fetch_ready=True, corpse_lootable=False)
    assert run.loot_corpse(ctx, 0, 0, corpse, navigation)
    assert run.loot_corpse(ctx, 7.9, 0, corpse, navigation)
    assert not run.loot_corpse(ctx, 8.1, 0, corpse, navigation)
    assert not run.loot_corpse(ctx, 9, 0, corpse, navigation)
    assert ctx.input.count("key_down", "l") == 1
    assert run.loot_for_fight is None and run.fetch_attempt is None


def test_completed_kill_quest_waits_for_loot_batch_before_turn_in(tmp_path, monkeypatch):
    run, ctx, tick = make_run(tmp_path, monkeypatch, combat=True)
    run.auto_route = run.require_prepared = True
    current = [TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                           QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED,
                           loot_epoch=0, sweep_ready=True, fetch_ready=True)]
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(current[0], sequence=ctx.frame_sequence()))
    tick(QuestState.ACTIVE, target=244669)
    tick(QuestState.ACTIVE, target=244669)
    tick(QuestState.ACTIVE, target=244669)
    current[0] = replace(current[0], phase=QuestState.READY)
    tick(QuestState.READY, done=3, target=244669, dead=True)
    assert run.loot_sweep is not None and run.phase == QuestState.ACTIVE
    tick(QuestState.READY, done=3, target=244669, dead=True)
    assert ctx.input.count("key_down", "o") == 1
    current[0] = replace(current[0], loot_epoch=1)
    tick(QuestState.READY, done=3, dead=True)
    tick(QuestState.READY, done=3, dead=True)
    assert ctx.input.count("key_down", "o") == 2 and run.phase == QuestState.ACTIVE
    tick(QuestState.READY, done=3, dead=True, elapsed=8.1)
    assert run.loot_sweep is None and run.phase == QuestState.READY


@pytest.mark.parametrize("flag", [TravelFlags.MOUNTED, TravelFlags.FLYING, TravelFlags.FALLING,
                                  TravelFlags.SWIMMING, TravelFlags.CASTING, TravelFlags.IN_COMBAT])
def test_fetch_does_not_cast_in_blocked_state(tmp_path, monkeypatch, flag):
    run, ctx, _ = make_run(tmp_path, monkeypatch)
    run.hunt = quest.QuestHunt(frozenset({244676}))
    run.hunt.fight = SimpleNamespace(presses=1, target_id=244676, state_epoch=2)
    corpse = SimpleNamespace(target_dead=True, target_id=244676, state_epoch=2, unsafe=False)
    navigation = SimpleNamespace(task=TaskFlags.PREPARED, flags=flag,
                                 fetch_ready=True, corpse_lootable=False)
    assert not run.loot_corpse(ctx, 0, 0, corpse, navigation)
    assert not ctx.input.actions
