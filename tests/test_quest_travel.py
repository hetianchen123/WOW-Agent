import math
from dataclasses import replace

import numpy as np

from bot.control.pathfinding import GridRouter
from bot.control.quest_travel import QuestTrip
from bot.perception import channel_spec as spec
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import (
    LifeState, ObjectiveAction, RoundStage, TaskFlags, TravelFlags as Flags, TravelSample, decode_travel,
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
    assert trip.step(7, approach, east).keys == {"f12"}
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
    assert trip.step(.4, airborne, east).keys == {"0"}
    assert not trip.step(.9, airborne, east).keys
    recovery = trip.step(1.9, airborne, east)
    assert recovery.tap == "2" and not recovery.keys
    assert math.isclose(trip.router.distance(start.position, trip.sky_landing_target), 30)
    assert not trip.step(2.2, airborne, east).keys
    retry = replace(airborne, position=trip.sky_landing_target)
    trip.step(2.6, retry, east)
    assert trip.step(3, retry, east).keys == {"0"}
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


def test_objective_area_can_be_reached_without_walking_into_its_center():
    start = sample(position=(.291, .2))
    east = 3 * math.pi / 2
    area_trip = QuestTrip(start.world_size, start.destination, "ccw_west", arrival_radius=14)
    exact_trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    assert area_trip.step(0, start, east).outcome == "arrived"
    assert exact_trip.step(0, start, east).keys == {"w"}


def test_ground_route_steers_in_map_scale_and_replans_after_blockage():
    start = sample(flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(start.world_size, start.destination, "ccw_west")
    east = 3 * math.pi / 2
    assert trip.step(0, start, east).keys == {"w"}
    assert trip.step(3.2, start, east).tap == "space"
    assert trip.step(4, start, east).keys == {"w"}
    assert trip.step(7.2, start, east).tap in {"left", "right"}
    assert trip.step(7.4, start, east).tap == "space"
    assert trip.step(8.2, start, east).keys == {"w"}
    assert trip.step(11.4, start, east).keys == {"s"}
    command = trip.step(12.2, start, east)
    assert trip.path[-1] == start.destination
    assert any(point[1] != start.position[1] for point in trip.path[:-1])
    assert all(trip.router.cell(point) not in trip.router.blocked for point in trip.path)
    assert command.keys & {"left", "right"}


def test_ground_approach_releases_movement_and_pulses_only_on_fresh_frames():
    start = sample(position=(.294, .2), flags=Flags.KEYS_VERIFIED)
    trip = QuestTrip(start.world_size, start.destination, "ccw_west", arrival_radius=2.5)
    east = 3 * math.pi / 2
    turn = trip.step(0, start, east + math.radians(20))
    assert turn.tap == "right" and turn.hold_s == .08 and not turn.keys
    assert trip.step(.1, start, east).tap is None
    move = trip.step(.3, replace(start, sequence=2), east)
    assert move.tap == "w" and 0 < move.hold_s <= .16 and not move.keys
    assert trip.step(.6, replace(start, position=(.298, .2), sequence=3), east).outcome == "arrived"


def test_grid_search_never_routes_through_a_blocked_goal_cell():
    router = GridRouter(60., 30.)
    start, goal = (.2, .5), (.8, .5)
    router.blocked.add(router.cell(goal))
    assert not router.route(start, goal)


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
    spec.render_frame(recovery_strip, 0, 0, spec.encode_frame(12, recovery_rows[0] - 8192))
    assert decode_travel(recovery_strip) is None
    spec.render_frame(with_minimap, 0, 32, spec.encode_frame(2, 2 * 2048 + 233))
    assert decode_travel(with_minimap).minimap is None
    spec.render_frame(with_round, 0, 0, spec.encode_frame(3, 2 * 64 + int(RoundStage.PICKUP)))
    assert decode_travel(with_round) is None
    extended[:16] = extended[16:32]
    assert decode_travel(extended) is None
    image[16:32] = image[:16]
    assert decode_travel(image) is None
