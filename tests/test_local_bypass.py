"""Actual command/receipt loop around a solid fence, including opposite-side retry."""
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bot.control.local_bypass import LocalBypass
from bot.control.pathfinding import GridRouter
from bot.control.quest_travel import QuestTrip
from bot.control.approach import ActionResult, ActionState
from bot.control.quest_terrain import TerrainDetour
from bot.perception.travel_channel import TaskFlags, TravelFlags, LifeState, RecoverySample
from test_quest_travel import sample
from test_npc_obstacle import npc
from bot.modules import quest


def fixture():
    nav = sample(position=(.2,.208), destination=(.2,.188), flags=TravelFlags.KEYS_VERIFIED,
        task=TaskFlags.PREPARED|TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN)
    router = GridRouter(*nav.world_size, cell_size=2)
    events = []
    ctx = SimpleNamespace(heading_age=lambda: 0, log_event=lambda n,d: events.append((n,d)))
    bypass = LocalBypass(0, nav, (.2,.2), nav.destination, router, 'ccw_west')
    return bypass, nav, ctx, events


def drive(second_obstacle=False):
    controller, nav, ctx, events = fixture()
    heading, actions, positions = 0, [], [nav.position]
    def blocked(p):
        x,y=controller.router.metric(p)
        return 197<=x<=203 and 99<=y<=101 or second_obstacle and 203<=x<=205 and 102<=y<=106
    assert any(blocked((.2,.188+i*.001)) for i in range(21))  # Straight approach intersects fence.
    for i in range(600):
        at=i*.1
        flags=nav.task
        if controller.router.distance(nav.position,nav.destination)<=4.5:
            flags |= TaskFlags.NPC_WITHIN_5_YARDS
        nav=replace(nav,sequence=(i+1)%128,task=flags)
        command=controller.step(ctx,at,nav,heading)
        if command.outcome:
            return command.outcome,actions,positions,events
        if command.tap in {'left','right'}:
            heading=(heading+(1 if command.tap=='left' else -1)*math.radians(240)*command.hold_s)%(2*math.pi)
        if command.tap=='w':
            distance=command.hold_s*5
            end=(nav.position[0]-math.sin(heading)*distance/controller.router.width,
                 nav.position[1]-math.cos(heading)*distance/controller.router.height)
            # Simulated collision, not a fabricated positive movement receipt.
            if not any(blocked(tuple(a+(b-a)*j/10 for a,b in zip(nav.position,end))) for j in range(1,11)):
                nav=replace(nav,position=end)
                positions.append(end)
        if command.tap: actions.append(command.tap)
    pytest.fail('no bounded outcome')


def test_two_segment_bypass_issues_movement_and_reaches_native_range():
    outcome, actions, positions, events=drive()
    assert outcome=='detoured' and 'w' in actions
    assert max(x for x,y in positions)>.203
    assert any(y<.198 for x,y in positions)
    assert not any(197<=x*1000<=203 and 99<=y*500<=101 for x,y in positions)
    assert events[-1][0]=='quest_bypass_range_confirmed'
    assert next(d for n,d in events if n=='quest_bypass_candidate')['route_verified'] is False


def test_blocked_right_retraces_actual_path_and_tries_left():
    outcome, actions, positions, events=drive(True)
    assert outcome=='detoured'
    assert [d['side'] for n,d in events if n=='quest_bypass_candidate']==['right','left']
    assert any(n=='quest_bypass_step' and d['returning'] for n,d in events)
    assert min(x for x,y in positions)<.197


def test_stale_position_receipt_releases_without_next_pulse():
    ctl,nav,ctx,_=fixture()
    ctl.step(ctx,0,nav,0)
    # Right-side first leg points east.
    heading=3*math.pi/2
    assert ctl.step(ctx,.1,nav,heading).tap=='w'
    result=ctl.step(ctx,1.7,nav,heading)
    assert result.outcome=='probe_telemetry_stale' and not result.keys and not result.tap


def test_unconfirmed_range_does_not_claim_success_at_map_pin():
    ctl,nav,ctx,events=fixture()
    ctl.side,ctl.goals='right',[]
    ctl.step(ctx,1,nav,0)
    assert ctl.returning and not any(n=='quest_bypass_range_confirmed' for n,d in events)


@pytest.mark.parametrize('change', [{'height':90},{'task':TaskFlags.PREPARED}])
def test_bad_height_or_lost_npc_stops_bypass(change):
    ctl,nav,ctx,_=fixture()
    command=ctl.step(ctx,1,replace(nav,**change),0)
    assert command.outcome.startswith('probe_') and not command.tap


@pytest.mark.parametrize('state', [TravelFlags.FALLING,TravelFlags.FLYING,TravelFlags.IN_COMBAT])
def test_terrain_interrupts_bypass_before_any_input(state):
    ctl,nav,ctx,_=fixture()
    trip=QuestTrip(nav.world_size,nav.destination,'ccw_west')
    probe=TerrainDetour(0,nav,0,trip,None)
    probe.bypass,probe.phase=ctl,'bypass'
    command=probe.step(ctx,1,replace(nav,flags=nav.flags|state),0)
    assert command.outcome=='probe_interrupted' and not command.tap


def test_rejoin_precision_collision_enters_retreat_and_bypass_without_model(tmp_path,monkeypatch):
    run,ctx,_,nav,events=npc(tmp_path,monkeypatch)
    nav=replace(nav,position=(.4,.498),sequence=1)
    run.npc_rejoin=True
    run.trip=QuestTrip(nav.world_size,nav.destination,'ccw_west',walk_only=True,arrival_radius=1.5)
    run.travel(ctx,0,nav,0)
    assert ctx.input.count('key_down','w')==1
    run.travel(ctx,.7,replace(nav,sequence=2),0)
    assert run.npc_detouring and run.terrain.allow_clearance and run.terrain.npc_bypass
    run.interact_quest_target(ctx,.8,replace(nav,sequence=3),0)
    assert ctx.input.count('key_down','s')==ctx.input.count('key_up','s')==1
    assert not any(n=='quest_terrain_request' for n,d in events)
    evidence=next(d for n,d in events if n=='quest_npc_visual_probe')
    assert evidence['trigger']=='rejoin_route_blocked' and evidence['contact_confirmed']


def test_reason_string_alone_cannot_invent_contact(tmp_path,monkeypatch):
    run,ctx,_,nav,_=npc(tmp_path,monkeypatch)
    run.begin_npc_detour(ctx,0,nav,'rejoin_route_blocked')
    assert not run.terrain.allow_clearance and not run.terrain.npc_bypass


def test_contact_evidence_survives_short_heading_wait(tmp_path,monkeypatch):
    run,ctx,_,nav,_=npc(tmp_path,monkeypatch)
    ctx.heading_age=lambda:.8
    result=ActionResult(ActionState.RECOVERABLE,'no_displacement',moved_yards=0)
    run.begin_npc_detour(ctx,0,nav,'rejoin_route_blocked',movement_result=result)
    assert run.terrain is None
    ctx.heading_age=lambda:0
    run.begin_npc_detour(ctx,.4,nav,'rejoin_route_blocked')
    assert run.terrain.allow_clearance


def test_full_retreat_to_dogleg_to_native_range_does_not_wait_for_model(monkeypatch):
    from bot.control import quest_terrain
    monkeypatch.setattr(quest_terrain,'SceneRequest',lambda *_a,**_k: pytest.fail('local feedback route waited for model'))
    _,nav,ctx,events=fixture()
    nav=replace(nav,position=(.2,.2022))  # Just before the south face of the fence.
    trip=QuestTrip(nav.world_size,nav.destination,'ccw_west')
    probe=TerrainDetour(0,nav,0,trip,None,contact_release=True,npc_bypass=True)
    heading,actions=0,[]
    for i in range(600):
        task=nav.task
        if trip.router.distance(nav.position,nav.destination)<=4.5:
            task |= TaskFlags.NPC_WITHIN_5_YARDS
        nav=replace(nav,task=task,sequence=(i+1)%128)
        command=probe.step(ctx,i*.1,nav,heading)
        if command.outcome:
            assert command.outcome=='detoured'
            break
        if command.tap in {'left','right'}:
            heading=(heading+(1 if command.tap=='left' else -1)*math.radians(240)*command.hold_s)%(2*math.pi)
        if command.tap in {'w','s'}:
            length=command.hold_s*(5 if command.tap=='w' else -5)
            end=(nav.position[0]-math.sin(heading)*length/trip.router.width,
                 nav.position[1]-math.cos(heading)*length/trip.router.height)
            blocked=any(197<=x*1000<=203 and 99<=y*500<=101 for x,y in
                [tuple(a+(b-a)*j/10 for a,b in zip(nav.position,end)) for j in range(1,11)])
            if not blocked: nav=replace(nav,position=end)
        if command.tap: actions.append(command.tap)
    else:
        pytest.fail('bounded route never completed')
    assert actions[0]=='s' and actions.index('w')>actions.index('s')
    assert probe.clearance_total>=4 and any(n=='quest_bypass_range_confirmed' for n,d in events)
    assert not any(n=='quest_terrain_request' for n,d in events)


def test_old_contact_evidence_cannot_survive_wait_or_relocation(tmp_path,monkeypatch):
    run,ctx,_,nav,_=npc(tmp_path,monkeypatch)
    ctx.heading_age=lambda: .8
    run.begin_npc_detour(ctx,0,nav,'rejoin_route_blocked',
        movement_result=ActionResult(ActionState.RECOVERABLE,'no_displacement',moved_yards=0))
    ctx.heading_age=lambda: 0
    run.begin_npc_detour(ctx,3,nav,'rejoin_route_blocked')
    assert not run.terrain.allow_clearance
