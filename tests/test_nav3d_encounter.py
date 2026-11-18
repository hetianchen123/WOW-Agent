"""Synthetic state/input checks; not live encounter success evidence."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from bot.nav3d.encounter import RespawnEncounter, MESH_ID, HOLD_XY
from bot.perception.nav3d_channel import Nav3DSample
from bot.perception.combat_channel import CombatSample
from bot.perception.observation import ObservationTracker
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import TravelFlags, TaskFlags, LifeState


@pytest.fixture
def encounter():
    events = []
    state = RespawnEncounter(MESH_ID, log_event=lambda *args: events.append(args))
    tracker = ObservationTracker()
    serial = 0
    def tick(now, *, target=False, sequence=None, xyz=None, combat=None, nav=None):
        nonlocal serial
        serial += 1
        seq = serial if sequence is None else sequence
        s = Nav3DSample(seq, 2927, 70961, 2451, HOLD_XY, 0, 0, 90896, 2,
            2927, (2133, -854), True, True, False, 4096,
            True, True, target, target, target)
        s = replace(s, **(xyz or {}))
        c = CombatSample(3, 8, 1, 70961, 253, 80, 1, 123 if target else 999,
            False, True, False, 1, 1, seq, in_range=True if target else None,
            target_health=100, cast_epoch=0)
        c = replace(c, **(combat or {}))
        n = NS(sequence=seq, quest_id=90896, phase=QuestState.ACTIVE, map_id=2451,
            task=TaskFlags.PREPARED, flags=TravelFlags.KEYS_VERIFIED,
            recovery=NS(life=LifeState.ALIVE), objective_target=123 if target else 0,
            objective_progress=(0, 1), objectives_finished=False)
        n.__dict__.update(nav or {})
        q = NS(sequence=seq, quest_id=n.quest_id, state=n.phase, map_id=n.map_id, tracking=True)
        observation = tracker.snapshot(now, q, n, c)
        tick.last = (s, n, observation)
        return state.step(now, s, n, observation)
    return state, tick, events


def test_wait_holds_position_exact_select_then_first_attack_and_cast_receipt(encounter):
    state, tick, events = encounter
    assert tick(0).tap == 'k'
    assert tick(.1).tap is None
    assert tick(.21).tap == 'k'
    assert tick(.42, target=True).tap == '1'
    assert tick(.63, target=True, xyz={'in_combat': True}, combat={'cast_epoch': 1}).tap is None
    assert tick(1.3, target=True, xyz={'in_combat': True}, combat={'cast_epoch': 1}).tap == '1'
    assert any(kind == 'quest_encounter_first_cast_receipt' for kind, _ in events)
    assert not state.terminal


@pytest.mark.parametrize('xyz,combat,nav', [
    ({'encounter_target_matches': False}, {}, {}),
    ({'encounter_target_alive': False}, {}, {}),
    ({'encounter_target_attackable': False}, {}, {}),
    ({}, {'target_dead': True}, {}),
    ({}, {}, {'objective_target': 456}),
    ({}, {'in_range': None}, {}),
    ({}, {'in_range': False}, {}),
    ({}, {'line_of_sight_blocked': True}, {}),
    ({}, {'cast_epoch': None}, {}),
    ({}, {'key_verified': False}, {}),
])
def test_no_attack_without_all_target_receipts(encounter, xyz, combat, nav):
    _, tick, _ = encounter
    command = tick(0, target=True, xyz=xyz, combat=combat, nav=nav)
    assert command.tap in (None, 'k') and not command.keys


def test_no_damage_or_cast_receipt_terminates_fight(encounter):
    _, tick, _ = encounter
    assert tick(0, target=True).tap == '1'
    assert tick(1.7, target=True).tap == '1'
    assert tick(3.4, target=True).tap == '1'
    assert tick(5.1, target=True).outcome == 'quest_encounter_no_cast_or_damage_receipt'


def test_death_waits_for_credit_then_can_wait_for_another_spawn(encounter):
    state, tick, events = encounter
    tick(0, target=True)
    assert tick(.3, target=True, combat={'target_dead': True}).tap is None
    assert tick(4).tap is None
    assert tick(5.4).tap == 'k'
    assert state.deadline == 300
    assert any(k == 'quest_encounter_credit_missing' for k, _ in events)
    assert tick(5.7, target=True, combat={'state_epoch': 2}).tap == '1'
    c = tick(6, xyz={'phase': 3, 'encounter_profile': False}, nav={'phase': QuestState.READY})
    assert c.outcome == 'quest_encounter_objective_complete_ready_for_turn_in'
    assert c.tap is None and not c.keys


def test_stale_packets_cannot_repeat_selection_or_attack(encounter):
    _, tick, _ = encounter
    assert tick(0, sequence=1).tap == 'k'
    assert tick(.3, sequence=1).tap is None
    assert tick(.6, sequence=1).outcome == 'quest_encounter_stale_pose'


def test_independent_stale_combat_never_uses_fresh_navigation_to_attack(encounter):
    _, tick, _ = encounter
    tick(0, combat={'sequence': 1})
    assert tick(1, target=True, combat={'sequence': 1}).tap is None
    assert tick(1.8, target=True, combat={'sequence': 1}).outcome == 'quest_encounter_combat_telemetry_unavailable'


def test_wait_has_shared_budget_and_unknown_range_never_walks(encounter):
    _, tick, _ = encounter
    assert tick(0, target=True, combat={'in_range': False}).tap is None
    c = tick(5.1, target=True, combat={'in_range': False})
    assert c.outcome == 'quest_encounter_approach_position_required'
    assert not c.keys


def test_spawn_wait_budget_does_not_renew(encounter):
    _, tick, _ = encounter
    tick(0)
    assert tick(299).tap == 'k'
    assert tick(300).outcome == 'quest_encounter_wait_budget_exhausted'


@pytest.mark.parametrize('xyz,nav,reason', [
    ({'client_build': 70783}, {}, 'world_or_quest_changed'),
    ({'world_id': 0}, {}, 'world_or_quest_changed'),
    ({'bindings_verified': False}, {}, 'bindings_unverified'),
    ({'encounter_selector_verified': False}, {}, 'selector_unverified_reload_and_prepare'),
    ({'encounter_profile': False}, {}, 'selector_unverified_reload_and_prepare'),
    ({}, {'recovery': NS(life=LifeState.DEAD)}, 'player_not_alive'),
    ({'grounded': False}, {}, 'player_state_unsafe'),
    ({}, {'flags': TravelFlags.FALLING}, 'player_state_unsafe'),
])
def test_changed_state_stops_and_releases(encounter, xyz, nav, reason):
    _, tick, _ = encounter
    tick(0)
    c = tick(.2, xyz=xyz, nav=nav)
    assert c.outcome == 'quest_encounter_' + reason and c.tap is None and not c.keys


def test_casting_pauses_without_treating_combat_as_navigation_failure(encounter):
    _, tick, _ = encounter
    tick(0, target=True)
    assert tick(.3, target=True, xyz={'grounded': False, 'in_combat': True},
        nav={'flags': TravelFlags.CASTING | TravelFlags.IN_COMBAT}).tap is None
    assert tick(.9, target=True, xyz={'in_combat': True}, combat={'cast_epoch': 1}).tap == '1'


def test_unsupported_profile_or_outside_region_leaves_navigation_in_control(encounter):
    state, tick, _ = encounter
    assert tick(0, xyz={'position_xy': (2100, -888)}) is None
    state.enabled = False
    assert tick(.3) is None


def test_quest_elite_allowed_only_when_exact_target_is_verified(encounter):
    _, tick, _ = encounter
    assert tick(0, target=True, combat={'target_danger': 7}).tap == '1'


def test_unrelated_combat_never_switches_or_attacks_bystander(encounter):
    _, tick, _ = encounter
    assert tick(0, xyz={'in_combat': True}).tap is None
    assert tick(3.1, xyz={'in_combat': True}).outcome == 'quest_encounter_combat_target_unconfirmed'


def test_lua_encounter_binding_identity_and_restricted_values():
    lua = pytest.importorskip('lupa.lua51').LuaRuntime(unpack_returned_tuples=True)
    lua.execute('''
        WowAgent={}; C_Map={GetBestMapForUnit=function() return 2451 end}
        function GetLocale() return 'zhCN' end
        function GetBindingAction() return 'CLICK WowAgentTaskTarget:LeftButton' end
        function UnitGUID() return 'Creature-0-1-2-3-123-000ABC' end
        function UnitName() return '罗格洛克' end
        function UnitCanAttack() return true end
        function UnitIsDead() return false end
        button={GetAttribute=function(_,key)
            if key=='type' then return 'macro' end
            if key=='macrotext' then return '/targetexact 罗格洛克' end
        end}
    ''')
    lua.execute(Path('addon/WowAgent/RetailQuestData.lua').read_text())
    lua.execute(Path('addon/WowAgent/QuestEncounter.lua').read_text())
    read = lambda: lua.eval('WowAgent.QuestEncounter.State(90896,2,button)')
    assert read() == (3968, 123)
    lua.execute("function UnitName() return '装甲顺劈者' end")
    assert read() == (2176, 0)
    lua.execute("function UnitName() return '罗格洛克' end; function UnitCanAttack() return false end")
    assert read() == (3456, 0)
    lua.execute('function issecretvalue(v) return v==false end')
    assert read() == (2432, 0)  # unknown dead and attackable never become true
    lua.execute("function GetBindingAction() return 'TARGETNEARESTENEMY' end")
    assert read() == (2304, 0)
    assert lua.eval('WowAgent.QuestEncounter.State(90896,3,button)') == (0, 0)
    assert lua.eval('WowAgent.QuestEncounter.State(90895,2,button)') == (0, 0)


def test_selected_attacker_defense_returns_to_spawn_wait(encounter):
    state, tick, events = encounter
    c = tick(0, xyz={'in_combat': True}, combat={'target_engaged': True, 'in_range': True})
    assert c.tap == '1'
    assert state.state == 'defending'
    assert tick(.3, combat={'state_epoch': 2}).tap == 'k'
    assert tick(.6, target=True, combat={'state_epoch': 3}).tap == '1'


def test_binding_change_is_not_rearmed_by_new_target_epoch(encounter):
    _, tick, _ = encounter
    tick(0, target=True)
    c = tick(.3, target=True, combat={'state_epoch': 2, 'key': 2})
    assert c.outcome == 'quest_encounter_combat_binding_changed'
    assert c.tap is None


def test_new_individual_of_same_species_revalidates_without_old_pending_cast(encounter):
    state, tick, _ = encounter
    assert tick(0, target=True).tap == '1'
    assert tick(.3, target=True, combat={'state_epoch': 2}).tap == '1'
    assert state.fight.state_epoch == 2 and state.fight.presses == 1


def test_manual_runner_hands_off_without_nav_step_and_releases_movement(encounter, tmp_path, monkeypatch):
    from test_npc_obstacle import npc
    state, step, _ = encounter
    run, ctx, _, _, _ = npc(tmp_path, monkeypatch)
    ctx.cfg.nav3d.mode = 'nav3d'
    run.nav3d_coordinator = NS(mesh=NS(mesh_id=MESH_ID),
        step=lambda *a, **kw: pytest.fail('movement must relinquish ownership in holding region'))
    step(0)
    run.nav3d_sample, nav, run.observation = step.last
    run.set_travel_keys(ctx, frozenset({'w'}))
    run.travel_nav3d(ctx, 0, nav)
    assert not ctx.input._held_keys and run.OUTCOME is None
    assert run.nav3d_encounter.state == 'waiting_spawn'
    step(.3, target=True)
    run.nav3d_sample, nav, run.observation = step.last
    run.travel_nav3d(ctx, .3, nav)
    assert run.nav3d_encounter.state == 'fighting' and run.OUTCOME is None
    step(.6, xyz={'phase': 3}, nav={'phase': QuestState.READY})
    run.nav3d_sample, nav, run.observation = step.last
    run.travel_nav3d(ctx, .6, nav)
    assert run.OUTCOME == 'stopped:quest_encounter_objective_complete_ready_for_turn_in'
    assert not ctx.input._held_keys
