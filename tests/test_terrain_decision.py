import math
from types import SimpleNamespace

from bot import terrain_cli
from bot.decision.terrain import decide_multiview, decide_terrain


class RecordingClient:
    def decide(self, state, criteria, *, instructions):
        self.criteria = criteria
        return {"recommendation": next(reversed(criteria))}


def test_terrain_never_offers_a_step_into_a_blocked_or_unknown_front():
    client = RecordingClient()
    view = {"front": "blocked", "left": "open", "right": "unknown", "next_view": "front"}
    assert decide_terrain(client, view)["recommendation"] == "scan_left"
    assert set(client.criteria) == {"stop", "scan_left"}
    view["front"] = "unknown"
    view["left"] = "unknown"
    assert decide_terrain(client, view, ("left",))["recommendation"] == "scan_right"
    assert decide_terrain(client, view, ("left", "right"))["recommendation"] == "stop"
    view["front"] = "open"
    assert decide_terrain(client, view)["recommendation"] == "advance"


def test_low_confidence_allows_stationary_observation_but_never_a_step():
    class UncertainClient:
        def decide(self, state, criteria, *, instructions):
            return {"recommendation": None, "reason": "low_confidence"}

    client = UncertainClient()
    view = {"front": "blocked", "left": "unknown", "right": "unknown", "next_view": "scan"}
    result = decide_terrain(client, view)
    assert result["recommendation"] == "scan_left" and result["fallback"] == "stationary_scan_only"
    view["front"] = "open"
    assert decide_terrain(client, view)["recommendation"] is None


def test_fresh_open_side_can_keep_turning_toward_a_passage():
    client = RecordingClient()
    view = {"front": "blocked", "left": "blocked", "right": "open", "next_view": "right"}
    assert decide_terrain(client, view, ("right",))["recommendation"] == "scan_right"
    assert decide_terrain(client, view, ("right", "right"))["recommendation"] == "scan_right"
    assert decide_terrain(client, view, ("right", "right", "right"))["recommendation"] == "stop"
    view["right"] = "unknown"
    assert decide_terrain(client, view, ("right",))["recommendation"] == "scan_left"
    assert decide_terrain(client, view, ("right", "right"))["recommendation"] == "stop"


def test_multiview_excludes_obstacles_and_previous_no_progress_headings():
    client = RecordingClient()
    views = {"front": {"front": "blocked"}, "left": {"front": "open"},
             "right": {"front": "open"}, "back": {"front": "unknown"}}
    assert decide_multiview(client, views, frozenset({"left"}))["recommendation"] == "right"
    assert set(client.criteria) == {"stop", "right"}
    assert decide_multiview(client, views, frozenset({"left", "right"}))["recommendation"] == "stop"


def test_multiview_checks_selected_heading_again_before_any_step(monkeypatch):
    pose = SimpleNamespace(world_size=(1000, 1000), map_id=2451, quest_id=90882, position=(.5, .5))
    cfg = SimpleNamespace(navigation=SimpleNamespace(facing_convention="ccw_west"))
    turns = []
    monkeypatch.setattr(terrain_cli, "ground_pose", lambda *_: (pose, 0))
    monkeypatch.setattr(terrain_cli, "turn_to", lambda heading, *_: turns.append(heading) or heading)
    monkeypatch.setattr(terrain_cli, "save_scene", lambda *_: 0)
    monkeypatch.setattr(terrain_cli, "inspect_images", lambda paths, **_: [
        {"observation": {"front": "open" if path.stem == "right" else "blocked"}} for path in paths
    ])
    monkeypatch.setattr(terrain_cli, "inspect_image", lambda *_, **__: {"observation": {"front": "blocked"}})

    def unexpected_step(*_):
        raise AssertionError("a blocked confirmation cannot send a movement key")

    monkeypatch.setattr(terrain_cli, "short_step", unexpected_step)
    terrain_cli.probe_multiview(cfg, None, None, None, RecordingClient(), [])
    assert turns == [0, math.pi / 2, math.pi, -math.pi / 2, -math.pi / 2]
