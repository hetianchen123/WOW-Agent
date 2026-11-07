import math

import pytest

from bot.control.navigation import (
    Route,
    RouteFollower,
    compass_from_delta,
    dist,
    heading_error,
    minimap_bearing,
    minimap_distance_yards,
    wrap_pi,
)
from bot.core.config import NavigationConfig


def test_wrap_pi():
    assert wrap_pi(math.pi) == math.pi
    assert wrap_pi(-math.pi) == math.pi
    assert wrap_pi(3 * math.pi) == pytest.approx(math.pi)
    assert wrap_pi(0.5) == pytest.approx(0.5)


def test_compass_cardinals_ccw_west():
    # dx east+, dy south+; north=0, west=pi/2, south=pi, east=3pi/2
    assert compass_from_delta(0, -1) == pytest.approx(0.0)          # north
    assert compass_from_delta(-1, 0) == pytest.approx(math.pi / 2)  # west
    assert compass_from_delta(0, 1) == pytest.approx(math.pi)       # south
    assert compass_from_delta(1, 0) == pytest.approx(3 * math.pi / 2)  # east


def test_compass_cw_east_convention():
    assert compass_from_delta(1, 0, "cw_east") == pytest.approx(math.pi / 2)  # east
    assert compass_from_delta(-1, 0, "cw_east") == pytest.approx(3 * math.pi / 2)  # west


def test_heading_error_wraps():
    assert heading_error(0.1, 0.2) == pytest.approx(0.1)
    assert heading_error(0.2, 0.1) == pytest.approx(-0.1)
    assert heading_error(6.2, 0.1) == pytest.approx(0.1 - 6.2 + 2 * math.pi)


def _sim_walk(follower, start, speed=0.004, dt=0.1, ticks=400):
    """Simulate a player following decisions; returns final position."""
    x, y = start
    hdg = 0.0
    t = 0.0
    for _ in range(ticks):
        t += dt
        d = follower.update(t, (x, y), hdg, True)
        if d.finished:
            return (x, y), d
        if d.recovery:
            continue
        if d.turn_err is not None:
            hdg = (hdg + max(-0.5 * dt, min(0.5 * dt, d.turn_err))) % (2 * math.pi)
        if d.move_forward:
            # ccw_west: north=0, x_east = -sin(hdg), y_south = -cos(hdg)
            x += -math.sin(hdg) * speed * dt
            y += -math.cos(hdg) * speed * dt
    return (x, y), None


def test_route_follower_reaches_waypoints():
    route = Route(name="t", ui_map_id=1, points=[(0.5, 0.5), (0.6, 0.5), (0.6, 0.6)])
    cfg = NavigationConfig()
    follower = RouteFollower(route, cfg, loop=False)
    pos, d = _sim_walk(follower, (0.49, 0.49))
    assert d is not None and d.finish_reason == "route_complete"
    assert dist(pos, (0.6, 0.6)) < route.effective_radius() * 2


def test_stuck_detection_and_escalation():
    route = Route(name="t", ui_map_id=1, points=[(0.9, 0.9)])
    cfg = NavigationConfig()
    cfg.stuck_window_s = 2.0
    cfg.stuck_displacement = 0.001
    cfg.stuck_max_events = 2
    follower = RouteFollower(route, cfg, loop=False)
    t = 0.0
    finished = None
    stuck_events = 0
    # face the waypoint so the follower believes it should move (it never does)
    face = compass_from_delta(0.4, 0.4)
    for i in range(400):
        t += 0.1
        d = follower.update(t, (0.5, 0.5), face, True)
        if d.finished:
            finished = d
            break
        if d.stuck_event:
            stuck_events += 1
    assert finished is not None and finished.finish_reason == "stuck_escalation"
    assert stuck_events >= 1
    assert follower.stuck_count >= cfg.stuck_max_events


def test_minimap_helpers():
    assert minimap_bearing(0, -10) == pytest.approx(0.0)  # dot above center = north
    assert minimap_bearing(-10, 0) == pytest.approx(math.pi / 2)  # left = west
    assert minimap_distance_yards(30, 40, 0.5) == pytest.approx(25.0)
