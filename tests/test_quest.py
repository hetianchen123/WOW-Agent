import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.input_mock import MockInput
from bot.core.config import BotConfig
from bot.modules import quest
from bot.perception import channel_spec as spec
from bot.perception.combat_channel import combat_region
from bot.perception.quest_channel import Dialogue, Objective, QuestKind, QuestState, decode_quest
from bot.perception.travel_channel import (
    LifeState, MinimapGeometry, ObjectiveAction, PetState, RecoverySample, RoundStage,
    TaskFlags, TravelFlags, TravelSample,
)


def image(sequence, state, dialogue=Dialogue.CLOSED, dialogue_id=0, done=0, kind=QuestKind.CAMPAIGN,
          quest_id=12345, required=3):
    progress = Objective.COUNTED if state == QuestState.ACTIVE else Objective.UNKNOWN
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
        pos=lambda: (0.4, 0.5), heading=lambda: 0.0, log_event=lambda *_: None,
    )
    run = quest.RunQuests(*quest.load_quest_plan(path), {QuestKind.CAMPAIGN})
    run.on_enter(ctx)

    def tick(state, dialogue=Dialogue.CLOSED, dialogue_id=0, done=0, kind=QuestKind.CAMPAIGN,
             *, target=0, dead=False, fighting=False, epoch=1, verified=True, elapsed=2.1, quest_id=12345,
             required=3, engaged=False, facing=None, facing_error=0):
        nonlocal sequence
        clock[0] += elapsed
        sequence = (sequence + 1) % 128
        frame[0] = image(sequence, state, dialogue, dialogue_id, done, kind, quest_id, required)
        if combat:
            flags[0] = spec.StatusFlags.HP_UNKNOWN | (spec.StatusFlags.IN_COMBAT if fighting else 0)
            combat_frame[0] = np.zeros((112, 142, 3), dtype=np.uint8)
            identity = 8 | (1 << 5) | (1 << 13)
            status = int(dead) | (int(verified) << 1) | (4 << 3) | (epoch << 7) | (sequence << 13)
            spec_level = 63 | (80 << 12) | (int(engaged) << 19)
            for row, value in enumerate([identity, 70783, spec_level, 123, target, status]):
                spec.render_frame(combat_frame[0], 0, (row + 1) * 16, spec.encode_frame(0xA + row, value))
            if facing is not None:
                angle = round(facing * 511 / math.tau) % 511 + 1
                facing_bits = angle | sequence << 9 | facing_error << 16 | 1 << 19
                spec.render_frame(combat_frame[0], 0, 0, spec.encode_frame(9, facing_bits))
        run.tick(ctx)

    return run, ctx, tick


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
    position = [(.4, .5)]
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
    position[0] = (.4018, .5)
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
                          capture=SimpleNamespace(grab=lambda *region: captures.append(region)))
    start.on_enter(ctx)
    now[0] = 14
    start.tick(ctx)
    assert start.OUTCOME is None and not captures
    focused[0] = True
    now[0] = 15
    start.tick(ctx)
    assert start.OUTCOME is None and captures
    now[0] = 21
    start.tick(ctx)
    assert start.OUTCOME == "stopped:quest_navigation_not_prepared"


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
    run.hunt = SimpleNamespace(fight=None, step=lambda *_: ("target_not_found", None))
    monkeypatch.setattr(quest, "decode_combat", lambda _: None)
    nav = TravelSample(12345, 2451, 2451, (.44, .5), (.44, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1)
    run.hunt_tick(ctx, 10, 0, False, nav)
    assert isinstance(run.hunt, quest.QuestHunt) and run.search_waypoint is not None
    assert not ctx.input.count("move")


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
    ghost = RecoverySample(LifeState.GHOST, corpse=(.43, .5))
    current = [alive]
    nav = TravelSample(12345, 2248, 2248, (.4, .5), (.4, .5), (1000, 1000), 0,
                       QuestState.ACTIVE, TravelFlags.KEYS_VERIFIED, 1, task=TaskFlags.PREPARED)
    monkeypatch.setattr(quest, "decode_travel", lambda _: replace(
        nav, sequence=ctx.frame_sequence(), recovery=current[0]))

    tick(QuestState.ACTIVE)
    current[0] = release
    tick(QuestState.ACTIVE)
    assert ctx.input.last("move")[1] == (800, 385)
    assert not ctx.input.count("mouse_down")
    current[0] = replace(release, hovered=True)
    tick(QuestState.ACTIVE, elapsed=.3)
    assert ctx.input.count("mouse_down", "left") == 1
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
    assert ctx.input.count("mouse_down", "left") == 2

    prep = replace(alive, pet=PetState.ABSENT, pet_expected=True, follow=True, summon=True,
                   revive=True, buff_missing=True)
    current[0] = prep
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
    current[0] = replace(current[0], buff_missing=False)
    tick(QuestState.ACTIVE, elapsed=.4)
    assert run.recovery is None and ctx.stats["quest_recoveries"] == 1
    tick(QuestState.ACTIVE)
    assert run.OUTCOME is None and run.arrived


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
    assert ctx.stats == {"quests_accepted": 2, "quests_done": 2, "quest_rounds_done": 1}


@pytest.mark.parametrize("moved, range_known", [(True, True), (True, False), (False, True)])
def test_auto_turn_in_requires_five_yard_confirmation(tmp_path, monkeypatch, moved, range_known):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    position = [(.4025, .5)]
    interaction = [TaskFlags.PREPARED | TaskFlags.NPC_CANDIDATE | TaskFlags.NPC_SELECTED]
    if range_known:
        interaction[0] |= TaskFlags.NPC_RANGE_KNOWN

    def navigation(_):
        return TravelSample(12345, 2248, 2248, (.403, .5), position[0], (1000, 1000),
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
        position[0] = (.404, .5)  # The NPC can be beyond the map marker.
        interaction[0] |= TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    tick(QuestState.READY, elapsed=.3)
    if not moved:
        assert run.OUTCOME == "stopped:quest_turn_in_approach_blocked"
        assert ctx.input.count("key_down", "f") == 0
        return
    assert ctx.input.count("key_down", "f") == 1
    # The client can still refuse an in-range interaction. Retry after a step.
    tick(QuestState.READY, elapsed=1.3)
    assert ctx.input.count("key_down", "w") == 2
    assert ctx.input.count("key_down", "f") == 1
    position[0] = (.405, .5)
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
    assert ctx.input.count("key_down", "4") == 0
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
    tick(QuestState.ACTIVE, fighting=True, target=901, engaged=True, facing=math.pi, facing_error=1)
    assert ctx.input.count("key_down", "4") == 2
    flags[0] = TravelFlags.KEYS_VERIFIED
    tick(QuestState.ACTIVE)
    tick(QuestState.ACTIVE)
    assert run.OUTCOME is None and run.defense is None
    assert ctx.input.count("move") == 1


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
