import math
from dataclasses import replace
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np

from bot.control.pathfinding import GridRouter
from bot.control import quest_terrain
from bot.control.quest_travel import QuestTrip
from bot.perception import channel_spec as spec
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import (
    ChoiceStep, CorpseSource, ForeverStep, LifeState, ObjectiveAction, RoundStage, SkyFlight, TaskFlags, TravelFlags as Flags, TravelSample,
    decode_travel,
)


def sample(**changes):
    base = TravelSample(90882, 2451, 2451, (.3, .2), (.2, .2), (1000., 500.), 100.,
                        QuestState.ACTIVE, Flags.CAN_FLY | Flags.CAN_MOUNT | Flags.KEYS_VERIFIED, 1,
                        descent_key="c")
    return replace(base, **changes)


def test_ground_stall_tries_jump_turn_jump_and_both_strafes():
    grounded = sample(flags=Flags.KEYS_VERIFIED, strafe_verified=True)
    trip = QuestTrip(grounded.world_size, grounded.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, grounded, east).keys == {"w"}
    jump = trip.step(3.1, grounded, east)
    assert jump.keys == {"w"} and jump.tap == "space"
    assert trip.step(3.8, grounded, east).keys == {"w"}
    turn = trip.step(7, grounded, east)
    assert not turn.keys and turn.tap in {"left", "right"}
    jump = trip.step(7.2, grounded, east)
    assert jump.keys == {"w"} and jump.tap == "space"
    assert trip.step(7.9, grounded, east).keys == {"w"}
    assert trip.step(11, grounded, east).keys == {"q"}
    assert trip.step(11.7, grounded, east).keys == {"w"}
    assert trip.step(14.9, grounded, east).keys == {"e"}


def test_ground_slide_along_wall_does_not_reset_forward_progress_timer():
    nav = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, nav, east).keys == {"w"}
    assert trip.step(1.6, replace(nav, position=(.2, .206), sequence=2), east).tap is None
    assert trip.step(3.1, replace(nav, position=(.2, .212), sequence=3), east).tap == "space"


def test_terrain_detour_observes_both_sides_rechecks_then_confirms_displacement(monkeypatch):
    nav = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    events = {}
    ctx = SimpleNamespace(heading_age=lambda: 0, capture=None, log_event=events.__setitem__)
    requests = []

    def request(frames, decide=None, **_):
        requests.append(tuple(frames))
        future = Future()
        views = {name: {"front": "blocked"} if name == "left" else
                 {"front": "open", "feature": "ramp", "landmark": [500, 690]} for name in frames}
        future.set_result((views, decide(views) if decide else None))
        return SimpleNamespace(future=future, close=lambda: None)

    monkeypatch.setattr(quest_terrain, "SceneRequest", request)
    monkeypatch.setattr(quest_terrain, "capture_scene", lambda _: object())
    client = SimpleNamespace(decide=lambda state, choices, **_: {"recommendation": "right"})
    detour = quest_terrain.TerrainDetour(0, nav, 0, trip, client, retreat_first=False)
    assert detour.step(ctx, 0, nav, 0).tap == "left"
    left, right = math.radians(55), math.radians(-55)
    assert not detour.step(ctx, .6, nav, left).keys
    assert not detour.step(ctx, 1.2, nav, left).keys  # Consume the left result first.
    assert detour.step(ctx, 1.8, nav, left).tap == "right"
    assert not detour.step(ctx, 2.4, nav, right).keys
    assert requests == [("left",), ("right",)]
    assert not detour.step(ctx, 2.6, nav, right).keys
    assert not detour.step(ctx, 3.2, nav, right).keys
    assert requests[-1] == ("confirm",)
    assert detour.step(ctx, 3.4, nav, right).keys == {"w"}
    moved = replace(nav, position=(.206, .191))
    assert detour.step(ctx, 4.2, moved, right).outcome == "detoured"
    mark = events["quest_terrain_waypoint"]
    assert mark["position"] == moved.position and mark["landmark_on_screen"] == [500, 690]
    assert trip.router.passages
    interrupted = replace(nav, flags=nav.flags | Flags.IN_COMBAT)
    assert detour.step(ctx, 4.3, interrupted, right).outcome == "probe_interrupted"


def test_terrain_unknown_or_blocked_views_never_authorize_walking(monkeypatch):
    nav = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west")
    ctx = SimpleNamespace(heading_age=lambda: 0, capture=None, log_event=lambda *_: None)

    def request(frames, decide=None, **_):
        future = Future()
        views = {name: {"front": "blocked" if name == "left" else "unknown"} for name in frames}
        future.set_result((views, decide(views) if decide else None))
        return SimpleNamespace(future=future, close=lambda: None)

    monkeypatch.setattr(quest_terrain, "SceneRequest", request)
    monkeypatch.setattr(quest_terrain, "capture_scene", lambda _: object())
    detour = quest_terrain.TerrainDetour(0, nav, 0, trip, None)
    commands = []
    for now in range(25):
        commands.append(detour.step(ctx, now, nav, detour.angles[detour.direction]))
        if commands[-1].outcome:
            break
    assert commands[-1].outcome == "no_passage"
    assert set(detour.observations) == {"left", "right", "wide_left", "wide_right", "back"}
    assert all(not command.keys and command.tap != "space" for command in commands)


def test_fly_before_mount_and_land_before_dismount():
    start = sample()
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, start, east).tap == "6"
    mounted = replace(start, flags=start.flags | Flags.MOUNTED | Flags.STYLE_SWITCH | Flags.SKYRIDING,
                      flight_mount_active=True)
    assert trip.step(2, mounted, east).tap == "5"
    assert not trip.step(3, mounted, east).keys
    steady = replace(mounted, flags=mounted.flags & ~Flags.SKYRIDING)
    assert trip.step(7, steady, east).keys == {"space"}
    flying = replace(steady, flags=steady.flags | Flags.FLYING, height=106)
    assert trip.step(8, flying, east).keys == {"space"}
    cruising = replace(flying, height=112)
    assert trip.step(9, cruising, east).keys == {"w"}
    assert trip.step(9.2, replace(cruising, height=116), east).keys == {"w", "c"}
    above_goal = replace(cruising, position=start.destination)
    assert trip.step(10, above_goal, east).keys == {"c"}
    falling = replace(above_goal, flags=(above_goal.flags & ~Flags.FLYING) | Flags.FALLING)
    assert trip.step(11, falling, east).tap is None
    landed = replace(falling, flags=falling.flags & ~Flags.FALLING)
    assert trip.step(12, landed, east).tap == "8"
    assert trip.step(13, landed, east).outcome is None
    grounded = replace(landed, flags=landed.flags & ~Flags.MOUNTED)
    assert trip.step(14, grounded, east).outcome == "arrived"


def test_local_object_travel_mounts_for_the_long_leg_and_walks_at_the_end():
    east = 3 * math.pi / 2
    start = sample(position=(.224, .2))  # 76 yards, like the catapult approach in the log.
    trip = QuestTrip(start.world_size, start.destination, "ccw_west", local_travel=True, approach_on_foot=True)
    assert trip.step(0, start, east).tap == "6"
    middle = replace(start, position=(.260, .2))
    trip = QuestTrip(start.world_size, start.destination, "ccw_west", local_travel=True, approach_on_foot=True)
    assert trip.step(0, middle, east).tap == "7"
    riding = replace(middle, flags=(middle.flags | Flags.MOUNTED) & ~Flags.CAN_MOUNT)
    assert trip.step(2, riding, east).keys == {"w"}
    close = replace(riding, position=(.292, .2))
    assert trip.step(5, close, east).tap == "8"
    walking = replace(close, flags=middle.flags, sequence=2)
    assert trip.step(6, walking, east).tap == "w"
    assert trip.mode == "walk"


def test_summon_timeout_falls_back_to_ground_mount_then_walk():
    start = sample()
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, start, east).tap == "6"
    assert trip.step(6, start, east).tap is None
    assert trip.step(6.2, start, east).tap == "7"
    assert trip.step(12, start, east).tap is None
    assert trip.step(12.2, start, east).keys == {"w"}
    assert trip.mode == "walk"


def test_missing_descent_binding_uses_ground_mount_and_never_guesses_a_key_in_flight():
    start = sample(descent_key=None)
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, start, east).tap == "7"
    airborne = replace(start, flags=start.flags | Flags.MOUNTED | Flags.FLYING)
    result = trip.step(1, airborne, east)
    assert result.outcome == "descent_binding_unavailable" and not result.keys


def test_skyriding_launches_brakes_lands_and_waits_for_dismount():
    start = sample(descent_key=None, sky_pitch_key="f12", sky_dismount_key="f11")
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, start, east).tap == "6"
    mounted = replace(start, flags=start.flags | Flags.MOUNTED | Flags.SKYRIDING)
    assert trip.step(2, mounted, east).tap is None
    assert trip.step(3, replace(mounted, sky_ascent_key="2"), east).tap == "2"
    flying = replace(mounted, flags=mounted.flags | Flags.FLYING)
    assert not trip.step(4, flying, east).keys
    approach = replace(flying, position=(.275, .2), sky_halt_key="4")
    assert trip.step(6, approach, east).tap == "4"
    assert trip.step(7, approach, east).keys == {"f12", "w"}  # dive-land near the marker
    above = replace(approach, position=start.destination)
    assert trip.step(8, above, east).tap is None
    landed = replace(mounted, position=start.destination)
    assert trip.step(9, landed, east).tap == "f11"
    assert trip.step(10, landed, east).outcome is None
    grounded = replace(start, position=start.destination)
    assert trip.step(11, grounded, east).outcome is None
    assert trip.step(12, grounded, east).outcome == "arrived"


def test_skyriding_landing_stall_releases_pitch_and_tries_one_offset_approach():
    start = sample(position=(.275, .2), height=0, descent_key=None,
                   sky_pitch_key="0", sky_dismount_key="z", sky_ascent_key="2")
    airborne = replace(start, flags=start.flags | Flags.MOUNTED | Flags.SKYRIDING | Flags.FLYING)
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert not trip.step(0, airborne, east).keys
    assert trip.step(.4, airborne, east).keys == {"0", "w"}  # stationary hover dives forward
    assert not trip.step(.9, airborne, east).keys
    recovery = trip.step(1.9, airborne, east)
    assert recovery.tap == "2" and not recovery.keys
    assert math.isclose(trip.router.distance(start.position, trip.sky_landing_target), 30)
    assert not trip.step(2.2, airborne, east).keys
    retry = replace(airborne, position=trip.sky_landing_target)
    trip.step(2.6, retry, east)
    assert trip.step(3, retry, east).keys == {"0", "w"}  # offset retry also dives forward
    assert trip.step(4.5, retry, east).outcome == "skyriding_landing_blocked"


def test_skyriding_resume_near_destination_dismounts_and_walks():
    start = sample(position=(.291, .2), descent_key=None,
                   sky_pitch_key="0", sky_dismount_key="z", sky_ascent_key="2")
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    mounted = replace(start, flags=start.flags | Flags.MOUNTED | Flags.SKYRIDING)
    east = 3 * math.pi / 2
    assert trip.step(0, mounted, east).tap == "z"
    assert trip.step(1, start, east).tap is None
    assert trip.step(2, start, east).keys == {"w"}
    assert trip.mode == "walk"


def test_skyriding_free_launch_replenishes_then_uses_surge_without_spending_reserve():
    east = 3 * math.pi / 2
    mounted = sample(destination=(.8, .2), sky_pitch_key="f12", sky_dismount_key="f11",
                     flags=Flags.MOUNTED | Flags.SKYRIDING | Flags.KEYS_VERIFIED,
                     sky_flight=SkyFlight(charges=0))
    events = []
    trip = QuestTrip(mounted.world_size, mounted.destination, "ccw_west",
                     log_event=lambda event, fields: events.append((event, fields)))
    assert trip.step(0, mounted, east).tap == "space"
    jumping = replace(mounted, flags=mounted.flags | Flags.FALLING, sequence=2)
    assert trip.step(.15, jumping, east).tap is None
    assert trip.step(.4, jumping, east).tap == "space"
    assert trip.step(.6, jumping, east).tap is None
    flight = SkyFlight(surge_key="1", second_wind_key="5", speed=20, charges=1)
    airborne = replace(mounted, flags=mounted.flags | Flags.FLYING, height=118, sequence=3, sky_flight=flight)
    assert trip.step(.8, airborne, east).tap is None
    assert trip.step(1.3, airborne, east).tap == "5"
    assert trip.step(1.9, airborne, east).tap is None
    restored = replace(airborne, sequence=4, sky_flight=replace(flight, charges=2, cast="second_wind", cast_epoch=1))
    assert trip.step(3, restored, east).tap == "1"
    used = replace(restored, sequence=5, sky_flight=replace(flight, cast="surge", cast_epoch=2))
    assert trip.step(5, used, east).tap == "5"
    assert [event[1]["action"] for event in events if event[0].endswith("cast_result")] == ["second_wind", "surge"]
    assert all(event[1]["confirmed"] for event in events if event[0].endswith("cast_result"))


def test_skyriding_unconfirmed_boost_is_not_repeated_and_fast_approach_brakes_early():
    east = 3 * math.pi / 2
    flight = SkyFlight(surge_key="1", whirling_key="3", speed=16, charges=3)
    airborne = sample(destination=(.8, .2), sky_pitch_key="f12", sky_dismount_key="f11", sky_halt_key="4",
                      flags=Flags.MOUNTED | Flags.SKYRIDING | Flags.FLYING | Flags.KEYS_VERIFIED, sky_flight=flight)
    trip = QuestTrip(airborne.world_size, airborne.destination, "ccw_west")
    trip.step(0, airborne, east)
    assert trip.step(.5, airborne, east).tap == "3"
    assert trip.step(1.9, airborne, east).tap is None
    # The game never acknowledged Whirling Surge, so try the independently verified Surge Forward.
    assert trip.step(2.6, replace(airborne, sequence=2), east).tap == "1"
    fast = replace(airborne, position=(.7, .2), sequence=3,
                   sky_flight=replace(flight, speed=72, charges=2, cast="surge", cast_epoch=1))
    assert trip.step(3, fast, east).tap == "4"
    assert trip.phase == "sky_brake"


def test_skyriding_retries_unconfirmed_ascent_and_climbs_again_when_sinking():
    east = 3 * math.pi / 2
    mounted = sample(destination=(.95, .2), sky_pitch_key="0", sky_dismount_key="z", sky_ascent_key="2",
                     flags=Flags.MOUNTED | Flags.SKYRIDING | Flags.KEYS_VERIFIED, sky_flight=SkyFlight(charges=6))
    trip = QuestTrip(mounted.world_size, mounted.destination, "ccw_west")
    assert trip.step(0, mounted, east).tap == "space"
    airborne = replace(mounted, flags=mounted.flags | Flags.FLYING, height=105, sequence=2)
    assert trip.step(.6, airborne, east).tap is None
    assert trip.step(1.8, replace(airborne, sequence=3), east).tap == "2"
    assert trip.step(3.9, replace(airborne, sequence=4), east).tap is None
    assert trip.step(4.4, replace(airborne, sequence=5), east).tap == "2"
    climbed = replace(airborne, sequence=6, height=134, sky_flight=SkyFlight(charges=5))
    assert trip.step(5, climbed, east).tap is None
    assert trip.sky_pending_cast is None and trip.sky_ascent_misses == 0
    assert trip.step(6.4, replace(climbed, sequence=7, height=130), east).tap is None
    assert trip.step(7.8, replace(climbed, sequence=8, height=122), east).tap == "2"


def test_early_landing_after_failed_ascent_switches_to_steady_flight_out_of_the_saddle():
    east = 3 * math.pi / 2
    mounted = sample(destination=(.8, .2), sky_pitch_key="0", sky_dismount_key="z", sky_ascent_key="2",
                     flags=Flags.MOUNTED | Flags.SKYRIDING | Flags.KEYS_VERIFIED | Flags.CAN_FLY | Flags.STYLE_SWITCH,
                     flight_mount_active=True, sky_flight=SkyFlight(charges=6, speed=24))
    trip = QuestTrip(mounted.world_size, mounted.destination, "ccw_west")
    assert trip.step(0, mounted, east).tap == "space"
    airborne = replace(mounted, flags=mounted.flags | Flags.FLYING, height=104)
    trip.step(.6, airborne, east)
    assert trip.step(1.7, replace(airborne, sequence=2), east).tap == "2"
    landed = replace(mounted, position=(.35, .2), sequence=3)
    assert trip.step(19, replace(landed, flags=landed.flags | Flags.FALLING), east).tap is None
    assert trip.step(20, landed, east).tap == "z"
    assert trip.step(21, landed, east).tap is None
    unmounted = replace(landed, flags=landed.flags & ~(Flags.MOUNTED | Flags.SKYRIDING), flight_mount_active=False)
    assert trip.step(21.5, unmounted, east).tap == "5"
    assert trip.step(22, unmounted, east).tap is None
    assert trip.step(23, replace(unmounted, flags=unmounted.flags | Flags.CASTING), east).tap is None
    assert trip.step(27, unmounted, east).tap == "6"
    assert trip.step(29, replace(landed, flags=landed.flags & ~Flags.SKYRIDING), east).keys == {"space"}


def test_objective_area_can_be_reached_without_walking_into_its_center():
    start = sample(position=(.291, .2))
    east = 3 * math.pi / 2
    area_trip = QuestTrip(start.world_size, start.destination, "ccw_west", arrival_radius=14)
    exact_trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    assert area_trip.step(0, start, east).outcome == "arrived"
    assert exact_trip.step(0, start, east).keys == {"w"}


def test_ground_route_marks_blockage_before_visual_recovery():
    start = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, start, east).keys == {"w"}
    assert trip.step(3.2, start, east).tap == "space"
    assert trip.step(4, start, east).keys == {"w"}
    assert trip.step(7.2, start, east).tap in {"left", "right"}
    assert trip.step(7.4, start, east).tap == "space"
    assert trip.step(8.2, start, east).keys == {"w"}
    command = trip.step(11.4, start, east)
    assert command.outcome == "route_blocked" and not command.keys
    path = trip.router.route(start.position, start.destination)
    assert path[-1] == start.destination
    assert any(point[1] != start.position[1] for point in path[:-1])
    assert all(trip.router.cell(point) not in trip.router.blocked for point in path)


def test_ground_approach_releases_movement_and_pulses_only_on_fresh_frames():
    start = sample(position=(.294, .2), flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(start.world_size, start.destination, "ccw_west", arrival_radius=2.5)
    east = 3 * math.pi / 2
    turn = trip.step(0, start, east + math.radians(20))
    assert turn.tap == "right" and .03 <= turn.hold_s <= .16 and not turn.keys
    assert trip.step(.1, start, east).tap is None
    move = trip.step(.3, replace(start, sequence=2), east)
    assert move.tap == "w" and 0 < move.hold_s <= .16 and not move.keys
    assert trip.step(.6, replace(start, position=(.298, .2), sequence=3), east).outcome == "arrived"


def test_ground_cruise_pulses_turns_and_waits_for_fresh_feedback():
    start = sample(position=(.2, .2), flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east, south = 3 * math.pi / 2, math.pi
    first = trip.step(0, start, south)  # Facing south, waypoint east: 90 degrees off.
    assert first.tap == "left" and not first.keys and .03 <= first.hold_s <= .16
    assert trip.step(.2, start, south).tap is None  # No fresh frame since the pulse.
    assert trip.step(.3, replace(start, sequence=2), south).tap is None  # Inside the interval.
    assert trip.step(.5, replace(start, sequence=3), south, heading_age=.4).tap is None
    second = trip.step(.6, replace(start, sequence=4), south)
    assert second.tap == "left" and not second.keys
    early_reverse = trip.step(1.1, replace(start, sequence=5), east + math.radians(20))
    assert early_reverse.tap is None and early_reverse.keys == {"w"}
    arc = trip.step(1.6, replace(start, sequence=6), east + math.radians(20))
    assert arc.tap == "right" and arc.keys == {"w"}  # Small deviation keeps moving.
    settled = trip.step(2.1, replace(start, sequence=7), east + math.radians(4))
    assert settled.tap is None and settled.keys == {"w"}


def test_grid_search_never_routes_through_a_blocked_goal_cell():
    router = GridRouter(60., 30.)
    start, goal = (.2, .5), (.8, .5)
    router.blocked.add(router.cell(goal))
    assert not router.route(start, goal)


def test_grid_replanning_prefers_observed_passage_around_the_same_obstacle():
    router = GridRouter(60., 60.)
    def point(x, y):
        return ((x + .5) / 10, (y + .5) / 10)

    start, goal = point(3, 4), point(7, 4)
    router.blocked.add((5, 4))
    router.remember_passage([start, point(4, 5), point(5, 5), point(6, 5), goal])
    assert point(5, 5) in router.route(start, goal)
    router.blocked.add((5, 5))
    assert point(5, 5) not in router.route(start, goal)


def test_v12_loot_receipts_and_blob_mask_share_the_navigation_frame():
    image = np.zeros((29 * 16, 142, 3), np.uint8)
    sequence = 19
    values = [90886, 2451, round(.5*1048575), round(.5*1048575), sequence*8192,
              524288, 10000, 5000, 12 + 2*16 + 128 + 512 + 1024,
              round(.51*1048575), round(.51*1048575), 2451]
    for row, value in enumerate(values):
        spec.render_frame(image, 0, (17+row)*16, spec.encode_frame(4+row, value))
    for row in range(5):
        # First and final corner grid points: scale conversion must use yards on each axis.
        mask = 1 if row == 0 else 32768 if row == 4 else 0
        spec.render_frame(image, 0, row*16, spec.encode_frame(4+row, mask+(sequence%16)*65536))
    spec.render_frame(image, 0, 5*16, spec.encode_frame(9, 255+256+512+sequence*1024))
    for row, value in enumerate((1+sequence*8192, 0, 0, 0)):
        spec.render_frame(image, 0, (6+row)*16, spec.encode_frame(12+row, value))
    spec.render_frame(image, 0, 13*16, spec.encode_frame(3, sequence*64))
    spec.render_frame(image, 0, 14*16, spec.encode_frame(2, 0))
    spec.render_frame(image, 0, 15*16, spec.encode_frame(1, sequence*1024+131072+1))
    spec.render_frame(image, 0, 16*16, spec.encode_frame(0, 0))
    result = decode_travel(image)
    assert result.loot_epoch == 255 and result.sweep_ready
    assert not result.camera_restore_available and not result.camera_zoom_close
    assert len(result.search_area) == 2
    assert math.dist(result.search_area[0], (.38, .26)) < .00001
    assert math.dist(result.search_area[1], (.62, .74)) < .00001
    spec.render_frame(image, 0, 5*16, spec.encode_frame(9, (sequence-1)*1024))
    assert decode_travel(image) is None
    spec.render_frame(image, 0, 5*16, spec.encode_frame(9, 255+256+512+sequence*1024+(7<<17)))
    calibrated = decode_travel(image)
    assert calibrated.camera_restore_available and calibrated.camera_zoom_close and calibrated.camera_epoch == 1
    assert calibrated.search_area == result.search_area
    for row, value in enumerate((1025+sequence*8192, 0, 409*1024+409, 17)):
        spec.render_frame(image, 0, (6+row)*16, spec.encode_frame(12+row, value))
    handoff = decode_travel(image).recovery
    assert handoff.attacker_scan_supported and handoff.attacker_hovered and handoff.attacker_count == 1
    assert handoff.attacker_cursor == (409 / 1023, 409 / 1023) and not handoff.patrol_id
    extended = np.vstack((np.zeros((32, 142, 3), np.uint8), image))
    spec.render_frame(extended, 0, 0, spec.encode_frame(10, 1+5*16+3*256+sequence*8192))
    spec.render_frame(extended, 0, 16, spec.encode_frame(11, 9+3*32+2*256+1*2048+sequence*8192))
    spec.render_frame(extended, 0, 23*16, spec.encode_frame(8, sequence*8192+int(Flags.SKYRIDING)))
    spec.render_frame(extended, 0, 27*16, spec.encode_frame(12, values[8]+1))
    assert decode_travel(extended).sky_flight == SkyFlight("1", "5", "3", 32, 2, "second_wind", 1)
    new = np.vstack((np.zeros((32, 142, 3), np.uint8), extended))
    spec.render_frame(new, 0, 0, spec.encode_frame(sequence % 16, 2 * 1024 + 4))
    spec.render_frame(new, 0, 16, spec.encode_frame(12, sequence * 8192 + 3 * 1024 + 512 + 64 + 2))
    spec.render_frame(new, 0, 29 * 16, spec.encode_frame(12, values[8] + 2))
    result = decode_travel(new)
    assert result.objective_index == 2 and result.objective_progress == (2, 4)
    assert result.suspend_ready and result.current_suspended and result.suspend_epoch == 3
    camera = np.vstack((np.zeros((32, 142, 3), np.uint8), new))
    spec.render_frame(camera, 0, 31 * 16, spec.encode_frame(12, values[8] + 3))
    spec.render_frame(camera, 0, 16, spec.encode_frame(13, 41 + 2 * 128 + 5 * 1024 + sequence * 8192))
    spec.render_frame(camera, 0, 0, spec.encode_frame(14, 7 + 7 * 16 + 3 * 256 + 4096 + sequence * 8192))
    decoded = decode_travel(camera)
    assert decoded.protocol_version == 15 and decoded.camera.zoom == 20
    assert decoded.camera.scene.name == "MELEE" and decoded.camera.applied.name == "MELEE"
    assert decoded.camera.result.name == "DISTANCE_VERIFIED" and decoded.camera.calibrated
    assert decoded.camera.request == 3 and decoded.camera.view == 5
    assert decoded.addon_build is None
    build_image = np.vstack((np.zeros((32, 142, 3), np.uint8), camera))
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357377))
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 263 + sequence * 4096))
    assert decode_travel(build_image).addon_build == 263
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 263 + (sequence - 1) * 4096))
    assert decode_travel(build_image).addon_build is None  # Mixed frames do not claim a loaded build.
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357378))
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 264 + sequence * 4096 + 524288))
    assert decode_travel(build_image).addon_build == 264
    assert not decode_travel(build_image).probe_camera_available  # Old F9 never authorizes F6.
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357379))
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 265 + sequence * 4096 + 524288))
    assert decode_travel(build_image).addon_build == 265
    assert decode_travel(build_image).probe_camera_available
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357380))
    assert not decode_travel(build_image).overhead_camera_available
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357380 + 524288))
    assert decode_travel(build_image).overhead_camera_available
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 267 + sequence * 4096))
    assert not decode_travel(build_image).overhead_camera_available  # Preset without F6 binding.
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 267 + (sequence-1) * 4096 + 524288))
    assert not decode_travel(build_image).overhead_camera_available  # Torn build receipt.
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357381))
    assert decode_travel(build_image).addon_build is None  # Unknown extension keeps the v15 core readable.
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 268+sequence*4096+524288))
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357384+1+2+524288))
    automatic=decode_travel(build_image)
    assert automatic.calibration_orbit_available and automatic.calibration_orbit_active
    assert automatic.overhead_camera_available and automatic.addon_build==268
    assert automatic.extension_schema == 5 and automatic.round_pending_count is None
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357384+524288))
    assert not decode_travel(build_image).calibration_orbit_available
    spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357380+524288))
    assert not decode_travel(build_image).calibration_orbit_available  # Old schema cannot lease mouse input.
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 269+sequence*4096+524288))
    for count in (0, 1, 2, 127):
        spec.render_frame(build_image, 0, 0, spec.encode_frame(15, 357888+count*4+1+2+524288))
        pending = decode_travel(build_image)
        assert pending.round_pending_count == count and pending.extension_schema == 6
        assert pending.calibration_orbit_available and pending.calibration_orbit_active
        assert pending.overhead_camera_available and pending.addon_build == 269
    # A separately versioned optional row can be missing/torn without losing
    # navigation. Only current verified UI receipts may unmask image regions.
    ui_image = np.vstack((np.zeros((16, 142, 3), np.uint8), build_image))
    spec.render_frame(ui_image, 0, 32, spec.encode_frame(14, 274+sequence*4096+524288))
    for hidden in range(16):
        spec.render_frame(ui_image, 0, 0, spec.encode_frame(13, 45*16384+sequence*128+16+hidden))
        current = decode_travel(ui_image)
        assert current.visual_ui_hidden == hidden
        assert replace(current, addon_build=269, visual_ui_hidden=None) == pending
        assert decode_travel(ui_image[16:]).visual_ui_hidden is None  # Older capture crop.
    for kind, payload in ((13, 0), (12, 45*16384+sequence*128+16+15),
                          (13, 44*16384+sequence*128+16+15),
                          (13, 45*16384+sequence*128+32+15),
                          (13, 45*16384+(sequence-1)*128+16+15)):
        spec.render_frame(ui_image, 0, 0, spec.encode_frame(kind, payload))
        decoded = decode_travel(ui_image)
        assert decoded.visual_ui_hidden is None
        assert replace(decoded, addon_build=269) == pending
    spec.render_frame(ui_image, 0, 0, spec.encode_frame(13, 45*16384+sequence*128+16+15))
    spec.render_frame(ui_image, 0, 32, spec.encode_frame(14, 273+sequence*4096+524288))
    assert decode_travel(ui_image).visual_ui_hidden is None
    spec.render_frame(build_image, 0, 16, spec.encode_frame(14, 269+(sequence-1)*4096+524288))
    assert decode_travel(build_image).round_pending_count is None  # Stale receipt is unknown, never zero.
    assert decode_travel(build_image).extension_schema is None
    spec.render_frame(camera, 0, 16, spec.encode_frame(13, sequence * 8192))
    assert decode_travel(camera).camera.zoom is None
    spec.render_frame(camera, 0, 0, spec.encode_frame(14, (sequence - 1) * 8192))
    assert decode_travel(camera) is None
    spec.render_frame(new, 0, 16, spec.encode_frame(12, (sequence - 1) * 8192))
    assert decode_travel(new) is None
    spec.render_frame(extended, 0, 16, spec.encode_frame(11, (sequence-1)*8192))
    assert decode_travel(extended) is None


def test_navigation_channel_decodes_coordinates_flags_and_rejects_corrupt_rows():
    values = [90882, 2451, round(.3 * 1048575), round(.2 * 1048575),
              int(Flags.CAN_FLY | Flags.KEYS_VERIFIED) + 3 * 8192, 525288,
              10000, 5000, 1 + 2 * 16 + 128 + 256 + 512 + 1024 + 2048 + 4096,
              round(.2 * 1048575), round(.2 * 1048575), 2451]
    image = np.zeros((192, 142, 3), dtype=np.uint8)
    for row, value in enumerate(values):
        spec.render_frame(image, 0, row * 16, spec.encode_frame(4 + row, value))
    decoded = decode_travel(image)
    assert decoded.quest_id == 90882 and decoded.phase == QuestState.ACTIVE and decoded.sequence == 3
    assert decoded.height == 100 and decoded.world_size == (1000., 500.)
    assert decoded.flight_mount_active and decoded.interact_verified
    assert math.dist(decoded.destination, (.3, .2)) < .000002
    assert decoded.descent_key == "x"
    for code, key in ((ord("C"), "c"), (8, "f6"), (2, "down"), (0, None), (127, None)):
        spec.render_frame(image, 0, 8 * 16, spec.encode_frame(12, values[8] + 1 + code * 8192))
        decoded = decode_travel(image)
        assert decoded.descent_key == key
        assert bool(decoded.flags & Flags.CAN_FLY) == (key is not None)
    extended = np.zeros((208, 142, 3), dtype=np.uint8)
    extended[16:] = image
    spec.render_frame(extended, 0, 0, spec.encode_frame(0, 2 + 4 * 16 + 12 * 256 + 11 * 4096))
    spec.render_frame(extended, 0, 9 * 16, spec.encode_frame(12, values[8] + 2))
    decoded = decode_travel(extended)
    assert decoded.descent_key is None and decoded.flags & Flags.CAN_FLY
    assert (decoded.sky_ascent_key, decoded.sky_halt_key) == ("2", "4")
    assert (decoded.sky_pitch_key, decoded.sky_dismount_key) == ("f12", "f11")
    spec.render_frame(extended, 0, 0, spec.encode_frame(0, 2 + 4 * 16 + 12 * 256 + 15 * 16384))
    spec.render_frame(extended, 0, 9 * 16, spec.encode_frame(12, values[8] + 3))
    assert decode_travel(extended).sky_dismount_key == "c"
    with_range = np.zeros((224, 142, 3), dtype=np.uint8)
    with_range[16:] = extended
    spec.render_frame(with_range, 0, 10 * 16, spec.encode_frame(12, values[8] + 4))
    range_flags = TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
    spec.render_frame(with_range, 0, 0, spec.encode_frame(1, 3 * 512 + int(range_flags)))
    assert decode_travel(with_range).npc_range_supported
    assert decode_travel(with_range).task == range_flags
    spec.render_frame(with_range, 0, 0, spec.encode_frame(1, 2 * 512 + int(range_flags)))
    assert not decode_travel(with_range).npc_range_supported
    assert not decode_travel(with_range).task
    with_button = np.zeros((240, 142, 3), dtype=np.uint8)
    with_button[16:] = with_range
    spec.render_frame(with_button, 0, 16, spec.encode_frame(1, 3 * 512 + int(range_flags)))
    assert decode_travel(with_button).task == range_flags  # v5 in a v6 capture region.
    spec.render_frame(with_button, 0, 11 * 16, spec.encode_frame(12, values[8] + 5))
    spec.render_frame(with_button, 0, 16, spec.encode_frame(1, 3 * 1024 + int(TaskFlags.MOUNT_TUTORIAL)))
    spec.render_frame(with_button, 0, 0, spec.encode_frame(2, 400 * 1024 + 990))
    assert decode_travel(with_button).mount_button is None  # v6 exposed the macro, not the native icon.
    spec.render_frame(with_button, 0, 11 * 16, spec.encode_frame(12, values[8] + 6))
    spec.render_frame(with_button, 0, 16, spec.encode_frame(1, 2 * 131072 + 3 * 1024 + int(TaskFlags.MOUNT_TUTORIAL)))
    decoded = decode_travel(with_button)
    assert decoded.mount_button == (400 / 1023, 990 / 1023)
    assert decoded.mount_button_key == "2"
    assert decoded.npc_range_supported and decoded.task == TaskFlags.MOUNT_TUTORIAL
    spec.render_frame(with_button, 0, 0, spec.encode_frame(2, 0))
    assert decode_travel(with_button).mount_button is None
    spec.render_frame(with_button, 0, 11 * 16, spec.encode_frame(12, values[8] + 7))
    spec.render_frame(with_button, 0, 16, spec.encode_frame(1, 131072 + 3 * 1024 + int(TaskFlags.PREPARED)))
    spec.render_frame(with_button, 0, 0, spec.encode_frame(2, 765432))
    assert decode_travel(with_button).objective_action == ObjectiveAction.COMBAT
    assert decode_travel(with_button).objective_target == 765432
    spec.render_frame(with_button, 0, 16, spec.encode_frame(1, 6 * 131072 + 3 * 1024 + int(TaskFlags.PREPARED)))
    choice = decode_travel(with_button)
    assert choice.objective_action == ObjectiveAction.CHOICE and choice.choice_step == ChoiceStep.WAIT
    for step in ChoiceStep:
        spec.render_frame(with_button, 0, 0, spec.encode_frame(2, int(step)))
        assert decode_travel(with_button).choice_step == step
    spec.render_frame(with_button, 0, 0, spec.encode_frame(2, 5))
    assert decode_travel(with_button).choice_step == ChoiceStep.WAIT
    spec.render_frame(with_button, 0, 16, spec.encode_frame(1, 2 * 131072 + 3 * 1024 + int(TaskFlags.PREPARED)))
    spec.render_frame(with_button, 0, 0, spec.encode_frame(2, 400 * 1024 + 600))
    assert decode_travel(with_button).objective_cursor == (400 / 1023, 600 / 1023)
    with_round = np.zeros((256, 142, 3), dtype=np.uint8)
    with_round[16:] = with_button
    assert decode_travel(with_round).round_stage == RoundStage.NONE  # Legacy strip in a larger crop.
    spec.render_frame(with_round, 0, 12 * 16, spec.encode_frame(12, values[8] + 8))
    spec.render_frame(with_round, 0, 0, spec.encode_frame(3, 3 * 64 + int(RoundStage.PICKUP) + 32))
    round_sample = decode_travel(with_round)
    assert round_sample.round_stage == RoundStage.PICKUP and round_sample.round_immediate
    assert not round_sample.round_advance and round_sample.objective_cursor == (400 / 1023, 600 / 1023)
    spec.render_frame(with_round, 0, 2 * 16, spec.encode_frame(1, 4 * 131072 + 3 * 1024 + int(TaskFlags.PREPARED)))
    pin_sample = decode_travel(with_round)
    assert pin_sample.objective_pin and pin_sample.objective_action == ObjectiveAction.INTERACT
    assert pin_sample.objective_cursor == round_sample.objective_cursor
    with_minimap = np.zeros((304, 142, 3), dtype=np.uint8)
    with_minimap[48:] = with_round
    spec.render_frame(with_minimap, 0, 0, spec.encode_frame(0, 900 * 1024 + 132))
    spec.render_frame(with_minimap, 0, 16, spec.encode_frame(1, 100 * 1024 + 170))
    spec.render_frame(with_minimap, 0, 32, spec.encode_frame(2, 3 * 2048 + 1024 + 233))
    geometry = decode_travel(with_minimap).minimap
    assert geometry.center == (900 / 1023, 132 / 1023)
    assert geometry.size == (100 / 1023, 170 / 1023)
    assert geometry.radius == 233 and geometry.rotates
    spec.render_frame(with_minimap, 0, 48, spec.encode_frame(3, 3 * 64 + int(RoundStage.OBJECTIVES) + 57344 + 65536))
    telemetry = decode_travel(with_minimap)
    assert telemetry.corpse_lootable and telemetry.fetch_ready and telemetry.precise_object
    spec.render_frame(with_minimap, 0, 48, spec.encode_frame(3, 3 * 64 + 65536 + 131072))
    assert not decode_travel(with_minimap).apr_waypoint  # An exact object pin takes precedence.
    spec.render_frame(with_minimap, 0, 80, spec.encode_frame(1, 2 * 131072 + 3 * 1024 + int(TaskFlags.PREPARED)))
    assert decode_travel(with_minimap).apr_waypoint
    recovery_strip = np.zeros((368, 142, 3), dtype=np.uint8)
    recovery_strip[64:] = with_minimap
    assert decode_travel(recovery_strip).recovery is None  # Existing v9 clients.
    spec.render_frame(recovery_strip, 0, 19 * 16, spec.encode_frame(12, values[8] + 9))
    recovery_rows = (3 * 8192 + 3 + 512 + 1024 + 2048 + 4096, 200 * 1024 + 300, 500 * 1024 + 400, 56250)
    for index, value in enumerate(recovery_rows):
        spec.render_frame(recovery_strip, 0, index * 16, spec.encode_frame(12 + index, value))
    recovery = decode_travel(recovery_strip).recovery
    assert recovery.life == LifeState.GHOST and recovery.dialog and recovery.hovered
    assert recovery.corpse == (200 / 1023, 300 / 1023)
    assert recovery.button == (500 / 1023, 400 / 1023) and recovery.ui_aspect == .5625
    assert recovery.walk_verified is None and recovery.facing is None
    spec.render_frame(recovery_strip, 0, 0, spec.encode_frame(12, recovery_rows[0] - 8192))
    assert decode_travel(recovery_strip) is None
    spec.render_frame(recovery_strip, 0, 19 * 16, spec.encode_frame(12, values[8] + 10))
    spec.render_frame(recovery_strip, 0, 0, spec.encode_frame(12, 3 * 8192 + 3 + 8 + 256 + 512))
    spec.render_frame(recovery_strip, 0, 32, spec.encode_frame(14, 1))  # North is valid.
    recovery = decode_travel(recovery_strip).recovery
    assert recovery.walk_verified and recovery.facing == 0 and recovery.button is None
    assert recovery.corpse_source == CorpseSource.PARENT_MAP
    spec.render_frame(recovery_strip, 0, 0, spec.encode_frame(12, 3 * 8192 + 3 + 256))
    spec.render_frame(recovery_strip, 0, 48, spec.encode_frame(15, 432))
    recovery = decode_travel(recovery_strip).recovery
    assert recovery.corpse is None and recovery.corpse_distance == 43.2 and recovery.ui_aspect == 0
    spec.render_frame(with_minimap, 0, 32, spec.encode_frame(2, 2 * 2048 + 233))
    assert decode_travel(with_minimap).minimap is None
    spec.render_frame(with_round, 0, 0, spec.encode_frame(3, 2 * 64 + int(RoundStage.PICKUP)))
    assert decode_travel(with_round) is None
    extended[:16] = extended[16:32]
    assert decode_travel(extended) is None
    image[16:32] = image[:16]
    assert decode_travel(image) is None


def test_forever_progression_carries_full_node_and_ignores_old_task_row():
    offset = 26
    image = np.zeros(((offset + 12) * 16, 142, 3), dtype=np.uint8)

    def row(index, kind, value):
        spec.render_frame(image, 0, (offset + index) * 16, spec.encode_frame(kind, value))

    base = [783, 1429, 520000, 450000, 3 * 8192 + int(Flags.KEYS_VERIFIED), 524288,
            34708, 23146, 15 + 3 * 16 + 128 + 512 + 1024, 510000, 450000, 1429]
    for index, value in enumerate(base):
        row(index, 4 + index, value)
    for index, kind, value in (
        (-1, 0, 0), (-2, 1, 7 * 131072 + 3 * 1024 + int(TaskFlags.PREPARED)),
        (-3, int(ForeverStep.TALENT_SPENT), 104960), (-4, 3, 3 * 64),
        (-8, 15, 0), (-9, 14, 0), (-10, 13, 0), (-11, 12, 3 * 8192 + 1),
        (-12, 9, 3 * 1024), (-18, 11, 3 * 8192), (-19, 10, 3 * 8192),
        (-20, 12, 3 * 8192), (-21, 3, 0), (-22, 13, 3 * 8192), (-23, 14, 3 * 8192),
    ):
        row(index, kind, value)
    progression = decode_travel(image)
    assert progression.phase == QuestState.READY
    assert progression.forever_step == ForeverStep.TALENT_SPENT and progression.forever_value == 104960
    assert progression.objective_action == ObjectiveAction.NONE
    row(-2, 1, 7 * 131072 + 4 * 1024 + int(TaskFlags.PREPARED))
    assert decode_travel(image).forever_step == ForeverStep.NONE


def test_dropped_skyriding_flag_after_landing_dismounts_instead_of_stopping():
    # 20261118T145120: the gliding-derived flag drops mid-dismount while still
    # mounted next to the turn-in NPC; that must not terminal-stop the flight.
    east = 3 * math.pi / 2
    mounted = sample(destination=(.8, .2), sky_pitch_key="0", sky_dismount_key="z", sky_ascent_key="2",
                     flags=Flags.MOUNTED | Flags.SKYRIDING | Flags.KEYS_VERIFIED | Flags.CAN_FLY | Flags.STYLE_SWITCH,
                     flight_mount_active=True, sky_flight=SkyFlight(charges=6, speed=24))
    trip = QuestTrip(mounted.world_size, mounted.destination, "ccw_west")
    assert trip.step(0, mounted, east).tap == "space"
    airborne = replace(mounted, flags=mounted.flags | Flags.FLYING, height=104)
    trip.step(.6, airborne, east)
    landed = replace(mounted, position=(.79, .2), sequence=2)
    assert trip.step(3, landed, east).tap == "z"  # near target: dismount begins
    flag_dropped = replace(landed, sequence=3, flags=landed.flags & ~Flags.SKYRIDING)
    command = trip.step(3.4, flag_dropped, east)
    assert command.outcome != "skyriding_controls_unavailable" and not command.keys
    unmounted = replace(flag_dropped, flags=flag_dropped.flags & ~Flags.MOUNTED, flight_mount_active=False)
    assert trip.step(4, unmounted, east).outcome is None  # ground travel resumes


def test_hovering_landing_dives_with_forward_near_the_marker():
    # 20261119T012728: pitched down without forward motion the skyriding mount
    # hovered at the marker until the landing budget expired. Near the marker
    # the pitch window must include W to dive into ground contact.
    east = 3 * math.pi / 2
    mounted = sample(destination=(.8, .2), sky_pitch_key="0", sky_dismount_key="z", sky_ascent_key="2",
                     flags=Flags.MOUNTED | Flags.SKYRIDING | Flags.KEYS_VERIFIED | Flags.CAN_FLY | Flags.STYLE_SWITCH,
                     flight_mount_active=True, sky_flight=SkyFlight(charges=6, speed=24))
    trip = QuestTrip(mounted.world_size, mounted.destination, "ccw_west")
    assert trip.step(0, mounted, east).tap == "space"
    airborne = replace(mounted, flags=mounted.flags | Flags.FLYING, height=104)
    trip.step(.6, airborne, east)
    close = replace(airborne, position=(.79, .2), sequence=2)  # ~10 yards out
    trip.step(2, close, east)   # brake window
    command = trip.step(2.5, replace(close, sequence=3), east)  # sky_land begins
    assert "0" in command.keys and "w" in command.keys  # dive-land near the marker
