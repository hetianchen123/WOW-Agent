"""No-history NPC contact must actually retreat before any visual request."""
import math
from dataclasses import replace

import pytest

from bot.control.quest_terrain import TerrainDetour
from bot.control.quest_travel import QuestTrip
from bot.perception.travel_channel import LifeState, RecoverySample, CameraSample, CameraScene, CameraResult
from bot.modules import quest
from test_terrain_retreat import setup
from test_npc_obstacle import npc
from test_quest_travel import Flags
from test_clutter_escape import traced, inject


def contact(monkeypatch):
    _, nav, ctx, events, requests = setup(monkeypatch)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    return TerrainDetour(0, nav, 0, trip, None, contact_release=True), nav, ctx, events, requests


def test_retreats_four_yards_then_fresh_observation_not_task_success(monkeypatch):
    probe, nav, ctx, events, requests = contact(monkeypatch)
    probe.router.block_ahead(nav.position, (.2, .19))
    blocked = probe.router.blocked.copy()
    probe.observations = {"left": {"front": "open"}}
    for i in range(5):
        at = .1 + i
        command = probe.step(ctx, at, nav, 0)
        assert command.tap == "s" and command.hold_s == .18 and not command.keys
        moved = replace(nav, sequence=nav.sequence + 1,
                        position=(nav.position[0], nav.position[1] + .0016))
        assert not probe.step(ctx, at + .3, moved, 0).tap
        command = probe.step(ctx, at + .7, moved, 0)
        assert not command.keys and not command.tap and command.outcome is None
        nav = moved
    assert probe.phase == "clearance_check" and not requests
    assert probe.anchor.position == nav.position and not probe.observations
    assert probe.router.blocked == blocked and not probe.escape_checked
    steps = [data for name, data in events if name == "quest_escape_step"]
    assert len(steps) == 5 and sum(d["yards"] for d in steps) == pytest.approx(4)
    assert all(d["basis"] == "contact_release_probe" and not d["path_verified"] for d in steps)
    inject(probe, {"composition": {"usable": True, "view": "normal"}}, 6)
    assert not probe.step(ctx, 6.1, nav, 0).keys
    assert probe.phase == "scan" and not probe.escape_checked


def test_no_displacement_tries_once_then_marks_rear_failed(monkeypatch):
    probe, nav, ctx, _, requests = contact(monkeypatch)
    assert probe.step(ctx, .1, nav, 0).tap == "s"
    result = probe.step(ctx, .9, replace(nav, sequence=2), 0)
    assert not result.tap and result.outcome is None
    assert probe.retreat_pulses == 1 and probe.phase == "scan" and "back" in probe.failed
    assert not requests


def test_old_sequence_cannot_confirm_or_issue_second_pulse(monkeypatch):
    probe, nav, ctx, _, _ = contact(monkeypatch)
    assert probe.step(ctx, .1, nav, 0).tap == "s"
    for at in (.5, 1, 1.5):
        command = probe.step(ctx, at, nav, 0)
        assert not command.keys and not command.tap and command.outcome is None
    assert probe.step(ctx, 1.61, nav, 0).outcome == "probe_telemetry_stale"
    assert probe.contact_release.pulses == 1


@pytest.mark.parametrize("change", [
    {"flags": Flags.KEYS_VERIFIED | Flags.IN_COMBAT},
    {"flags": Flags.KEYS_VERIFIED | Flags.FALLING},
    {"flags": Flags.KEYS_VERIFIED | Flags.FLYING},
    {"flags": Flags.KEYS_VERIFIED | Flags.SWIMMING},
    {"flags": Flags.KEYS_VERIFIED | Flags.CASTING},
    {"flags": Flags(0)}, {"map_id": 999}, {"quest_id": 111},
    {"recovery": RecoverySample(LifeState.DEAD)},
])
def test_state_interrupt_after_first_tap_never_sends_second(change, monkeypatch):
    probe, nav, ctx, _, _ = contact(monkeypatch)
    assert probe.step(ctx, .1, nav, 0).tap == "s"
    command = probe.step(ctx, .8, replace(nav, sequence=2, **change), 0)
    assert command.outcome == "probe_interrupted" and not command.tap and not command.keys


@pytest.mark.parametrize("change,heading,reason", [
    ({"height": 90}, 0, "probe_height_drop"),
    ({}, math.pi, "probe_pose_changed"),
    ({"position": (.201, .2)}, 0, "probe_pose_changed"),
    ({"position": (.2, .206)}, 0, "probe_pose_changed"),
])
def test_bad_motion_does_not_authorize_continuation(change, heading, reason, monkeypatch):
    probe, nav, ctx, _, _ = contact(monkeypatch)
    assert probe.step(ctx, .1, nav, 0).tap == "s"
    command = probe.step(ctx, .8, replace(nav, sequence=2, **change), heading)
    assert command.outcome == reason and not command.tap and not command.keys


def test_real_npc_contact_path_sends_and_releases_s_before_model(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    # Mirror the 18:48 run: wide camera, no traveled history, nearly zero forward motion.
    nav = replace(nav, probe_camera_available=True,
                  camera=CameraSample(32, 2, CameraScene.INTERACT, CameraScene.INTERACT,
                                      1, CameraResult.DISTANCE_VERIFIED, False, False))
    run.interact_quest_target(ctx, 1, nav, 0)
    assert ctx.input._held_keys == {"w"}
    fresh = replace(nav, sequence=2)
    run.interact_quest_target(ctx, 1.6, fresh, 0)
    assert run.terrain.phase == "contact_release"
    run.interact_quest_target(ctx, 1.7, fresh, 0)
    assert ctx.input.count("key_down", "s") == ctx.input.count("key_up", "s") == 1
    assert not ctx.input._held_keys and run.ground_trace.movement_sent
    assert not any(name == "quest_terrain_request" for name, _ in events)
    assert not ctx.input.count("key_down", "left") and not ctx.input.count("key_down", "right")


def test_contact_recovery_is_cancelled_on_focus_loss(tmp_path, monkeypatch):
    from bot.control.approach import ActionResult, ActionState
    run, ctx, tick, nav, _ = npc(tmp_path, monkeypatch)
    run.require_prepared = run.execution_started = True
    run.begin_npc_detour(ctx, 0, nav, "approach_no_displacement",
                         movement_result=ActionResult(ActionState.RECOVERABLE, "no_displacement", moved_yards=0))
    run.tick_npc_detour(ctx, .1, nav, 0)
    assert ctx.input.count("key_down", "s") == 1
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: False)
    tick(nav.phase)
    assert run.OUTCOME == "stopped:game_not_frontmost" and not ctx.input._held_keys
    assert ctx.input.count("key_down", "s") == 1


def test_interaction_timeout_without_contact_does_not_enable_probe(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    run.begin_npc_detour(ctx, 0, nav, "interaction_unconfirmed")
    assert run.terrain.contact_release is None


def test_recent_measured_path_keeps_priority(monkeypatch):
    trace, nav, trip = traced()
    probe = TerrainDetour(1.3, nav, 0, trip, None, ground_trace=trace, contact_release=True)
    assert probe.phase == "backtrack" and probe.contact_release is None
