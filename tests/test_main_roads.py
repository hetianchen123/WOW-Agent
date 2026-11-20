from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.control import road_scan
from bot.control.pathfinding import GridRouter
from bot.control.quest_travel import QuestTrip
from bot.control.road_route import RoadTrace, preferred_road
from bot.perception import channel_spec as pixels
from bot.perception.quest_channel import QuestState
from bot.perception.road_map import (
    MapIdentity, RoadMapStatus, decode_road_status, map_crop, parse_traces, supported_traces, visible_map_art,
)
from bot.perception.travel_channel import TravelFlags, TravelSample


def pixel_map():
    canvas = np.full((420, 620, 3), (45, 87, 42), np.uint8)
    cv2.line(canvas, (60, 210), (550, 210), (222, 174, 116), 5)
    cv2.rectangle(canvas, (40, 35), (110, 110), (160, 181, 130), -1)
    return canvas


def test_map_crop_rejects_missing_marker_and_uses_full_canvas_bounds():
    rgb = np.zeros((520, 900, 3), np.uint8)
    rgb[55:475, 140:760] = pixel_map()
    displayed_colors = ((253, 43, 255), (23, 255, 255), (255, 252, 3), (2, 46, 255))
    for (x, y), color in zip(((140, 55), (750, 55), (140, 465), (750, 465)), displayed_colors):
        rgb[y:y + 10, x:x + 10] = color
    canvas = map_crop(rgb)
    assert canvas.shape == (420, 620, 3)
    assert np.array_equal(canvas[210], pixel_map()[210])
    rgb[465:475, 750:760] = 0
    assert map_crop(rgb) is None


def test_road_status_disallows_torn_frames():
    identity = MapIdentity(734588, 70959, 1429, 16035)
    values = [0xB38, 32]
    for value in (identity.map_id, identity.art, identity.profile, identity.build):
        values.extend(divmod(value, 4096)[::-1])
    canvas = np.zeros((160, 142, 3), np.uint8)
    for row, value in enumerate(values):
        pixels.render_frame(canvas, 0, row * 16, pixels.encode_frame(row, value + 93 * 4096))
    assert decode_road_status(canvas) == RoadMapStatus(93, identity, False, 2)
    pixels.render_frame(canvas, 0, 16, pixels.encode_frame(1, 32 + 92 * 4096))
    assert decode_road_status(canvas) is None


def test_map_candidates_need_visible_corridor_and_valid_normalized_points():
    payload = {"roads": [[[70, 462], [930, 462]],
                         [[150, 700], [500, 700]], [[100, 200], [1001, 500]],
                         [[250, True], [500, 200]]]}
    traces = parse_traces(payload)
    assert len(traces) == 2
    confirmed, = supported_traces(pixel_map(), traces)
    assert len(confirmed) >= 2
    assert all(abs(y * 419 - 210) < 3 and 55 <= x * 619 <= 555 for x, y in confirmed)


def test_partially_explored_parchment_excludes_ridges_outside_colored_terrain():
    canvas = np.full((420, 620, 3), (143, 96, 32), np.uint8)
    canvas[155:285, 230:400] = (75, 110, 39)
    cv2.line(canvas, (245, 210), (380, 210), (222, 174, 116), 5)
    cv2.line(canvas, (55, 90), (530, 90), (222, 174, 116), 7)
    visible, _ = visible_map_art(canvas)
    proposals = [((.08, .215), (.8, .215)), ((.40, .5), (.61, .5))]
    assert visible[210, 265] and not visible[90, 265]
    confirmed, = supported_traces(canvas, proposals, visible)
    assert all(abs(y * 419 - 210) < 3 for _, y in confirmed)


def test_model_arithmetic_progression_does_not_create_a_route():
    payload = {"roads": [[[420 + 60 * i, 380 + 40 * i] for i in range(10)],
                         [[420, 500], [480, 530], [550, 570], [620, 600]],
                         [[100, 500], [150, 530], [200, 560]]]}
    assert parse_traces(payload) == []
    assert len(parse_traces(payload, model=False)) == 3

def test_road_graph_joins_known_junctions_preserves_corners_and_directions():
    router = GridRouter(1000, 1000)
    start, goal = (.10, .40), (.33, .40)
    corner, junction = (.17, .44), (.24, .44)
    traces = [RoadTrace(((.10, .42), corner, junction), "walk", False),
              RoadTrace((junction, (.33, .42)), "walk", False)]
    path = preferred_road(router, start, goal, traces, "walk")
    assert corner in path and junction in path and path[-1] == goal
    assert preferred_road(router, goal, start, traces, "walk") == []
    assert preferred_road(router, start, goal, traces, "mount") == []
    router.blocked.add(router.cell(junction))
    partial = preferred_road(router, start, goal, traces, "walk")
    assert partial and partial[-1] == corner
    assert all(router.clear_segment(a, b) for a, b in zip([start] + partial, partial))


def test_partial_path_ends_at_the_last_mapped_waypoint():
    start, end, goal = (.1, .2), (.34, .25), (.7, .2)
    trace = RoadTrace((start, (.2, .25), end))
    router = GridRouter(1000., 1000.)
    assert preferred_road(router, start, goal, [trace], "walk")[-1] == end
    flags = TravelFlags.OUTDOORS | TravelFlags.KEYS_VERIFIED
    sample = TravelSample(54, 1429, 1429, goal, start, (1000., 1000.), 0, QuestState.READY, flags, 1)
    trip = QuestTrip(sample.world_size, goal, "ccw_west")
    trip.road_traces = (trace,)
    trip.step(0, sample, 1.5 * np.pi)
    assert trip.path == [(.2, .25), end]
    trip.step(1, replace(sample, sequence=2, position=(.2, .25)), 1.5 * np.pi)
    assert not trip.step(2, replace(sample, sequence=3, position=end), 1.5 * np.pi).keys
    assert trip.road_disabled and trip.phase == "choose"
    assert trip.step(3, replace(sample, sequence=4, position=end), 1.5 * np.pi).outcome is None
    assert trip.path == [goal]

def test_road_trip_releases_keys_and_abandons_candidate_on_confirmed_contact():
    flags = TravelFlags.OUTDOORS | TravelFlags.KEYS_VERIFIED
    sample = TravelSample(54, 1429, 1429, (.4, .2), (.2, .2), (1000., 1000.), 0,
                          QuestState.ACTIVE, flags, 1)
    events = []
    trip = QuestTrip(sample.world_size, sample.destination, "ccw_west", outdoor_detour=True,
                     log_event=lambda name, data: events.append((name, data)))
    trip.road_traces = (RoadTrace(((.2, .2), (.3, .2), (.4, .2))),)
    east = 1.5 * np.pi
    assert trip.step(0, sample, east).keys == {"w"}
    assert trip.road_origin == sample.position
    for sequence in (2, 3):
        assert trip.step(sequence * .3, replace(sample, sequence=sequence), east).keys == {"w"}
    assert trip.step(1.6, replace(sample, sequence=3), east).keys == {"w"}
    command = trip.step(1.7, replace(sample, sequence=4), east)
    assert not command.keys and command.tap is None
    assert trip.road_disabled and trip.road_origin is None and trip.router.blocked
    assert [name for name, _ in events] == ["quest_main_road_selected", "quest_main_road_released"]


def test_manual_roads_read_reloaded_profile_and_keep_map_boundary(tmp_path):
    account = tmp_path / "WTF/Account/Example/SavedVariables"
    account.mkdir(parents=True)
    path = account / "WowAgent.lua"
    path.write_text('''
        WowAgentDB = {roads = {schema = 1, profiles = {["2:无限"] = {routes = {
            [1] = {mode="walk", bidirectional=true, points={
                {mapID=1429, artID=10, x=.1, y=.2}, {mapID=1429, artID=10, x=.2, y=.2},
                {mapID=999, artID=11, x=.3, y=.2}, {mapID=1429, artID=10, x=.4, y=.2},
                {mapID=1429, artID=10, x=.5, y=.2}}}}}}}}
        '''.replace("=.", "=0."), encoding="utf-8")
    identity = MapIdentity(road_scan.profile_token("2:无限"), 70959, 1429, 10)
    traces = road_scan.manual_traces(tmp_path, identity)
    assert [trace.points for trace in traces] == [((.1, .2), (.2, .2)), ((.4, .2), (.5, .2))]
    assert road_scan.manual_traces(tmp_path, replace(identity, art=11)) == ()
    assert road_scan.manual_traces(tmp_path, replace(identity, profile=100)) == ()


@pytest.mark.parametrize("raised,button_y", [(False, 656), (True, 400)])
def test_scan_is_armed_only_after_settling_and_caches_verified_art(monkeypatch, tmp_path, raised, button_y):
    identity = MapIdentity(1, 70959, 1429, 10)
    controller = road_scan.RoadScanSession(tmp_path)
    monkeypatch.setattr(road_scan, "installed_forever", lambda: None)
    monkeypatch.setattr(road_scan, "local_vision_available", lambda: True)
    canvas = pixel_map()
    monkeypatch.setattr(road_scan, "game_window_pixels", lambda: canvas)
    monkeypatch.setattr(road_scan, "map_crop", lambda image: image if image is canvas else None)
    def infer(image, folder, cancelled):
        folder.mkdir()
        cv2.imwrite(str(folder / "map.png"), image)
        return (((.1, .2), (.3, .2)),)
    monkeypatch.setattr(road_scan, "infer_roads", infer)
    clicks, points, releases, events = [], [], [], []
    ctx = SimpleNamespace(input=SimpleNamespace(click_ui=lambda: clicks.append("click"),
                          move_to=lambda x, y, jitter_px: points.append((x, y, jitter_px))),
                          cfg=SimpleNamespace(channel=SimpleNamespace(x=40, y=830)),
                          capture=SimpleNamespace(grab=lambda *r: None),
                          log_event=lambda name, data: events.append(name))

    def tick(now, seq, state=0, allow=True):
        return controller.tick(ctx, now, RoadMapStatus(seq, identity, state == 0, state, raised=raised),
                               allow=allow, release=lambda: releases.append(now))

    assert tick(0, 10) and not clicks
    assert tick(.3, 10) and not clicks
    assert tick(.4, 11) and clicks == ["click"]
    assert points == [(543, button_y, 0)]
    assert tick(.8, 12, 2, allow=False) and controller.screenshot is not None
    assert tick(2.9, 13) and clicks == ["click"]
    controller.inference[0].result(timeout=2)
    assert not tick(3.1, 14)
    assert events == ["quest_main_road_loaded", "quest_main_road_scan_requested",
                      "quest_main_road_canvas_captured", "quest_main_road_scan_result"]
    assert (tmp_path / identity.key / "marked.png").is_file()
    cache = road_scan.RoadScanSession(tmp_path)
    cache.reset_identity(identity)
    assert identity in cache.checked and cache.detected
    cache.reset_identity(replace(identity, art=11))
    assert cache.detected == ()


def test_missing_roads_get_one_rescan_after_travel_without_waiting_for_inference(monkeypatch, tmp_path):
    identity = MapIdentity(1, 70959, 1429, 10)
    controller = road_scan.RoadScanSession(tmp_path)
    monkeypatch.setattr(road_scan, "installed_forever", lambda: None)
    monkeypatch.setattr(road_scan, "local_vision_available", lambda: True)
    monkeypatch.setattr(road_scan, "game_window_pixels", pixel_map)
    monkeypatch.setattr(road_scan, "map_crop", lambda image: image)
    finish = Event()

    def infer(image, folder, cancelled):
        folder.mkdir(exist_ok=True)
        finish.wait(2)
        return ()

    monkeypatch.setattr(road_scan, "infer_roads", infer)
    requested = []
    ctx = SimpleNamespace(input=SimpleNamespace(click_ui=lambda: None, move_to=lambda *args, **kwargs: None),
                          cfg=SimpleNamespace(channel=SimpleNamespace(x=40, y=830)),
                          log_event=lambda name, data: requested.append(data["rescan_after_travel"])
                          if name == "quest_main_road_scan_requested" else None)

    def tick(now, seq, x, state=0):
        return controller.tick(ctx, now, RoadMapStatus(seq, identity, state == 0, state),
                               position=(x, .42), world_size=(1000, 1000), allow=state == 0, release=lambda: None)

    assert tick(0, 1, .2) and tick(.4, 2, .2)
    assert tick(4, 3, .2) and controller.request_at is None
    assert not tick(59, 4, .6)
    assert not tick(70, 5, .3)
    assert tick(71, 6, .43) and tick(71.4, 7, .43)
    assert requested == [False, True]
    assert tick(72, 8, .43, 2) and tick(73, 9, .43)
    try:
        assert not tick(73.1, 10, .43)
    finally:
        finish.set()
        controller.inference[0].result(timeout=2)
    assert not tick(150, 11, .7)
    assert requested == [False, True]


@pytest.mark.parametrize("cache_age,rescan_at,rescan_x", [(120, 2, .2), (10, 71, .45)])
def test_unmatched_cached_road_allows_one_rescan(monkeypatch, tmp_path, cache_age, rescan_at, rescan_x):
    identity = MapIdentity(1, 70959, 1429, 10)
    trace = RoadTrace(((.10, .20), (.25, .20)))
    controller = road_scan.RoadScanSession(tmp_path)
    monkeypatch.setattr(road_scan, "installed_forever", lambda: None)
    monkeypatch.setattr(road_scan, "local_vision_available", lambda: True)
    monkeypatch.setattr(road_scan, "load_observation",
                        lambda _: (identity, [trace.points], road_scan.time.time() - cache_age))
    events = []
    ctx = SimpleNamespace(input=SimpleNamespace(click_ui=lambda: None, move_to=lambda *a, **k: None),
                          cfg=SimpleNamespace(channel=SimpleNamespace(x=40, y=830)),
                          log_event=lambda name, data: events.append((name, data)))

    def tick(now, seq, x, needs_coverage=False):
        return controller.tick(ctx, now, RoadMapStatus(seq, identity, True, 0), allow=True,
                               position=(x, .20), world_size=(1000, 1000), release=lambda: None,
                               needs_coverage=needs_coverage)

    assert not tick(0, 1, .2)
    assert not tick(1, 2, .45)
    if cache_age < 60:
        assert not tick(59, 3, .45, needs_coverage=True)
    assert tick(rescan_at, 4, rescan_x, needs_coverage=True)
    assert tick(rescan_at + .4, 5, rescan_x, needs_coverage=True)
    assert events[-1][0] == "quest_main_road_scan_requested" and events[-1][1]["rescan_after_travel"]
    assert tick(rescan_at + 4, 6, rescan_x, needs_coverage=True) and controller.request_at is None
    assert not tick(rescan_at + 74, 7, .72, needs_coverage=True)
