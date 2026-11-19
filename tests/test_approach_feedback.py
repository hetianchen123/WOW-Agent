from dataclasses import replace

import pytest

from bot.control.approach import ApproachAttempt, ApproachGoal, ApproachObservation, ActionState
from bot.control.quest_travel import QuestTrip
from bot.modules.combat import Fight
from bot.perception.observation import ObservationTracker
from bot.perception.travel_channel import TravelFlags, TaskFlags
from test_npc_obstacle import npc
from test_quest_travel import sample
from test_combat import CombatSample


def observation(**kw):
    return replace(ApproachObservation(("npc", 1), 5, False, True, (.2, .2), (1000, 1000)), **kw)


def test_duplicate_frame_cannot_confirm_movement_or_arrival_and_releases_w():
    old = observation()
    attempt = ApproachAttempt(ApproachGoal("npc", old.identity), 0, old)
    assert attempt.poll(.1, old).hold_forward
    alleged_arrival = replace(old, position=(.21, .2), in_range=True)
    assert attempt.poll(.6, alleged_arrival).state == ActionState.WAITING
    assert not attempt.poll(.6, alleged_arrival).hold_forward
    assert attempt.poll(1.6, alleged_arrival).reason == "feedback_stale"


@pytest.mark.parametrize("change,reason", [({"safe": False}, "unsafe_state"),
                                          ({"identity": ("npc", 2)}, "target_changed")])
def test_interrupt_even_with_same_sequence_stops_the_pulse(change, reason):
    old = observation()
    attempt = ApproachAttempt(ApproachGoal("npc", old.identity), 0, old)
    result = attempt.poll(.1, replace(old, **change))
    assert result.reason == reason and not result.hold_forward


def test_new_frame_without_displacement_is_recoverable_not_arrival():
    old = observation()
    attempt = ApproachAttempt(ApproachGoal("npc", old.identity), 0, old)
    assert attempt.poll(.6, replace(old, sequence=6)).reason == "no_displacement"


def test_new_range_feedback_stops_immediately_and_confirms_only_range():
    old = observation()
    attempt = ApproachAttempt(ApproachGoal("npc", old.identity), 0, old)
    result = attempt.poll(.1, replace(old, sequence=6, in_range=True))
    assert result.state == ActionState.CONFIRMED and result.reason == "range_verified"
    assert not result.hold_forward


@pytest.mark.parametrize('change', [dict(position=None),dict(position=(float('nan'),.2)),
                                    dict(world_size=(500,500))])
def test_missing_or_invalid_position_is_not_movement_confirmation(change):
    old=observation(in_range=None)
    result=ApproachAttempt(ApproachGoal('npc',old.identity),0,old).poll(.6,
        replace(old,sequence=6,**change))
    assert result.state==ActionState.RECOVERABLE and result.reason=='movement_evidence_missing'
    assert not result.hold_forward


def test_large_displacement_not_accepted_as_successful_short_step():
    old=observation()
    result=ApproachAttempt(ApproachGoal('npc',old.identity),0,old).poll(.6,
        replace(old,sequence=6,position=(.22,.2)))
    assert result.reason=='displacement_unexpected' and result.state==ActionState.RECOVERABLE


def test_fresh_interaction_range_does_not_require_visible_feet_or_position():
    old=observation(position=None,in_range=None)
    result=ApproachAttempt(ApproachGoal('npc',old.identity),0,old).poll(.1,
        replace(old,sequence=6,in_range=True))
    assert result.reason=='range_verified' and not result.hold_forward


def test_native_npc_range_interrupts_detour_and_interacts_without_coordinates(tmp_path,monkeypatch):
    from types import SimpleNamespace
    run,ctx,_,nav,events=npc(tmp_path,monkeypatch)
    closed=[]
    run.terrain=SimpleNamespace(close=lambda:closed.append(True),step=lambda *_:pytest.fail('old detour ran'))
    run.npc_detouring=True
    run.npc_obstacle_reason='approach_no_displacement'
    run.set_travel_keys(ctx,frozenset({'w'}))
    receipt=replace(nav,position=None,world_size=None,
                    task=nav.task | TaskFlags.NPC_WITHIN_5_YARDS)
    run.interact_quest_target(ctx,1,receipt,None)
    assert closed and run.terrain is None and not run.npc_detouring
    assert not ctx.input._held_keys and ctx.input.count('key_down','f')==1
    assert run.OUTCOME is None  # Open-dialogue request is not quest completion.


def test_turn_in_marker_is_search_area_not_exact_npc_footprint(tmp_path,monkeypatch):
    run,ctx,_,nav,_=npc(tmp_path,monkeypatch)
    nav=replace(nav,position=(.4,.4955),task=TaskFlags.PREPARED)
    run.travel_to_destination(ctx,1,nav,0)
    assert run.arrived and run.trip.arrival_radius==4
    assert not ctx.input._held_keys and not ctx.input.count('key_down','f')
    assert run.OUTCOME is None


def test_new_ready_followup_waits_for_fresh_stable_turn_in_pin(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    run.accepted_from_offer = True
    initial = replace(nav, destination=nav.position, task=TaskFlags.PREPARED)
    run.travel_to_destination(ctx, 1, initial, 0)
    run.travel_to_destination(ctx, 2.1, initial, 0)
    assert not run.arrived and run.trip is None
    updated = replace(initial, destination=(.4, .497), sequence=2)
    run.travel_to_destination(ctx, 2.2, updated, 0)
    run.travel_to_destination(ctx, 3.3, replace(updated, sequence=3), 0)
    assert run.arrived and run.trip.destination == updated.destination
    assert not ctx.input.count("key_down", "f")


def test_independent_channel_updates_do_not_refresh_stale_navigation():
    tracker = ObservationTracker()
    nav = sample()
    tracker.snapshot(1, nav, nav, nav)
    seen = tracker.snapshot(2, replace(nav, sequence=2), nav, replace(nav, sequence=3))
    assert not seen.navigation.fresh(2, .75)
    assert seen.quest.fresh(2, .75) and seen.combat.fresh(2, .75)
    assert not tracker.snapshot(3, None, None, None).navigation.fresh(3, 1)


def test_npc_duplicate_feedback_never_triggers_another_step(tmp_path, monkeypatch):
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    run.interact_quest_target(ctx, 1, nav, 0)
    run.interact_quest_target(ctx, 1.6, nav, 0)
    assert not ctx.input._held_keys and run.terrain is None
    run.interact_quest_target(ctx, 2.6, nav, 0)
    assert run.OUTCOME == "stopped:quest_navigation_channel_stale"
    assert ctx.input.count("key_down", "w") == 1


def test_npc_entering_combat_discards_pending_movement(tmp_path, monkeypatch):
    run, ctx, _, nav, _ = npc(tmp_path, monkeypatch)
    run.interact_quest_target(ctx, 1, nav, 0)
    run.interact_quest_target(ctx, 1.1, replace(nav, flags=nav.flags | TravelFlags.IN_COMBAT), 0)
    assert not ctx.input._held_keys and run.npc_step is None


def test_precision_object_approach_uses_new_position_and_hands_blockage_to_router():
    nav = sample(destination=(.207, .2), flags=TravelFlags.KEYS_VERIFIED)
    trip = QuestTrip(nav.world_size, nav.destination, "ccw_west", walk_only=True, approach_on_foot=True)
    east = 4.71238898038469
    assert trip.step(0, nav, east).tap == "w"
    assert trip.step(.6, nav, east).tap is None
    assert trip.step(.7, replace(nav, sequence=2), east).outcome == "route_blocked"


def test_combat_no_displacement_does_not_keep_issuing_forward():
    target = CombatSample(8, 1, 1, 70783, 63, 80, 87, 12345, False, True, False, 5, 8, 1,
                          facing=0, turn_verified=True, in_range=False)
    nav = sample(flags=TravelFlags.KEYS_VERIFIED)
    fight = Fight(12345, approach=True)
    assert fight.step(0, target, nav)[0] == "waiting"
    assert fight.step(.1, replace(target, sequence=2), nav)[0] == "approach"
    assert fight.step(.8, replace(target, sequence=3), replace(nav, sequence=2))[0] == "approach_blocked"
    assert fight.presses == 0


def test_corpse_approach_waits_for_displacement_and_never_reports_loot(tmp_path, monkeypatch):
    from types import SimpleNamespace
    run, ctx, _, nav, events = npc(tmp_path, monkeypatch)
    fight = Fight(12345)
    fight.presses, fight.state_epoch = 1, 2
    run.hunt = SimpleNamespace(fight=fight, resource=False, credit_deadline=None)
    target = CombatSample(8, 1, 1, 70783, 63, 80, 87, 12345, True, True, False, 5, 2, 1)
    nav = replace(nav, corpse_lootable=True, loot_epoch=1, fetch_ready=False,
                  task=TaskFlags.PREPARED)
    assert run.loot_corpse(ctx, 1, 0, target, nav)
    assert run.loot_corpse(ctx, 1.7, 0, target, nav)
    assert ctx.input.count("key_down", "w") == 1
    assert not run.loot_corpse(ctx, 1.8, 0, target, replace(nav, sequence=2))
    assert run.loot_for_fight is None
    assert not any(name == "quest_loot_confirmed" for name, _ in events)
