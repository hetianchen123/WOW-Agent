import math
from types import SimpleNamespace

import numpy as np

from bot.control.input_mock import MockInput
from bot.core.config import BotConfig
from bot.modules import quest
from bot.perception import channel_spec as spec
from bot.perception.quest_channel import Dialogue, Objective, QuestKind, QuestState, decode_quest
from bot.perception.travel_channel import TaskFlags, TravelFlags, TravelSample


def image(sequence, state, dialogue=Dialogue.CLOSED, dialogue_id=0, done=0, kind=QuestKind.CAMPAIGN):
    progress = Objective.COUNTED if state == QuestState.ACTIVE else Objective.UNKNOWN
    packed = int(kind) | (int(state) << 4) | (int(dialogue) << 7)
    packed |= (int(progress) << 10) | (sequence << 12) | (1 << 19)
    canvas = np.zeros((80, 142, 3), dtype=np.uint8)
    for row, value in enumerate([12345, packed, done * 1024 + 3, 2248, dialogue_id]):
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
        cfg=BotConfig(), input=MockInput(), stats={},
        capture=SimpleNamespace(grab=lambda *region: combat_frame[0] if region[-1] == 96 else frame[0]),
        channel=SimpleNamespace(status=lambda **_: (flags[0], 0)),
        pos=lambda: (0.4, 0.5), heading=lambda: 0.0, log_event=lambda *_: None,
    )
    run = quest.RunQuests(*quest.load_quest_plan(path), {QuestKind.CAMPAIGN})
    run.on_enter(ctx)

    def tick(state, dialogue=Dialogue.CLOSED, dialogue_id=0, done=0, kind=QuestKind.CAMPAIGN,
             *, target=0, dead=False, fighting=False, epoch=1, verified=True, elapsed=2.1):
        nonlocal sequence
        clock[0] += elapsed
        sequence = (sequence + 1) % 128
        frame[0] = image(sequence, state, dialogue, dialogue_id, done, kind)
        if combat:
            flags[0] = spec.StatusFlags.HP_UNKNOWN | (spec.StatusFlags.IN_COMBAT if fighting else 0)
            combat_frame[0] = np.zeros((96, 142, 3), dtype=np.uint8)
            identity = 8 | (1 << 5) | (1 << 13)
            status = int(dead) | (int(verified) << 1) | (4 << 3) | (epoch << 7) | (sequence << 13)
            for row, value in enumerate([identity, 70783, 63 | (80 << 12), 123, target, status]):
                spec.render_frame(combat_frame[0], 0, row * 16, spec.encode_frame(0xA + row, value))
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
                            run.last_sequence, interact_verified=True, task=interaction[0])

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
    interaction[0] |= TaskFlags.NPC_SELECTED
    tick(QuestState.READY)
    assert ctx.input.count("key_down", "f") == 1
    tick(QuestState.READY, Dialogue.REWARD, 12345)
    assert ctx.input.count("key_down", "9") == 1
    tick(QuestState.TURNED_IN)
    assert run.OUTCOME == "completed"


@pytest.mark.parametrize("moved", [True, False])
def test_auto_turn_in_takes_a_short_step_if_interact_does_not_open_dialogue(tmp_path, monkeypatch, moved):
    run, ctx, tick = make_run(tmp_path, monkeypatch)
    run.auto_route = True
    position = [(.4025, .5)]
    interaction = TaskFlags.PREPARED | TaskFlags.NPC_CANDIDATE | TaskFlags.NPC_SELECTED

    def navigation(_):
        return TravelSample(12345, 2248, 2248, (.403, .5), position[0], (1000, 1000),
                            0, QuestState.READY, TravelFlags.KEYS_VERIFIED,
                            run.last_sequence, interact_verified=True, task=interaction)

    monkeypatch.setattr(quest, "decode_travel", navigation)
    tick(QuestState.READY)
    assert ctx.input.count("key_down", "f") == 1
    tick(QuestState.READY, elapsed=1.3)
    assert ctx.input.count("key_down", "w") == 1
    assert ctx.input.count("key_down", "f") == 1
    tick(QuestState.READY, elapsed=.26)
    assert ctx.input.count("key_up", "w") == 1
    if moved:
        position[0] = (.404, .5)  # The NPC can be beyond the map marker.
    tick(QuestState.READY, elapsed=.3)
    if not moved:
        assert run.OUTCOME == "stopped:quest_turn_in_approach_blocked"
        assert ctx.input.count("key_down", "f") == 1
        return
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
