"""Back away from a contact before searching, using fresh rear-ground evidence."""
import math
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bot.control import quest_terrain as terrain
from bot.control.quest_travel import QuestTrip
from test_quest_travel import sample, Flags


def setup(monkeypatch, rear="open"):
    nav = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    events, requests = [], []
    ctx = SimpleNamespace(heading_age=lambda: 0, capture=None,
                          log_event=lambda event, data: events.append((event, data)))

    def request(frames, **kwargs):
        requests.append(kwargs["metadata"])
        future = Future()
        views = {name: {"front": rear if name == "retreat" else "blocked"} for name in frames}
        decide = kwargs.get("decide")
        future.set_result((views, decide(views) if decide else None))
        return SimpleNamespace(future=future, close=lambda: None)

    monkeypatch.setattr(terrain, "SceneRequest", request)
    monkeypatch.setattr(terrain, "capture_scene", lambda _: object())
    return terrain.TerrainDetour(0, nav, 0, trip, None), nav, ctx, events, requests


def start(probe, nav, ctx):
    assert probe.phase == "retreat_observe"
    assert not probe.step(ctx, 1, nav, math.pi).keys  # capture behind the player
    assert not probe.step(ctx, 1.2, nav, math.pi).keys  # rear observation received
    assert probe.phase == "retreat_align"
    assert not probe.step(ctx, 1.8, nav, math.pi).keys  # turn back toward original facing
    assert probe.step(ctx, 2.5, nav, 0).keys == {"s"}


def test_retreat_four_yards_then_searches_from_new_origin_without_rejoining_old_goal(monkeypatch):
    probe, nav, ctx, events, requests = setup(monkeypatch)
    start(probe, nav, ctx)
    original_estimates = probe.remaining_after_probe.copy()
    probe.observations["left"] = {"front": "open"}  # must not survive relocation
    probe.failed.add("right")
    for step in range(1, 5):
        moved = replace(nav, position=(.2, .2 + step / 500), sequence=step + 1)
        command = probe.step(ctx, 2.5 + (step - 1) * .9 + .3, moved, 0)
        assert not command.keys
        command = probe.step(ctx, 2.5 + step * .9, moved, 0)
        if step < 4:
            assert command.keys == {"s"}
    assert command.outcome is None and probe.phase == "clearance_check"
    assert probe.anchor.position == moved.position and not probe.observations and probe.failed == {"right"}
    assert probe.remaining_after_probe != original_estimates
    assert probe.destination == nav.destination
    result = next(data for event, data in events if event == "quest_terrain_retreat_result")
    assert result["yards"] == 4 and result["displacement_verified"] and not result["clearance_verified"]
    assert requests[0]["phase"] == "retreat_observe"
    # Next images are sampled at the new position, not reused from the fence.
    probe.phase = "scan"  # Complete framing is separately checked before route observations.
    probe.step(ctx, 7, moved, probe.angles["left"])
    probe.step(ctx, 8, moved, probe.angles["left"])  # Consume the first frame before requesting another.
    assert requests[-1]["pose"]["left"]["position"] == moved.position
    assert len(requests[-1]["pose"]) == 1  # Each request contains just its fresh frame.


@pytest.mark.parametrize("rear", ["blocked", "unknown"])
def test_unconfirmed_rear_skips_backing_and_keeps_existing_visual_search(monkeypatch, rear):
    probe, nav, ctx, events, _ = setup(monkeypatch, rear)
    assert not probe.step(ctx, 1, nav, math.pi).keys
    command = probe.step(ctx, 1.2, nav, math.pi)
    assert not command.keys and command.outcome is None and probe.phase == "scan"
    assert probe.retreat_pulses == 0
    assert events[-1][1]["reason"] == "rear_unconfirmed"


def test_retreat_releases_and_stops_retrying_when_backward_motion_is_blocked(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    start(probe, nav, ctx)
    assert not probe.step(ctx, 2.8, nav, 0).keys
    command = probe.step(ctx, 3.4, replace(nav, sequence=2), 0)
    assert not command.keys and probe.phase == "scan" and probe.retreat_pulses == 1
    assert events[-1][1]["reason"] == "blocked" and not events[-1][1]["clearance_verified"]


def test_retreat_needs_new_position_sequence_between_pulses(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    start(probe, nav, ctx)
    moved = replace(nav, position=(.2, .202))  # repeated sequence is not a new receipt
    assert not probe.step(ctx, 2.8, moved, 0).keys
    assert not probe.step(ctx, 3.1, moved, 0).keys
    command = probe.step(ctx, 3.6, moved, 0)
    assert not command.keys and command.outcome == "probe_telemetry_stale"


@pytest.mark.parametrize("change, outcome", [
    ({"flags": Flags.KEYS_VERIFIED | Flags.FALLING}, "probe_interrupted"),
    ({"flags": Flags.KEYS_VERIFIED | Flags.IN_COMBAT}, "probe_interrupted"),
    ({"height": 98}, "probe_height_drop"),
    ({"position": (.202, .2)}, "probe_pose_changed"),
    ({"flags": Flags(0)}, "probe_interrupted"),
])
def test_retreat_interruptions_release_backward_input(monkeypatch, change, outcome):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    start(probe, nav, ctx)
    command = probe.step(ctx, 2.6, replace(nav, **change), 0)
    assert not command.keys and command.outcome == outcome


def test_retreat_observation_expires_while_turning_back(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    probe.step(ctx, 1, nav, math.pi)
    probe.step(ctx, 1.2, nav, math.pi)
    command = probe.step(ctx, 22, nav, 0)
    assert not command.keys and probe.phase == "scan"
    assert events[-1][1]["reason"] == "rear_observation_stale"


def test_mounted_probe_dismounts_before_observing_rear(monkeypatch):
    probe, nav, ctx, _, _ = setup(monkeypatch)
    mounted = replace(nav, flags=nav.flags | Flags.MOUNTED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    probe = terrain.TerrainDetour(0, mounted, 0, trip, None)
    assert probe.step(ctx, 0, mounted, 0).tap == "8"
    assert not probe.step(ctx, 1, nav, 0).keys and probe.phase == "retreat_observe"


def test_travel_hands_backtrack_to_visual_clearance_instead_of_blind_s():
    nav = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, nav, east).keys == {"w"}
    trip.detours = 2  # short jump and turn recovery already attempted
    command = trip.step(3.1, nav, east)
    assert command.outcome == "route_blocked" and not command.keys
    assert trip.router.blocked


def test_slow_retreat_never_exceeds_four_pulses(monkeypatch):
    probe, nav, ctx, events, _ = setup(monkeypatch)
    start(probe, nav, ctx)
    for step in range(1, 5):
        moved = replace(nav, position=(.2, .2 + step * .25 / 500), sequence=step + 1)
        assert not probe.step(ctx, 2.5 + (step - 1) * .9 + .3, moved, 0).keys
        command = probe.step(ctx, 2.5 + step * .9, moved, 0)
        if step < 4:
            assert command.keys == {"s"}
    assert not command.keys and probe.phase == "clearance_check" and probe.retreat_pulses == 4
    assert events[-1][1]["reason"] == "pulse_budget"
    assert not events[-1][1]["clearance_verified"]
