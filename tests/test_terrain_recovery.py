"""Replay the 2026-11-15 rejection sequence without sending game input."""
import json
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control import quest_terrain as terrain
from bot.control.quest_travel import QuestTrip
from test_quest_travel import sample, Flags


def setup_probe(monkeypatch, views, confirmations):
    nav = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    events, captures = [], []
    ctx = SimpleNamespace(capture=None, heading_age=lambda: 0,
                          log_event=lambda name, value: events.append((name, value)))
    detour = terrain.TerrainDetour(0, nav, 0, trip, None)

    def request(frames, decide=None, **_):
        captures.append((detour.direction, tuple(frames)))
        observed = {name: {"front": confirmations.get(detour.direction, "blocked") if name == "confirm"
                          else views.get(name, "blocked")} for name in frames}
        future = Future()
        future.set_result((observed, decide(observed) if decide else None))
        return SimpleNamespace(future=future, close=lambda: None)

    monkeypatch.setattr(terrain, "capture_scene", lambda _: object())
    monkeypatch.setattr(terrain, "SceneRequest", request)
    return detour, nav, ctx, events, captures


def until_walk(detour, nav, ctx, start=0):
    for now in range(start, start + 30):
        command = detour.step(ctx, now, nav, detour.angles[detour.direction])
        if command.keys or command.outcome:
            return now, command
    raise AssertionError("probe did not reach bounded movement or an outcome")


def test_rejected_only_candidate_scans_wider_instead_of_suspending(monkeypatch):
    probe, nav, ctx, events, captures = setup_probe(monkeypatch,
        {"right": "open", "wide_right": "open"}, {"right": "blocked", "wide_right": "open"})
    _, command = until_walk(probe, nav, ctx)
    assert command.keys == {"w"} and probe.direction == "wide_right"
    assert probe.failed == {"right"}
    assert {"right", "left", "wide_right"} <= set(probe.observations)
    rejected = [e for name, e in events if name == "quest_terrain_candidate_rejected"]
    assert rejected[0]["reason"] == "confirmation_not_open"
    assert ("right", ("confirm",)) in captures and ("wide_right", ("confirm",)) in captures


def test_first_of_two_open_candidates_rejected_then_other_reconfirmed(monkeypatch):
    probe, nav, ctx, events, captures = setup_probe(monkeypatch,
        {"right": "open", "left": "open"}, {"right": "unknown", "left": "open"})
    # Goal lies to the right in this fixture, reproducing a rejected preferred candidate.
    _, command = until_walk(probe, nav, ctx)
    assert command.keys == {"w"} and probe.direction == "left"
    assert probe.failed == {"right"}
    assert sum(names == ("confirm",) for _, names in captures) == 2
    assert not any("wide_left" in names for _, names in captures)


def test_confirmed_open_but_no_displacement_tries_other_candidate(monkeypatch):
    probe, nav, ctx, events, _ = setup_probe(monkeypatch,
        {"right": "open", "left": "open"}, {"right": "open", "left": "open"})
    now, command = until_walk(probe, nav, ctx)
    assert command.keys == {"w"} and probe.direction == "right"
    command = probe.step(ctx, now + .8, replace(nav, sequence=2), probe.angles["right"])
    assert not command.keys and command.outcome is None and probe.direction == "left"
    _, command = until_walk(probe, nav, ctx, now + 2)
    assert command.keys == {"w"} and probe.failed == {"right"}
    result = next(value for name, value in events if name == "quest_terrain_step_result")
    assert result["yards"] == 0


def test_stale_confirmation_never_walks_and_falls_back_to_new_view(monkeypatch):
    probe, nav, ctx, events, _ = setup_probe(monkeypatch, {"right": "open"}, {"right": "open"})
    for now in range(20):
        command = probe.step(ctx, now, nav, probe.angles[probe.direction])
        assert not command.keys
        if probe.phase == "confirm" and probe.request:
            break
    command = probe.step(ctx, now + 21, nav, probe.angles[probe.direction])
    assert not command.keys and command.outcome is None and probe.failed == {"right"}
    assert any(value.get("reason") == "confirmation_stale" for _, value in events)
    command = probe.step(ctx, terrain.PROBE_BUDGET_S, nav, probe.angles[probe.direction])
    assert command.outcome == "probe_budget_exhausted" and not command.keys


def test_uncertain_startlux_ranking_can_only_select_another_confirmation(monkeypatch):
    probe, nav, ctx, events, _ = setup_probe(monkeypatch,
        {"right": "open", "left": "open"}, {"right": "unknown", "left": "unknown"})
    probe.observations = {"right": {"front": "open"}, "left": {"front": "open"}}
    result = probe.choose_or_scan(ctx, 1, nav, 0, {
        "recommendation": None, "reason": "low_confidence", "confidence": .16, "source": "startlux"})
    assert not result.keys and probe.phase == "confirm"
    decision = next(data["decision"] for name, data in events if name == "quest_terrain_decision")
    assert decision["selection_context"] == "uncertain_ranking_recheck_only"
    assert decision["startlux_decision"]["confidence"] == .16


@pytest.mark.parametrize("broken", [False, True])
def test_scene_request_keeps_image_pose_and_raw_result_even_on_error(tmp_path, monkeypatch, broken):
    monkeypatch.setattr(terrain, "DATA_DIR", tmp_path)
    cases = [{"parse_error": "bad JSON", "raw_output": "unreadable"}]

    def inspect(paths, **_):
        assert paths[0].is_file()
        if broken:
            raise ValueError("model unavailable")
        return cases

    monkeypatch.setattr(terrain, "inspect_images", inspect)
    request = terrain.SceneRequest({"confirm": np.zeros((5, 5, 3), dtype=np.uint8)},
                                   metadata={"id": 90895, "heading": 1.2})
    if broken:
        with pytest.raises(ValueError):
            request.future.result(timeout=2)
    else:
        views, decision = request.future.result(timeout=2)
        assert views["confirm"]["front"] == "unknown"
    report = json.loads((request.evidence_dir / "report.json").read_text())
    assert (request.evidence_dir / "confirm.png").is_file()
    assert report["metadata"]["heading"] == 1.2
    assert report.get("error") == "model unavailable" if broken else report["cases"] == cases
    request.close()
