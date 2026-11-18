"""Stationary encounter ownership after Nav3D travel; no guessed combat paths.

The XY holding region comes from run 20261118T082900-bcf6e213 and the
user's arrival confirmation. It is NOT a floor/altitude measurement. Starting
here only permits stationary selection; attacking still requires native range
and exact quest-target receipts. No support surface is seeded from this region.
"""
from __future__ import annotations

import math

from ..control.quest_travel import TravelCommand
from ..modules.combat import Fight
from ..perception.quest_channel import QuestState
from ..perception.travel_channel import LifeState, TaskFlags, TravelFlags


QUEST_ID = 90896
MESH_ID = '2141aec34ec36431d1b35d6400609b2d13ecd976063d77fe4f4ded5ed9d27436'
HOLD_XY = (2117.2, -865.4)


class RespawnEncounter:
    def __init__(self, mesh_id, *, wait_budget_s=300, log_event=lambda *_: None):
        if not 0 < wait_budget_s <= 600:
            raise ValueError('invalid_encounter_wait_budget')
        self.wait_budget_s = wait_budget_s
        self.enabled = mesh_id == MESH_ID
        self.log = log_event
        self.active = False
        self.terminal = None
        self.state = None
        self.deadline = None
        self.sequence = None
        self.changed_at = None
        self.select_at = float('-inf')
        self.fight = None
        self.fight_at = None
        self.credit_until = None
        self.blocked = None
        self.blocked_at = None
        self.seen_at = None
        self.first_attack_at = None
        self.cast_confirmed = False
        self.origin = None
        self.defense = None
        self.defense_at = None
        self.binding = None
        self.selected_identity = None
        self.named_seen_at = None
        self.named_seen_epoch = None
        self.latest = None

    def enter(self, state, now, **data):
        if self.state != state:
            self.state = state
            self.log('quest_encounter_state', dict(state=state, at=now, **data))

    def stop(self, reason):
        if not self.terminal:
            self.log('quest_encounter_stop', dict(reason=reason, quest_id=QUEST_ID, last_state=self.latest))
        self.terminal = 'quest_encounter_' + reason
        return TravelCommand(outcome=self.terminal)

    def wait(self, reason, now, *, budget=5):
        if self.blocked != reason:
            self.blocked, self.blocked_at = reason, now
        self.enter(reason, now)
        return self.stop(reason) if now - self.blocked_at >= budget else TravelCommand()

    def defend(self, now, combat, nav):
        """Only an already selected, confirmed attacker; no blind Tab or W."""
        if not combat.target_id or not combat.target_engaged or combat.target_dead:
            return self.wait('combat_target_unconfirmed', now, budget=3)
        if combat.target_danger:
            return self.stop('dangerous_attacker')
        if combat.in_range is not True or combat.line_of_sight_blocked or combat.cast_epoch is None:
            return self.wait('defense_position_unconfirmed', now, budget=3)
        self.blocked = None
        if self.defense_at is None:
            self.defense_at = now
        if now - self.defense_at >= 35:
            return self.stop('defense_timeout')
        if self.defense is None or (combat.target_id, combat.state_epoch) != (self.defense.target_id, self.defense.state_epoch):
            self.defense = Fight(combat.target_id, defense=True, immediate=True)
            self.log('quest_encounter_defense', dict(target_id=combat.target_id, state_epoch=combat.state_epoch))
        self.enter('defending', now)
        status, key = self.defense.step(now, combat, nav)
        if status in {'action', 'turn'}:
            self.binding = (combat.identity, combat.key)
            return TravelCommand(tap=key, hold_s=.06 if status == 'turn' else None)
        return TravelCommand() if status == 'waiting' else self.stop('defense_' + status)

    def step(self, now, sample, nav, observation):
        """None means navigation owns this tick; an empty command releases keys."""
        if self.terminal:
            return TravelCommand(outcome=self.terminal)
        combat = observation.combat.value if observation else None
        if not self.active:
            if not self.enabled or sample is None or nav is None or sample.quest_id != QUEST_ID:
                return None
            in_region = sample.position_xy is not None and math.dist(sample.position_xy, HOLD_XY) <= 3
            in_range = sample.encounter_target_matches and combat is not None and combat.in_range is True
            if not in_region and not in_range:
                return None
            self.active = True
            self.deadline = now + self.wait_budget_s
            self.origin = sample.position_xy
            self.enter('arrived_waiting_spawn', now, position_xy=self.origin,
                       floor_verified=False, source='confirmed_holding_region' if in_region else 'target_range',
                       budget_s=self.wait_budget_s)
        if now >= self.deadline:
            return self.stop('wait_budget_exhausted')
        if sample is None or nav is None or observation is None:
            return self.wait('telemetry_unavailable', now, budget=.75)
        self.latest = dict(at=now, sequence=sample.sequence, position_xy=sample.position_xy,
            grounded=sample.grounded, bindings_verified=sample.bindings_verified,
            in_combat=sample.in_combat, travel_flags=int(nav.flags),
            selector_verified=sample.encounter_selector_verified,
            target_matches=sample.encounter_target_matches,
            target_alive_verified=sample.encounter_target_alive,
            target_attackable_verified=sample.encounter_target_attackable,
            target_id=combat.target_id if combat else None,
            target_epoch=combat.state_epoch if combat else None,
            in_range=combat.in_range if combat else None,
            line_of_sight_blocked=combat.line_of_sight_blocked if combat else None)
        if (sample.sequence != nav.sequence or sample.quest_id != nav.quest_id
                or sample.phase != int(nav.phase) or sample.ui_map_id != nav.map_id):
            return self.wait('incoherent_channels', now, budget=.75)
        if (sample.world_id != 2927 or sample.client_build != 70961 or nav.map_id != 2451
                or nav.quest_id != QUEST_ID or nav.phase not in (QuestState.ACTIVE, QuestState.READY)):
            return self.stop('world_or_quest_changed')
        quest = observation.quest.value
        if (not observation.quest.fresh(now, .75) or not observation.navigation.fresh(now, .75)
                or quest.quest_id != nav.quest_id or quest.state != nav.phase
                or quest.map_id != nav.map_id or not quest.tracking):
            return self.wait('quest_receipt_unavailable', now, budget=.75)
        if self.sequence == sample.sequence:
            return self.stop('stale_pose') if now - self.changed_at > .5 else TravelCommand()
        self.sequence, self.changed_at = sample.sequence, now
        if not sample.bindings_verified or not nav.task & TaskFlags.PREPARED:
            return self.stop('bindings_unverified')
        if not nav.recovery or nav.recovery.life != LifeState.ALIVE:
            return self.stop('player_not_alive')
        if (sample.position_xy is None or self.origin is None
                or math.dist(sample.position_xy, self.origin) > 2):
            return self.stop('holding_position_changed')
        unsafe = TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
        if nav.flags & unsafe or (not sample.grounded and not nav.flags & TravelFlags.CASTING):
            return self.stop('player_state_unsafe')
        if nav.phase == QuestState.READY or (nav.objectives_finished and
                nav.objective_progress[1] > 0 and nav.objective_progress[0] >= nav.objective_progress[1]):
            self.log('quest_encounter_credit', dict(quest_id=QUEST_ID, progress=nav.objective_progress,
                                                  ready_for_turn_in=nav.phase == QuestState.READY))
            return self.stop('objective_complete_ready_for_turn_in')
        if not sample.encounter_profile or not sample.encounter_selector_verified:
            return self.stop('selector_unverified_reload_and_prepare')
        if not observation.combat.fresh(now, .75):
            return self.wait('combat_telemetry_unavailable', now, budget=.75)
        if combat.unsafe or combat.build != sample.client_build:
            return self.stop('combat_state_unsafe')
        if self.binding is not None and self.binding != (combat.identity, combat.key):
            return self.stop('combat_binding_changed')
        if not sample.encounter_target_matches:
            self.named_seen_at = self.named_seen_epoch = None
        elif self.named_seen_at is None or self.named_seen_epoch != combat.state_epoch:
            self.named_seen_at, self.named_seen_epoch = now, combat.state_epoch
            self.log('quest_encounter_target_observed', dict(at=now, target_epoch=combat.state_epoch,
                alive_verified=sample.encounter_target_alive,
                attackable_verified=sample.encounter_target_attackable, actual_spawn_time_known=False))
        if nav.flags & TravelFlags.CASTING:
            return self.wait('casting', now, budget=15)
        # Death is evidence to wait for credit, never credit itself.
        if self.fight and combat.target_id == self.fight.target_id and combat.target_dead:
            self.credit_until = now + 5
            self.log('quest_encounter_target_dead', dict(target_id=combat.target_id,
                                                       state_epoch=combat.state_epoch, credit_confirmed=False))
            self.fight = None
        if self.credit_until is not None:
            if now < self.credit_until:
                self.enter('waiting_quest_credit', now)
                return TravelCommand()
            self.credit_until = None
            self.log('quest_encounter_credit_missing', dict(retry_spawn=True, deadline=self.deadline))
        matched = (sample.encounter_target_matches and sample.encounter_target_alive
                   and sample.encounter_target_attackable and not combat.target_dead
                   and combat.target_id > 0 and combat.target_id == nav.objective_target)
        if not matched:
            self.fight = None
            self.seen_at = self.first_attack_at = None
            self.selected_identity = None
            # Never redirect an ongoing fight to an unverified bystander.
            if sample.in_combat:
                return self.defend(now, combat, nav)
            self.defense, self.defense_at = None, None
            self.blocked = None
            waiting = ('waiting_spawn' if not sample.encounter_target_matches else
                       'waiting_respawn' if combat.target_dead else 'waiting_attackable_receipt')
            self.enter(waiting, now, selected_target=combat.target_id,
                       target_alive=sample.encounter_target_alive,
                       target_attackable=sample.encounter_target_attackable)
            if now - self.select_at >= .2:
                self.select_at = now
                self.log('quest_encounter_select', dict(key='k', target_name='罗格洛克'))
                return TravelCommand(tap='k')
            return TravelCommand()
        identity = (combat.target_id, combat.state_epoch)
        if self.selected_identity != identity:
            self.selected_identity = identity
            self.seen_at = now
            self.log('quest_encounter_target_selected', dict(target_id=combat.target_id,
                state_epoch=combat.state_epoch, in_range=combat.in_range, at=now))
        if combat.in_range is not True:
            # A quest map pin is not the NPC's position. Without a target position
            # and connected Nav3D route, do not fall back to straight-line W.
            return self.wait('range_unknown' if combat.in_range is None else 'approach_position_required', now)
        if combat.line_of_sight_blocked:
            return self.wait('line_of_sight_blocked', now)
        if combat.cast_epoch is None:
            return self.wait('cast_receipt_unavailable', now)
        self.blocked = None
        if self.fight is None or identity != (self.fight.target_id, self.fight.state_epoch):
            self.fight = Fight(combat.target_id, approach=False, immediate=True)
            self.fight_at = now
            self.first_attack_at = None
            self.cast_confirmed = False
        if now - self.fight_at >= 60:
            return self.stop('fight_timeout')
        if self.first_attack_at is not None and not self.cast_confirmed and combat.cast_epoch != self.fight.cast_epoch:
            self.cast_confirmed = True
            self.log('quest_encounter_first_cast_receipt', dict(target_id=combat.target_id,
                input_to_receipt_s=now-self.first_attack_at, selected_to_receipt_s=now-self.seen_at))
        status, key = self.fight.step(now, combat, nav)
        self.enter('fighting', now, target_id=combat.target_id, state_epoch=combat.state_epoch)
        if status in {'action', 'turn'}:
            self.binding = (combat.identity, combat.key)
            if status == 'action' and self.first_attack_at is None:
                self.first_attack_at = now
                self.log('quest_encounter_first_attack', dict(target_id=combat.target_id,
                    selected_to_input_s=now-self.seen_at,
                    observed_to_input_s=now-self.named_seen_at, cast_confirmed=False))
            return TravelCommand(tap=key, hold_s=.06 if status == 'turn' else None)
        if status == 'waiting':
            return TravelCommand()
        return self.stop(status)
