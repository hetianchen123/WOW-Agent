"""Execute a validated same-map quest plan with live dialogue confirmation."""
from __future__ import annotations

import logging
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..control.navigation import Route, RouteFollower, heading_error
from ..control.pathfinding import GridRouter
from ..control.quest_travel import QuestTrip
from ..core.statemachine import State
from ..decision.startlux import DECISION_WAIT_S
from ..perception.capture import CaptureError, frontmost_game_bounds
from ..perception.channel_spec import REGION_H, REGION_W, StatusFlags
from ..perception.combat_channel import combat_region, decode_combat
from ..perception.cv import minimap_quest_dots, pumpkin_probe_points
from ..perception.quest_channel import Dialogue, Objective, QuestKind, QuestState, decode_quest, quest_region
from ..perception.travel_channel import ObjectiveAction, RoundStage, TaskFlags, TravelFlags, decode_travel, travel_region
from .base import Module
from .combat import QuestDefense, QuestHunt, tap_combat_key

STORY_KINDS = frozenset({QuestKind.CAMPAIGN, QuestKind.MAINLINE, QuestKind.SIDE})
UNSAFE_FLAGS = StatusFlags.DEAD | StatusFlags.GHOST | StatusFlags.FLYING
PICKUP_DIALOGUE = {Dialogue.OFFER, Dialogue.DETAIL}
TURN_IN_DIALOGUE = {Dialogue.TURN_IN, Dialogue.PROGRESS, Dialogue.REWARD}
QUEST_TARGETS = {90882: frozenset({244669, 244670, 244671, 244672})}
ARRIVAL_QUESTS = frozenset({90883})
PREPARED_FOCUS_WAIT_S = 45
OBJECT_PROBES = tuple(sorted(
    ((x / 100, y / 100) for y in range(36, 77, 8) for x in range(28, 73, 5)),
    key=lambda point: math.dist(point, (.5, .60)),
))
NPC_SCAN_ANGLES = tuple(math.radians(degrees) for degrees in (0, 30, -30, 65, -65, 120, -120, 180))
OBJECT_APPROACH_YARDS = 2.5
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuestStep:
    quest_id: int
    dialog_key: str
    pickup: Route | None
    turn_in: Route | None
    objective: Route | None
    objective_key: str | None
    combat_targets: frozenset[int] = frozenset()


def load_quest_plan(path: Path) -> tuple[int, list[QuestStep]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("quest plan must be a mapping")
    map_id, quests = raw.get("map_id"), raw.get("quests")
    if type(map_id) is not int or not 1 <= map_id <= 1048575:
        raise ValueError("map_id must be a positive 20-bit integer")
    if not isinstance(quests, list) or not 1 <= len(quests) <= 8:
        raise ValueError("plan must contain 1 to 8 quests")

    def route(points, name):
        if not isinstance(points, list) or not points or any(
            not isinstance(p, list) or len(p) != 2
            or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in p)
            for p in points
        ):
            raise ValueError(f"{name} must contain normalized [x, y] waypoints")
        return Route.from_dict({
            "name": name, "ui_map_id": map_id, "mode": "ground",
            "points": points, "arrival_radius": .0015,
        })

    def key(value, name):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-z]|f(?:[1-9]|1[0-2])", value):
            raise ValueError(f"{name} must be a single game key")
        if value in {"w", "a", "s", "d"}:
            raise ValueError(f"{name} conflicts with movement")
        return value

    result, ids, dialog_keys = [], set(), set()
    for item in quests:
        if not isinstance(item, dict):
            raise ValueError("quest entries must be mappings")
        quest_id = item.get("id")
        if type(quest_id) is not int or not 1 <= quest_id <= 1048575 or quest_id in ids:
            raise ValueError("quest IDs must be distinct positive 20-bit integers")
        dialog_key = key(item.get("dialog_key"), f"{quest_id}.dialog_key")
        if dialog_key in dialog_keys or dialog_key == "f":
            raise ValueError("dialog keys must be distinct and differ from the interact key f")
        objective = item.get("objective")
        objective_key = item.get("objective_key")
        if objective_key is not None and objective is None:
            raise ValueError(f"{quest_id}.objective_key requires an objective route")
        targets = item.get("combat_targets", [])
        if (not isinstance(targets, list)
                or any(type(target) is not int or not 1 <= target <= 1048575 for target in targets)
                or len(targets) != len(set(targets))):
            raise ValueError(f"{quest_id}.combat_targets must contain distinct positive 20-bit NPC IDs")
        if targets and (objective is None or objective_key is not None):
            raise ValueError("combat_targets requires an objective route and excludes objective_key")
        result.append(QuestStep(
            quest_id, dialog_key, route(item.get("pickup"), f"{quest_id}.pickup"),
            route(item.get("turn_in"), f"{quest_id}.turn_in"),
            route(objective, f"{quest_id}.objective") if objective is not None else None,
            key(objective_key, f"{quest_id}.objective_key") if objective_key is not None else None,
            frozenset(targets),
        ))
        ids.add(quest_id)
        dialog_keys.add(dialog_key)
    if any(step.objective_key in dialog_keys for step in result):
        raise ValueError("objective keys must differ from dialogue keys")
    return map_id, result


class RunQuests(State):
    def __init__(self, map_id: int, steps: list[QuestStep], kinds: set[QuestKind],
                 planner=None, *, accept_only=False, auto_route=False, travel_only=False, require_prepared=False):
        self.map_id, self.steps, self.kinds = map_id, steps, kinds
        self.planner, self.accept_only = planner, accept_only
        self.auto_route, self.travel_only = auto_route, travel_only
        self.require_prepared = require_prepared
        self.trip = None
        self.travel_keys: frozenset[str] = frozenset()
        self.nav_sequence, self.nav_at = None, 0.0
        self.search_waypoint, self.searches = None, 0
        self.planned_phase = None
        self.index = 0
        self.forward = False
        self.had_focus = False
        self.reported_focus_wait = False
        self.execution_started = False
        self.focus_wait_started: float | None = None
        self.last_sequence = None
        self.last_frame = self.last_target = time.monotonic()
        self.sample = None
        self.phase = None
        self.follower = None
        self.arrived = False
        self.npc_press_at: float | None = None
        self.npc_range_wait_since: float | None = None
        self.npc_step: tuple[float, tuple[float, float]] | None = None
        self.npc_steps = 0
        self.npc_scan_started: float | None = None
        self.npc_scan_aligned_at: float | None = None
        self.npc_scan_direction = 0
        self.npc_scan_heading = 0.0
        self.npc_select_attempted = False
        self.pickup_dialogue_retries = 0
        self.pickup_dialogue_recovery_at: float | None = None
        self.mount_clicked_at: float | None = None
        self.mount_key_attempted = self.mount_style_attempted = False
        self.last_action = self.last_progress = 0.0
        self.unusable_since = None
        self.phase_started = 0.0
        self.progress = (0, 0)
        self.action_signature = None
        self.attempts = 0
        self.followup_started: float | None = None
        self.followup_id: int | None = None
        self.round_stage = RoundStage.NONE
        self.round_advance_at: float | None = None
        self.round_completed_ids: set[int] = set()
        self.accepted_from_offer = False
        self.hunt: QuestHunt | None = None
        self.defense: QuestDefense | None = None
        self.loot_for_fight = None
        self.fetch_for_fight = None
        self.fetch_attempt: tuple[float, int] | None = None
        self.object_probe = 0
        self.object_probe_at = 0.0
        self.object_probe_sequence: int | None = None
        self.object_points: tuple[tuple[float, float], ...] | None = None
        self.object_target: tuple[float, float] | None = None
        self.object_targets_tried: list[tuple[float, float]] = []
        self.object_minimap_checked = False
        self.object_approach_yards = OBJECT_APPROACH_YARDS
        self.object_click_at: float | None = None
        self.scanning_objects = False
        self.quest_keys = {key for step in steps for key in (step.dialog_key, step.objective_key) if key}
        self.OUTCOME = None

    def on_enter(self, ctx):
        self.last_frame = self.last_target = time.monotonic()

    def stop(self, ctx, reason):
        ctx.input.release_all()
        self.OUTCOME = f"stopped:{reason}"

    def set_forward(self, ctx, enabled):
        if self.auto_route and not enabled:
            self.set_travel_keys(ctx, frozenset())
            return
        if self.forward != enabled:
            (ctx.input.down if enabled else ctx.input.up)("w")
            self.forward = enabled

    def set_travel_keys(self, ctx, keys):
        for key in sorted(self.travel_keys - keys):
            ctx.input.up(key)
        for key in sorted(keys - self.travel_keys):
            ctx.input.down(key)
        self.travel_keys = keys

    def travel(self, ctx, now, navigation, heading):
        destination = self.object_target or self.search_waypoint or navigation.destination
        local_search = self.search_waypoint is not None
        live_object = bool(self.object_target or navigation.objective_pin and not local_search)
        if self.trip is None or self.trip.router.distance(self.trip.destination, destination) > (4 if live_object else 8):
            # A kill objective is an area marker. Start checking targets before
            # walking into whatever occupies the exact center of that area.
            if self.phase == QuestState.READY and not self.travel_only:
                radius = 1.5  # Reach the NPC, not just the area around the map pin.
            elif live_object and not self.travel_only:
                radius = self.object_approach_yards
            else:
                radius = 14 if self.hunt is not None and self.search_waypoint is None else 4
            self.trip = QuestTrip(navigation.world_size, destination, ctx.cfg.navigation.facing_convention,
                                  arrival_radius=radius, walk_only=live_object or local_search)
            source = ("object_minimap" if self.object_target else
                      "object_pin" if live_object else "local_search" if local_search else "quest_waypoint")
            ctx.log_event("quest_route", {"id": navigation.quest_id, "map": navigation.map_id,
                                          "destination": destination, "arrival_radius": radius,
                                          "source": source})
        previous = self.trip.mode, self.trip.phase
        command = self.trip.step(now, navigation, heading)
        if previous != (self.trip.mode, self.trip.phase):
            ctx.log_event("quest_travel", {"id": navigation.quest_id, "mode": self.trip.mode,
                                           "phase": self.trip.phase})
        if command.outcome:
            if command.outcome == "route_blocked" and self.object_target:
                self.set_travel_keys(ctx, frozenset())
                ctx.log_event("quest_object_approach_blocked", {"id": navigation.quest_id,
                                                               "destination": self.object_target})
                self.finish_object_approach()
                self.arrived = True
                return
            if command.outcome != "arrived":
                return self.stop(ctx, "quest_" + command.outcome)
            self.set_travel_keys(ctx, frozenset())
            self.arrived, self.last_progress = True, now
            ctx.log_event("quest_destination_reached", {"id": navigation.quest_id, "position": navigation.position})
            if live_object:
                ctx.log_event("quest_object_approach_reached", {"id": navigation.quest_id,
                                                               "distance_yards": round(self.trip.router.distance(
                                                                   navigation.position, destination), 2)})
            if self.travel_only:
                ctx.stats["destinations_reached"] = 1
                self.OUTCOME = "completed"
            return
        self.set_travel_keys(ctx, command.keys)
        if command.tap:
            ctx.input.tap(command.tap)

    def turn(self, ctx, error):
        cfg = ctx.cfg.input
        if abs(error) > math.radians(cfg.turn_deadband_deg):
            pixels = int(max(-cfg.mouse_turn_max_px, min(error * cfg.mouse_turn_gain, cfg.mouse_turn_max_px)))
            ctx.input.rmb_turn(-pixels if cfg.invert_turn else pixels)

    def walk(self, ctx, now, pos, heading):
        decision = self.follower.update(now, pos, heading, self.forward)
        if decision.finished or decision.stuck_event or decision.recovery:
            self.set_forward(ctx, False)
            if decision.finished and decision.finish_reason == "route_complete":
                self.arrived = True
                self.last_progress = now
            else:
                self.stop(ctx, "quest_route_blocked")
            return
        if decision.turn_err is not None:
            self.turn(ctx, decision.turn_err)
        self.set_forward(ctx, decision.move_forward)

    def tick(self, ctx):
        now = time.monotonic()
        if ctx.input.name != "mock":
            if not game_is_frontmost():
                if self.require_prepared and not self.execution_started:
                    if self.focus_wait_started is None:
                        self.focus_wait_started = now
                        log.info("首个操作尚未开始。等待魔兽回到前台，最长 %s 秒。", PREPARED_FOCUS_WAIT_S)
                    if now - self.focus_wait_started >= PREPARED_FOCUS_WAIT_S:
                        return self.stop(ctx, "game_not_frontmost")
                    return
                focus_wait = PREPARED_FOCUS_WAIT_S if self.require_prepared else 12
                if self.had_focus or now - self.last_frame > focus_wait:
                    return self.stop(ctx, "game_not_frontmost")
                if not self.reported_focus_wait:
                    self.reported_focus_wait = True
                    log.info("等待魔兽回到前台，最长 %s 秒。", focus_wait)
                self.last_target = now
                return
            if self.focus_wait_started is not None:
                self.focus_wait_started = None
                self.phase, self.sample = None, None
                self.last_sequence = self.nav_sequence = None
                self.last_frame = self.last_target = now
                log.info("魔兽已回到前台，重新读取任务状态。")
            if not self.had_focus:
                self.had_focus = True
                self.last_frame = self.last_target = now
        step = self.steps[self.index]
        channel = ctx.cfg.channel
        frame = decode_quest(ctx.capture.grab(*quest_region(channel.x, channel.y)))
        if frame is None:
            self.set_forward(ctx, False)
            if now - self.last_frame > 3:
                return self.stop(ctx, "quest_channel_lost")
            return
        navigation = decode_travel(ctx.capture.grab(*travel_region(channel.x, channel.y))) if self.auto_route else None
        if self.require_prepared and not self.travel_only and navigation and navigation.round_stage != RoundStage.NONE:
            if frame.quest_id != navigation.quest_id or frame.state != navigation.phase:
                return self.wait_for_data(ctx, now, "quest_navigation_channel_unavailable")
            if navigation.map_id != self.map_id or frame.map_id != self.map_id or not frame.tracking:
                return self.stop(ctx, "quest_wrong_map")
            if navigation.quest_id != step.quest_id or navigation.round_stage != self.round_stage:
                if self.round_stage != RoundStage.NONE and self.round_advance_at is None:
                    return self.stop(ctx, "quest_round_unexpected_switch")
                if (self.round_stage == RoundStage.TURN_IN and navigation.round_stage in {RoundStage.PICKUP, RoundStage.DONE}
                    or self.round_stage == RoundStage.PICKUP and navigation.round_stage == RoundStage.DONE
                    and self.round_completed_ids):
                    ctx.stats["quest_rounds_done"] = ctx.stats.get("quest_rounds_done", 0) + 1
                    self.round_completed_ids.clear()
                if navigation.quest_id != step.quest_id:
                    step = QuestStep(navigation.quest_id, "9", None, None, None, None,
                                     QUEST_TARGETS.get(navigation.quest_id, frozenset()))
                    self.steps[self.index] = step
                    self.accepted_from_offer = False
                self.round_stage, self.round_advance_at = navigation.round_stage, None
                self.phase, self.sample, self.last_sequence, self.nav_sequence = None, None, None, None
                self.last_target = now
                self.unusable_since = None
                ctx.log_event("quest_round_stage", {"stage": self.round_stage.name, "id": step.quest_id})
            if navigation.sequence != self.nav_sequence:
                self.nav_sequence, self.nav_at = navigation.sequence, now
            elif now - self.nav_at > 1.2:
                return self.stop(ctx, "quest_navigation_channel_stale")
        elif self.round_stage != RoundStage.NONE:
            return self.wait_for_data(ctx, now, "quest_navigation_channel_unavailable")
        if frame.sequence != self.last_sequence:
            self.last_sequence, self.last_frame = frame.sequence, now
            if self.followup_id is not None and frame.tracking and frame.quest_id == self.followup_id:
                self.steps.append(QuestStep(frame.quest_id, "9", None, None, None, None,
                                            QUEST_TARGETS.get(frame.quest_id, frozenset())))
                self.index += 1
                self.followup_id = self.followup_started = None
                self.phase, self.accepted_from_offer, self.nav_sequence = None, False, None
                step = self.steps[self.index]
            if frame.quest_id == step.quest_id:
                self.sample, self.last_target = frame, now
        if now - self.last_frame > 2 or now - self.last_target > 3:
            return self.stop(ctx, "quest_channel_stale")
        if not frame.tracking:
            return self.stop(ctx, "quest_plan_not_tracked")
        if frame.map_id != self.map_id:
            return self.stop(ctx, "quest_wrong_map")
        status, pos, heading = ctx.channel.status(max_age_s=1.5), ctx.pos(), ctx.heading()
        unsafe = UNSAFE_FLAGS & ~StatusFlags.FLYING if self.auto_route else UNSAFE_FLAGS
        if status and status[0] & unsafe:
            return self.stop(ctx, "quest_unsafe_character_state")
        if status is None or pos is None or heading is None or self.sample is None:
            self.set_forward(ctx, False)
            return
        sample = self.sample
        if self.accept_only and sample.state in {QuestState.ACTIVE, QuestState.READY}:
            self.set_forward(ctx, False)
            ctx.stats["quests_accepted"] = ctx.stats.get("quests_accepted", 0) + 1
            ctx.log_event("quest_accept_confirmed", {"id": step.quest_id, "state": sample.state.name})
            self.OUTCOME = "completed"
            return
        if (self.phase == QuestState.PENDING and sample.state in {QuestState.ACTIVE, QuestState.READY}
                and not self.accepted_from_offer):
            self.accepted_from_offer = True
            ctx.stats["quests_accepted"] = ctx.stats.get("quests_accepted", 0) + 1
            ctx.log_event("quest_accept_confirmed", {"id": step.quest_id, "state": sample.state.name})
        in_combat = bool(status[0] & StatusFlags.IN_COMBAT
                         or navigation and navigation.flags & TravelFlags.IN_COMBAT)
        if (sample.state == QuestState.READY and self.hunt and navigation and not self.travel_only
                and navigation.quest_id == step.quest_id and navigation.phase == sample.state
                and frame.dialogue == Dialogue.CLOSED and not in_combat):
            combat = decode_combat(ctx.capture.grab(*combat_region(channel.x, channel.y)))
            if self.loot_corpse(ctx, now, sample.progress[0], combat, navigation):
                return
        if in_combat and self.require_prepared and self.auto_route and not self.travel_only:
            if not (navigation and navigation.quest_id == step.quest_id and navigation.phase == sample.state
                    and navigation.task & TaskFlags.PREPARED):
                return self.wait_for_data(ctx, now, "quest_defense_preparation_unavailable")
            if navigation.sequence != self.nav_sequence:
                self.nav_sequence, self.nav_at = navigation.sequence, now
            if now - self.nav_at > 1.2:
                return self.stop(ctx, "quest_navigation_channel_stale")
            combat = decode_combat(ctx.capture.grab(*combat_region(channel.x, channel.y)))
            fight = self.hunt and self.hunt.fight
            continuing_hunt = (self.defense is None and self.arrived and self.phase == sample.state == QuestState.ACTIVE
                               and frame.dialogue == Dialogue.CLOSED and combat and fight
                               and combat.target_id == fight.target_id
                               and fight.state_epoch in {None, combat.state_epoch})
            if not continuing_hunt:
                return self.defend(ctx, now, navigation, frame.dialogue, combat)
        elif self.defense is not None:
            ctx.log_event("quest_defense_resumed", {"id": step.quest_id})
            self.phase_started += now - self.defense.started_at
            self.defense, self.unusable_since = None, None
            self.npc_step = self.npc_press_at = self.object_probe_sequence = None
            self.object_probe, self.object_points = 0, None
            if not self.arrived:
                self.trip = None
        if self.round_stage != RoundStage.NONE:
            if self.round_stage == RoundStage.DONE:
                self.set_forward(ctx, False)
                self.OUTCOME = "completed"
                return
            if (self.round_stage == RoundStage.TURN_IN or navigation.round_immediate) and sample.state == QuestState.TURNED_IN:
                if step.quest_id not in self.round_completed_ids:
                    self.round_completed_ids.add(step.quest_id)
                    ctx.stats["quests_done"] = ctx.stats.get("quests_done", 0) + 1
                    ctx.log_event("quest_done", {"id": step.quest_id, "kind": sample.kind.name})
            expected = {RoundStage.PICKUP: QuestState.PENDING, RoundStage.OBJECTIVES: QuestState.ACTIVE,
                        RoundStage.TURN_IN: QuestState.READY}[self.round_stage]
            if self.round_stage == RoundStage.PICKUP and navigation.round_immediate:
                expected = QuestState.READY
            if sample.state != expected or navigation.round_blocked:
                return self.advance_round(ctx, now, navigation, status[0])
            if (navigation.round_immediate and frame.dialogue != Dialogue.CLOSED
                    and frame.dialogue_id != step.quest_id and not status[0] & StatusFlags.IN_COMBAT):
                self.set_forward(ctx, False)
                signature = (QuestState.READY, Dialogue.BLOCKED)
                if self.action_signature != signature:
                    self.action_signature, self.attempts = signature, 0
                return self.act(ctx, step.dialog_key, now)
        if frame.dialogue == Dialogue.REWARD_CHOICE and frame.dialogue_id == step.quest_id:
            return self.stop(ctx, "quest_reward_choice_required")
        if sample.state == QuestState.TURNED_IN:
            self.execution_started = True
            return self.finish_or_follow(ctx, now, step, frame, sample)
        if sample.kind == QuestKind.UNKNOWN:
            return self.wait_for_data(ctx, now, "quest_classification_unknown")
        if sample.kind not in self.kinds:
            return self.stop(ctx, f"quest_kind_{sample.kind.name.lower()}")
        route = {QuestState.PENDING: step.pickup, QuestState.ACTIVE: step.objective,
                 QuestState.READY: step.turn_in}.get(sample.state)
        mount_tutorial = False
        objective_action = ObjectiveAction.NONE
        if self.auto_route:
            if not navigation or navigation.quest_id != step.quest_id or navigation.phase != sample.state:
                return self.wait_for_data(ctx, now, "quest_navigation_channel_unavailable")
            if navigation.sequence != self.nav_sequence:
                self.nav_sequence, self.nav_at = navigation.sequence, now
            if now - self.nav_at > 1.2:
                return self.stop(ctx, "quest_navigation_channel_stale")
            if self.require_prepared and not navigation.task & TaskFlags.PREPARED:
                return self.stop(ctx, "quest_automatic_preparation_lost")
            mount_tutorial = (
                step.quest_id == 90883 and sample.state == QuestState.ACTIVE and not self.travel_only
                and (sample.progress == (0, 1) or bool(navigation.task & TaskFlags.MOUNT_TUTORIAL))
            )
            if self.require_prepared and sample.state == QuestState.PENDING and frame.dialogue == Dialogue.CLOSED:
                if self.accepted_from_offer or (self.phase == QuestState.PENDING and self.action_signature in {
                    (QuestState.PENDING, Dialogue.OFFER), (QuestState.PENDING, Dialogue.DETAIL)
                }):
                    return self.wait_for_data(ctx, now, "quest_accept_not_confirmed")
                if navigation.destination is None:
                    return self.stop(ctx, "quest_offer_not_open")
            if navigation.map_id != self.map_id or (frame.dialogue == Dialogue.CLOSED
                    and not mount_tutorial and navigation.destination_map and navigation.destination_map != self.map_id):
                return self.stop(ctx, "quest_cross_map_route_required")
            objective_action = navigation.objective_action
            if (sample.state == QuestState.ACTIVE and not self.travel_only and not step.combat_targets
                    and step.quest_id not in ARRIVAL_QUESTS and not navigation.task & TaskFlags.USE_ITEM
                    and objective_action == ObjectiveAction.NONE):
                return self.wait_for_data(ctx, now, "quest_objective_profile_required")
        hunting = sample.state == QuestState.ACTIVE and not self.travel_only and (
            bool(step.combat_targets) or (objective_action == ObjectiveAction.COMBAT and not self.scanning_objects)
            or (self.phase == QuestState.ACTIVE and self.hunt is not None and objective_action != ObjectiveAction.INTERACT)
        )
        if in_combat:
            if self.hunt is not None and sample.state == QuestState.READY:
                return self.wait_for_data(ctx, now, "quest_combat_did_not_end")
            if not (hunting and self.phase == QuestState.ACTIVE and self.arrived and frame.dialogue == Dialogue.CLOSED):
                return self.stop(ctx, "quest_unsafe_character_state")
        if (navigation and not mount_tutorial and (not self.arrived or self.phase != sample.state)
                and frame.dialogue == Dialogue.CLOSED):
            if navigation.destination is None or navigation.position is None or navigation.world_size is None:
                return self.wait_for_data(ctx, now, "quest_destination_unavailable")
            if not navigation.flags & TravelFlags.KEYS_VERIFIED:
                return self.wait_for_data(ctx, now, "quest_navigation_bindings_unverified")
        if (not self.auto_route and route is None) or (
            sample.state == QuestState.ACTIVE and sample.objective == Objective.UNKNOWN and not self.travel_only
            and step.quest_id not in ARRIVAL_QUESTS
        ):
            return self.wait_for_data(ctx, now, "quest_objective_unsupported")
        if hunting and sample.objective != Objective.COUNTED:
            return self.wait_for_data(ctx, now, "quest_combat_needs_counted_objective")
        if hunting and sample.progress[0] >= sample.progress[1] > 0:
            return self.wait_for_data(ctx, now, "quest_ready_state_unconfirmed")
        self.unusable_since = None
        if sample.state != self.phase:
            if self.object_click_at is not None and sample.state == QuestState.READY:
                ctx.log_event("quest_object_progress", {"id": step.quest_id, "progress": sample.progress})
            self.phase = sample.state
            self.follower = RouteFollower(route, ctx.cfg.navigation, loop=False) if route else None
            self.trip, self.search_waypoint, self.searches = None, None, 0
            self.loot_for_fight = None
            self.fetch_for_fight, self.fetch_attempt = None, None
            self.arrived, self.attempts, self.action_signature = False, 0, None
            self.npc_press_at, self.npc_step, self.npc_steps = None, None, 0
            self.npc_range_wait_since = None
            self.npc_scan_started = self.npc_scan_aligned_at = None
            self.npc_scan_direction = 0
            self.npc_select_attempted = False
            self.pickup_dialogue_retries, self.pickup_dialogue_recovery_at = 0, None
            self.mount_clicked_at = None
            self.mount_key_attempted = self.mount_style_attempted = False
            self.phase_started = self.last_progress = now
            self.planned_phase = None
            self.progress = sample.progress
            self.hunt = QuestHunt(step.combat_targets) if hunting else None
            self.object_probe, self.object_probe_sequence = 0, None
            self.object_points = None
            self.object_target, self.object_targets_tried = None, []
            self.object_minimap_checked = False
            self.object_approach_yards = OBJECT_APPROACH_YARDS
            self.object_click_at = None
            self.scanning_objects = False
            ctx.log_event("quest_phase", {"id": step.quest_id, "kind": sample.kind.name, "state": self.phase.name})
        if hunting and self.hunt is None:
            self.hunt = QuestHunt(step.combat_targets)
        elif not hunting:
            self.hunt = None
        if now - self.phase_started > (600 if self.auto_route else 150):
            return self.stop(ctx, "quest_phase_timeout")

        if self.planner is not None and self.planned_phase != self.phase:
            self.set_forward(ctx, False)
            if now - self.phase_started > DECISION_WAIT_S:
                return self.stop(ctx, "quest_startlux_timeout")
            labels = {
                QuestState.PENDING: ("pick_up", "按计划路线接取当前任务"),
                QuestState.ACTIVE: ("do_objective", "按计划路线完成当前任务目标"),
                QuestState.READY: ("turn_in", "按计划路线交付当前任务"),
            }
            action, label = labels[self.phase]
            if mount_tutorial:
                label = "点击已核对的飞行坐骑按钮并等待任务确认"
            signature = (self.map_id, step.quest_id, self.phase, self.phase_started, sample.kind, sample.progress, in_combat,
                         frame.dialogue, frame.dialogue_id, navigation.destination if navigation else None)
            state = {
                "quest_id": step.quest_id, "quest_state": self.phase.name, "quest_kind": sample.kind.name,
                "is_on_quest": self.phase != QuestState.PENDING,
                "ready_for_turn_in": self.phase == QuestState.READY,
                "progress": list(sample.progress), "map_id": frame.map_id, "in_combat": in_combat,
                "dialogue": frame.dialogue.name, "dialogue_id": frame.dialogue_id,
                "dialogue_matches": frame.dialogue in {Dialogue.CLOSED, Dialogue.OFFER, Dialogue.DETAIL,
                                                      Dialogue.TURN_IN, Dialogue.PROGRESS, Dialogue.REWARD}
                    and frame.dialogue_id in {0, step.quest_id},
                "plan_is_allowlisted": True, "observation_age_ms": round((now - self.last_target) * 1000),
            }
            if navigation:
                state["destination"] = navigation.destination
                state["travel_priority"] = ["fly", "mount", "walk"]
                if objective_action != ObjectiveAction.NONE:
                    state["objective_action"] = objective_action.name
            try:
                result = self.planner.poll(signature, state, {action: label, "wait": "等待并停止本次执行"})
            except TimeoutError as exc:
                ctx.log_event("quest_decision_error", {"error": str(exc)})
                return self.stop(ctx, "quest_startlux_timeout")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                ctx.log_event("quest_decision_error", {"error": str(exc)})
                return self.stop(ctx, "quest_startlux_unavailable")
            if result is None:
                return
            ctx.log_event("quest_decision", {"id": step.quest_id, "state": self.phase.name, **result})
            if result["recommendation"] != action:
                return self.stop(ctx, "quest_startlux_" + (result.get("reason") or "wait"))
            self.planned_phase = self.phase

        self.execution_started = True
        if frame.dialogue != Dialogue.CLOSED:
            self.set_forward(ctx, False)
            if frame.quest_id != step.quest_id:
                return
            allowed = PICKUP_DIALOGUE if self.phase == QuestState.PENDING else TURN_IN_DIALOGUE
            if self.phase == QuestState.ACTIVE or frame.dialogue_id != step.quest_id or frame.dialogue not in allowed:
                if (self.phase == QuestState.PENDING and navigation and not in_combat
                        and navigation.task & TaskFlags.PREPARED and self.npc_press_at is not None):
                    if self.pickup_dialogue_recovery_at is not None:
                        if now - self.pickup_dialogue_recovery_at < 3:
                            return
                    elif self.pickup_dialogue_retries < 2 and now - self.npc_press_at < 5:
                        self.pickup_dialogue_retries += 1
                        self.pickup_dialogue_recovery_at = self.last_action = now
                        ctx.log_event("quest_pickup_dialogue_recovery", {
                            "id": step.quest_id, "dialogue_id": frame.dialogue_id, "dialogue": frame.dialogue.name,
                        })
                        ctx.input.tap(step.dialog_key)
                        return
                return self.stop(ctx, "quest_dialogue_unexpected")
            self.pickup_dialogue_recovery_at = None
            signature = (self.phase, frame.dialogue)
            if signature != self.action_signature:
                self.action_signature, self.attempts = signature, 0
            return self.act(ctx, step.dialog_key, now)
        if self.pickup_dialogue_recovery_at is not None:
            self.pickup_dialogue_recovery_at = self.npc_press_at = self.npc_step = None
            self.npc_range_wait_since = self.npc_scan_started = self.npc_scan_aligned_at = None
            self.npc_steps = self.npc_scan_direction = 0
            self.npc_select_attempted = False
        if mount_tutorial:
            self.set_forward(ctx, False)
            if frame.quest_id != step.quest_id:
                return
            return self.click_tutorial_mount(ctx, now, navigation, sample)
        if objective_action == ObjectiveAction.INTERACT and not self.travel_only:
            if self.object_click_at is not None and sample.progress[0] > self.progress[0]:
                self.object_click_at = None
                self.search_waypoint, self.searches = None, 0
                self.finish_object_approach()
                self.progress, self.last_progress = sample.progress, now
                ctx.log_event("quest_object_progress", {"id": step.quest_id, "progress": sample.progress})
            if (navigation.objective_pin and navigation.destination and self.arrived and self.trip
                    and self.object_click_at is None and self.object_target is None
                    and self.trip.router.distance(self.trip.destination, navigation.destination) > 1):
                self.search_waypoint, self.trip, self.arrived = None, None, False
                self.object_probe, self.object_probe_sequence = 0, None
                self.object_points = None
                self.object_approach_yards = OBJECT_APPROACH_YARDS
        if not self.arrived:
            if navigation is not None:
                grounded = not navigation.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING)
                if (self.phase in {QuestState.PENDING, QuestState.READY} and not self.travel_only and grounded
                        and navigation.task & (TaskFlags.NPC_CANDIDATE | TaskFlags.NPC_SELECTED)):
                    self.set_forward(ctx, False)
                    self.arrived, self.last_progress = True, now
                else:
                    return self.travel(ctx, now, navigation, heading)
            else:
                return self.walk(ctx, now, pos, heading)
        if frame.quest_id != step.quest_id:
            self.set_forward(ctx, False)
            return
        if self.phase == QuestState.ACTIVE:
            if self.hunt is not None:
                return self.hunt_tick(ctx, now, sample.progress[0], in_combat, navigation)
            if sample.progress[0] > self.progress[0]:
                self.progress, self.last_progress, self.attempts = sample.progress, now, 0
            if navigation and navigation.task & TaskFlags.USE_ITEM:
                if navigation.flags & (TravelFlags.CASTING | TravelFlags.MOUNTED):
                    return
                return self.act(ctx, "j", now)
            if objective_action == ObjectiveAction.INTERACT or self.scanning_objects:
                return self.search_object(ctx, now, navigation, heading)
            if step.objective_key is None:
                if now - self.last_progress > 10:
                    return self.stop(ctx, "quest_objective_no_progress")
                return
            return self.act(ctx, step.objective_key, now)
        if self.action_signature != (self.phase, Dialogue.CLOSED):
            self.action_signature, self.attempts = (self.phase, Dialogue.CLOSED), 0
        if navigation and not navigation.interact_verified:
            return self.stop(ctx, "quest_interact_binding_unverified")
        if navigation:
            if not navigation.task & TaskFlags.PREPARED:
                return self.stop(ctx, "quest_automatic_not_prepared")
            if self.npc_step is not None or navigation.task & TaskFlags.NPC_SELECTED:
                return self.interact_with_npc(ctx, now, navigation)
            if self.npc_press_at is not None:
                self.set_travel_keys(ctx, frozenset())
                if now - self.npc_press_at > 2:
                    return self.stop(ctx, "quest_turn_in_npc_lost")
                return
            if navigation.task & TaskFlags.NPC_CANDIDATE and self.attempts < 3:
                self.set_travel_keys(ctx, frozenset())
                if now - self.last_action >= 2:
                    self.npc_select_attempted = True
                return self.act(ctx, "k", now)
            return self.search_quest_npc(ctx, now, navigation, heading)
        return self.act(ctx, "f", now)

    def advance_round(self, ctx, now, navigation, status):
        self.set_forward(ctx, False)
        if navigation.round_blocked:
            return self.stop(ctx, "quest_round_state_changed")
        if not navigation.task & TaskFlags.PREPARED:
            return self.stop(ctx, "quest_automatic_preparation_lost")
        if self.round_advance_at is not None or not navigation.round_advance:
            return self.wait_for_data(ctx, now, "quest_round_advance_timeout")
        if status & StatusFlags.IN_COMBAT or navigation.flags & (TravelFlags.IN_COMBAT | TravelFlags.CASTING):
            return self.wait_for_data(ctx, now, "quest_combat_did_not_end")
        self.execution_started = True
        self.round_advance_at = now
        self.unusable_since = None
        ctx.input.tap(self.steps[self.index].dialog_key)

    def defend(self, ctx, now, navigation, dialogue, combat):
        self.set_forward(ctx, False)
        if self.defense is None:
            self.defense = QuestDefense(now)
            self.hunt = None
            ctx.log_event("quest_defense_started", {"id": navigation.quest_id, "phase": navigation.phase.name})
        blocked = (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
                   | TravelFlags.CASTING)
        if navigation.flags & blocked or dialogue != Dialogue.CLOSED:
            combat = None
        if combat and str(combat.key) in self.quest_keys:
            return self.stop(ctx, "quest_combat_action_key_conflicts")
        status, key = self.defense.step(now, combat)
        if key:
            self.execution_started = True
            event = "quest_defense_turn" if status == "turn" else "quest_defense_action"
            ctx.log_event(event, {"id": navigation.quest_id, "key": key, "target": combat.target_id})
            tap_combat_key(ctx.input, status, key)
        elif status != "waiting":
            return self.stop(ctx, "quest_defense_" + status)

    def click_tutorial_mount(self, ctx, now, navigation, sample):
        flags = navigation.flags
        if sample.progress != (0, 1):
            return
        if self.mount_clicked_at is not None:
            elapsed = now - self.mount_clicked_at
            if elapsed > 12:
                return self.stop(ctx, "quest_mount_not_confirmed")
            if flags & (TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.CASTING | TravelFlags.SWIMMING):
                return
            style_keys = TravelFlags.KEYS_VERIFIED | TravelFlags.STYLE_SWITCH
            if flags & TravelFlags.MOUNTED:
                if elapsed > 3 and not flags & TravelFlags.SKYRIDING and not self.mount_style_attempted \
                        and flags & style_keys == style_keys:
                    ctx.input.tap("5")
                    self.mount_style_attempted = True
                    self.mount_key_attempted = False
                    self.mount_clicked_at = now
                    ctx.log_event("quest_tutorial_mount_switch_style", {"id": sample.quest_id})
            elif elapsed > 4 and navigation.mount_button_key and not self.mount_key_attempted:
                ctx.input.tap(navigation.mount_button_key)
                self.mount_key_attempted = True
                self.mount_clicked_at = now
                ctx.log_event("quest_tutorial_mount_key", {"id": sample.quest_id, "key": navigation.mount_button_key})
            return
        if now - self.phase_started > DECISION_WAIT_S + 10:
            return self.stop(ctx, "quest_mount_button_unavailable")
        busy = TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.CASTING | TravelFlags.SWIMMING
        if navigation.mount_button is None or flags & busy:
            return
        try:
            x, y, width, height = frontmost_game_bounds()
        except ValueError:
            return
        channel = ctx.cfg.channel
        if not (x <= channel.x < x + width and y <= channel.y < y + height):
            return
        u, v = navigation.mount_button
        point = round(x + width * u), round(y + height * v)
        if not (x < point[0] < x + width and y < point[1] < y + height):
            return
        ctx.input.move_to(*point, jitter_px=0)
        ctx.input.click()
        self.mount_clicked_at = now
        ctx.log_event("quest_tutorial_mount_click", {"id": sample.quest_id, "position": point})

    def finish_or_follow(self, ctx, now, step, frame, sample):
        self.set_forward(ctx, False)
        if self.followup_started is None:
            self.followup_started = now
            ctx.stats["quests_done"] = ctx.stats.get("quests_done", 0) + 1
            ctx.log_event("quest_done", {"id": step.quest_id, "kind": sample.kind.name})
        if self.require_prepared and not self.travel_only and self.index + 1 == len(self.steps):
            if now - self.followup_started < 4:
                candidate = frame.dialogue_id
                if (self.followup_id is None and candidate and candidate != step.quest_id
                        and frame.dialogue in {Dialogue.DETAIL, Dialogue.OFFER, Dialogue.CLOSED}
                        and now - self.last_action > .5):
                    channel = ctx.cfg.channel
                    navigation = decode_travel(ctx.capture.grab(*travel_region(channel.x, channel.y)))
                    if (navigation and navigation.quest_id == step.quest_id and navigation.phase == QuestState.TURNED_IN
                            and navigation.task & TaskFlags.PREPARED):
                        self.followup_id = candidate
                        ctx.log_event("quest_followup_requested", {"from": step.quest_id, "id": candidate})
                        ctx.input.tap(step.dialog_key)
                return
            if self.followup_id is not None:
                return self.stop(ctx, "quest_followup_not_confirmed")
        self.index += 1
        self.followup_started = None
        self.sample, self.phase, self.last_target, self.hunt = None, None, now, None
        self.unusable_since = None
        if self.index == len(self.steps):
            self.OUTCOME = "completed"

    def interact_with_npc(self, ctx, now, navigation):
        selected = bool(navigation.task & TaskFlags.NPC_SELECTED)
        range_known = bool(navigation.task & TaskFlags.NPC_RANGE_KNOWN)
        within_range = range_known and bool(navigation.task & TaskFlags.NPC_WITHIN_5_YARDS)
        unsafe = TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
        if navigation.flags & unsafe or not navigation.flags & TravelFlags.KEYS_VERIFIED:
            return self.stop(ctx, "quest_turn_in_approach_unsafe")
        if not navigation.npc_range_supported:
            return self.stop(ctx, "quest_turn_in_range_addon_required")
        if navigation.position is None or navigation.world_size is None:
            return self.stop(ctx, "quest_interact_position_unavailable")
        if self.npc_step is not None:
            started, origin = self.npc_step
            elapsed = now - started
            if selected and elapsed < .24:
                return
            self.set_travel_keys(ctx, frozenset())
            # Allow a new position sample to arrive after releasing W.
            if elapsed < .55:
                return
            if not selected:
                return self.stop(ctx, "quest_turn_in_npc_lost")
            moved = GridRouter(*navigation.world_size).distance(origin, navigation.position)
            ctx.log_event("quest_turn_in_approach_result", {"id": navigation.quest_id,
                                                           "moved_yards": round(moved, 2)})
            if moved < .15 and not within_range:
                return self.stop(ctx, "quest_turn_in_approach_blocked")
            self.npc_step, self.npc_press_at = None, None
        self.set_travel_keys(ctx, frozenset())
        if navigation.flags & TravelFlags.CASTING:
            return
        if not range_known and self.npc_steps == 0 and self.npc_press_at is None:
            if self.npc_range_wait_since is None:
                self.npc_range_wait_since = now
            if now - self.npc_range_wait_since < 1:
                return
        if within_range and self.npc_press_at is None:
            self.npc_press_at = self.last_action = now
            ctx.log_event("quest_action", {"id": navigation.quest_id, "key": "f"})
            ctx.input.tap("f")
        elif self.npc_press_at is None or now - self.npc_press_at >= 1.2:
            if self.npc_steps >= 3:
                if not range_known:
                    return self.stop(ctx, "quest_turn_in_range_unknown")
                if not within_range:
                    return self.stop(ctx, "quest_turn_in_outside_five_yards")
                return self.stop(ctx, "quest_turn_in_approach_exhausted")
            self.npc_steps += 1
            self.npc_step = now, navigation.position
            ctx.log_event("quest_turn_in_approach", {
                "id": navigation.quest_id, "step": self.npc_steps,
                "range_known": range_known, "within_five_yards": within_range,
            })
            self.set_travel_keys(ctx, frozenset({"w"}))

    def search_quest_npc(self, ctx, now, navigation, heading):
        if not (navigation.flags & TravelFlags.KEYS_VERIFIED and navigation.position
                and navigation.destination and navigation.world_size):
            return self.stop(ctx, "quest_interact_position_unavailable")
        unsafe = TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
        if navigation.flags & unsafe:
            return self.stop(ctx, "quest_turn_in_approach_unsafe")
        if self.npc_scan_started is None:
            self.npc_scan_started, self.npc_scan_heading = now, heading
            ctx.log_event("quest_npc_search", {"id": navigation.quest_id, "phase": self.phase.name})
        if now - self.npc_scan_started > 18 or self.npc_scan_direction >= len(NPC_SCAN_ANGLES):
            if self.phase == QuestState.PENDING:
                reason = ("quest_pickup_npc_selection_unconfirmed" if self.npc_select_attempted
                          else "quest_pickup_npc_not_found")
            else:
                reason = "quest_turn_in_npc_not_in_interact_range"
            return self.stop(ctx, reason)
        if now - self.last_action < .45:
            self.set_travel_keys(ctx, frozenset())
            return
        if self.trip is None:
            self.trip = QuestTrip(navigation.world_size, navigation.destination, ctx.cfg.navigation.facing_convention)
        base = self.npc_scan_heading
        if self.trip.router.distance(navigation.position, navigation.destination) > .5:
            base = heading + self.trip.turn_error(navigation.position, navigation.destination, heading)
        target = base + NPC_SCAN_ANGLES[self.npc_scan_direction]
        turn = self.trip.turn_keys(heading_error(heading, target), math.radians(12))
        self.set_travel_keys(ctx, turn)
        if turn:
            self.npc_scan_aligned_at = None
        elif self.npc_scan_aligned_at is None:
            self.npc_scan_aligned_at = now
        elif now - self.npc_scan_aligned_at >= .45:
            self.npc_scan_direction += 1
            self.npc_scan_aligned_at = None
            self.attempts = 0

    def finish_object_approach(self):
        if self.object_target is not None:
            self.object_targets_tried.append(self.object_target)
            self.object_target = None
        self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None
        self.object_minimap_checked = False
        self.object_approach_yards = OBJECT_APPROACH_YARDS

    def object_minimap_target(self, ctx, navigation, bounds, heading, near=None):
        minimap = navigation.minimap
        if minimap is None or navigation.position is None or navigation.world_size is None:
            return
        x, y, width, height = bounds
        cx, cy = minimap.center
        mw, mh = minimap.size
        if not (0 < cx - mw / 2 < cx + mw / 2 < 1 and 0 < cy - mh / 2 < cy + mh / 2 < 1):
            return
        rect = (round(x + (cx - mw / 2) * width), round(y + (cy - mh / 2) * height),
                round(mw * width), round(mh * height))
        try:
            dots = minimap_quest_dots(ctx.capture.grab(*rect))
        except CaptureError:
            return
        theta = heading if minimap.rotates else 0
        if ctx.cfg.navigation.facing_convention == "cw_east":
            theta = -theta
        cosine, sine = math.cos(theta), math.sin(theta)
        px, py = navigation.position
        map_width, map_height = navigation.world_size
        router = GridRouter(map_width, map_height)
        candidates = []
        for dx, dy in dots:
            east = (dx * cosine + dy * sine) * minimap.radius
            south = (-dx * sine + dy * cosine) * minimap.radius
            distance = math.hypot(east, south)
            point = (px + east / map_width, py + south / map_height)
            if (distance <= 85 and all(0 < v < 1 for v in point)
                    and (near is None or router.distance(point, near) <= 8)
                    and all(router.distance(point, tried) > 6 for tried in self.object_targets_tried)):
                candidates.append((distance, point))
        if candidates:
            distance, point = min(candidates)
            ctx.log_event("quest_object_minimap_target", {"id": navigation.quest_id, "destination": point,
                                                         "distance_yards": round(distance, 2)})
            return point

    def search_object(self, ctx, now, navigation, heading):
        self.set_forward(ctx, False)
        busy = (TravelFlags.CASTING | TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING
                | TravelFlags.SWIMMING | TravelFlags.IN_COMBAT)
        if navigation.flags & busy or now - self.last_action < 2:
            return
        if self.object_click_at is not None:
            if now - self.object_click_at < 5:
                return
        if self.object_probe_sequence is not None and (
            navigation.sequence == self.object_probe_sequence or now - self.object_probe_at < .25
        ):
            return
        try:
            bounds = frontmost_game_bounds()
        except ValueError:
            return
        x, y, width, height = bounds
        channel = ctx.cfg.channel
        if not (x <= channel.x < x + width and y <= channel.y < y + height):
            return
        pumpkin = navigation.quest_id == 90885
        precise = navigation.precise_object or navigation.objective_pin or pumpkin
        if self.object_click_at is not None:
            ctx.log_event("quest_object_progress_unconfirmed", {"id": navigation.quest_id})
            self.object_click_at = None
            if precise and self.object_approach_yards == OBJECT_APPROACH_YARDS:
                if self.object_target is not None:
                    updated = self.object_minimap_target(ctx, navigation, bounds, heading, near=self.object_target)
                    self.object_target = updated or self.object_target
                target = self.object_target or (navigation.destination if navigation.objective_pin else None)
                if target and navigation.position and navigation.world_size:
                    distance = GridRouter(*navigation.world_size).distance(navigation.position, target)
                    self.object_approach_yards = 1.25
                    if distance > self.object_approach_yards:
                        self.search_waypoint, self.trip, self.arrived = None, None, False
                        self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None
                        ctx.log_event("quest_object_approach_refine", {"id": navigation.quest_id,
                                                                       "distance_yards": round(distance, 2)})
                        return
        if precise and not navigation.objective_pin and not self.object_minimap_checked:
            self.object_minimap_checked = True
            self.object_target = self.object_minimap_target(ctx, navigation, bounds, heading)
            if self.object_target is not None:
                self.search_waypoint, self.trip, self.arrived = None, None, False
                self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None
                return
            ctx.log_event("quest_object_minimap_no_target", {"id": navigation.quest_id,
                                                            "calibrated": navigation.minimap is not None})
            if not self.search_quest_area(ctx, navigation):
                return self.stop(ctx, "quest_object_not_found")
            self.finish_object_approach()
            return
        if self.object_points is None:
            hints = ()
            if pumpkin:
                # Avoid the native HUD and the strips painted by the plugin.
                exclusions = [(.415, .665, .59, .78), (.32, .79, .69, 1), (.79, 0, 1, 1),
                              (0, .39, .29, .61)]
                strips = [(channel.x, channel.y, REGION_W, REGION_H),
                          quest_region(channel.x, channel.y), combat_region(channel.x, channel.y),
                          travel_region(channel.x, channel.y)]
                exclusions.extend(((sx - x) / width, (sy - y) / height, (sx + sw - x) / width,
                                   (sy + sh - y) / height) for sx, sy, sw, sh in strips)
                try:
                    hints = pumpkin_probe_points(ctx.capture.grab(x, y, width, height), exclusions)
                except CaptureError:
                    log.warning("南瓜画面采样失败，继续使用鼠标网格。")
                ctx.log_event("quest_object_visual_candidates", {"id": navigation.quest_id, "points": hints})
            self.object_points = hints + OBJECT_PROBES
        cursor = navigation.objective_cursor
        if self.object_probe_sequence is not None and cursor:
            expected = self.object_points[self.object_probe - 1]
            # Require the game to report the object at the probe we just sent.
            target = self.object_target or (navigation.destination if navigation.objective_pin else None)
            nearby = target and navigation.position and navigation.world_size and GridRouter(
                *navigation.world_size).distance(navigation.position, target) <= self.object_approach_yards + .5
            if math.dist(cursor, expected) <= .012 and (not precise or nearby):
                ctx.input.click(button="right")
                self.object_click_at = self.last_action = now
                self.object_probe_sequence = None
                ctx.log_event("quest_object_interact", {"id": navigation.quest_id, "cursor": cursor})
                return
        if self.object_probe >= len(self.object_points):
            if self.object_target is not None:
                self.finish_object_approach()
                return
            if not self.search_quest_area(ctx, navigation):
                return self.stop(ctx, "quest_object_not_found")
            self.object_probe, self.object_probe_sequence = 0, None
            self.object_points = None
            self.object_minimap_checked = False
            self.scanning_objects = False
            return
        u, v = self.object_points[self.object_probe]
        ctx.input.move_to(round(x + u * width), round(y + v * height), jitter_px=0)
        self.object_probe += 1
        self.object_probe_at, self.object_probe_sequence = now, navigation.sequence

    def search_quest_area(self, ctx, navigation):
        if not (navigation and navigation.destination and navigation.world_size and self.searches < 8):
            return False
        width, height = navigation.world_size
        x, y = navigation.destination
        angle = self.searches * math.pi / 4
        self.searches += 1
        waypoint = x + 22 * math.cos(angle) / width, y + 22 * math.sin(angle) / height
        if not all(0 <= coordinate <= 1 for coordinate in waypoint):
            return False
        self.search_waypoint, self.trip, self.arrived = waypoint, None, False
        ctx.log_event("quest_search_nearby", {"waypoint": waypoint, "attempt": self.searches})
        return True

    def loot_corpse(self, ctx, now, progress, sample, navigation):
        fight = self.hunt.fight
        if not (fight and fight.presses and navigation.task & TaskFlags.PREPARED):
            return False
        same_corpse = (sample and not sample.unsafe and sample.target_dead and sample.target_id == fight.target_id
                       and sample.state_epoch == fight.state_epoch)
        if self.fetch_for_fight is fight and self.fetch_attempt is not None:
            started, baseline = self.fetch_attempt
            if progress > baseline or same_corpse and navigation.corpse_lootable is False:
                self.fetch_attempt, self.loot_for_fight = None, fight
                ctx.log_event("quest_fetch_confirmed", {"target": fight.target_id, "progress": progress})
                return False
            if now - started < 8:
                return True
            self.fetch_attempt = None
            ctx.log_event("quest_fetch_unconfirmed", {"target": fight.target_id})
        if not same_corpse or self.loot_for_fight is fight or navigation.corpse_lootable is False:
            return False
        if navigation.fetch_ready and self.fetch_for_fight is not fight:
            self.set_forward(ctx, False)
            self.fetch_for_fight, self.fetch_attempt = fight, (now, progress)
            self.hunt.credit_deadline = now + 11
            ctx.log_event("quest_fetch", {"target": fight.target_id})
            ctx.input.tap("l")
            return True
        if navigation.task & TaskFlags.LOOT_IN_RANGE:
            self.loot_for_fight = fight
            ctx.log_event("quest_loot", {"target": fight.target_id})
            ctx.input.tap("f")
            return True
        return False

    def hunt_tick(self, ctx, now, progress, in_combat, navigation=None):
        channel = ctx.cfg.channel
        sample = decode_combat(ctx.capture.grab(*combat_region(channel.x, channel.y)))
        if navigation and not in_combat and self.loot_corpse(ctx, now, progress, sample, navigation):
            return
        if sample and sample.key_verified and str(sample.key) in self.quest_keys:
            return self.stop(ctx, "quest_combat_action_key_conflicts")
        verified_target = navigation.objective_target if navigation else 0
        status, key = self.hunt.step(now, sample, progress, in_combat, verified_target)
        if key:
            if status == "turn":
                self.set_forward(ctx, False)
            event = "quest_combat_turn" if status == "turn" else "quest_combat_action"
            ctx.log_event(event, {"id": self.steps[self.index].quest_id, "target": sample.target_id, "key": key})
            tap_combat_key(ctx.input, status, key)
        elif status == "credited":
            ctx.log_event("quest_combat_credit", {"id": self.steps[self.index].quest_id, "progress": progress})
        elif status == "target_not_found":
            targets = self.steps[self.index].combat_targets
            if not targets and navigation:
                self.hunt, self.scanning_objects = None, True
                self.object_probe, self.object_probe_sequence = 0, None
            elif self.search_quest_area(ctx, navigation):
                self.hunt = QuestHunt(targets)
            else:
                self.stop(ctx, "quest_combat_target_not_found")
        elif status != "waiting":
            self.stop(ctx, f"quest_combat_{status}")

    def wait_for_data(self, ctx, now, reason):
        self.set_forward(ctx, False)
        if self.unusable_since is None:
            self.unusable_since = now
        elif now - self.unusable_since > 8:
            self.stop(ctx, reason)

    def act(self, ctx, key, now):
        if now - self.last_action < 2:
            return
        if self.attempts >= 3:
            return self.stop(ctx, "quest_action_no_progress")
        self.last_action, self.attempts = now, self.attempts + 1
        ctx.log_event("quest_action", {"id": self.steps[self.index].quest_id, "key": key})
        ctx.input.tap(key)


def game_is_frontmost() -> bool:
    if sys.platform != "darwin":
        return False
    from AppKit import NSWorkspace
    from Foundation import NSDate, NSRunLoop

    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.001))
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app is not None and app.bundleIdentifier() == "com.blizzard.worldofwarcraft"


def wait_for_game(*, activate=True, timeout=12) -> None:
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

    if activate and not game_is_frontmost():
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.blizzard.worldofwarcraft")
        if apps:
            apps[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    print(f"正在等待游戏前台，最长 {timeout} 秒。", flush=True)
    deadline = time.monotonic() + timeout
    while not game_is_frontmost():
        if time.monotonic() >= deadline:
            raise ValueError("游戏未切到前台")
        time.sleep(.1)


class QuestModule(Module):
    name = "quest"
    initial_state = "RunQuests"

    def __init__(self, ctx, plan, kinds, planner=None):
        self.plan, self.kinds, self.planner = plan, kinds, planner
        super().__init__(ctx)

    def build_states(self):
        self.machine.add(RunQuests(*self.plan, self.kinds, self.planner))

    def run(self, max_duration_s):
        try:
            return super().run(max_duration_s)
        finally:
            if self.planner is not None:
                self.planner.close()


class AcceptCurrentQuest(State):
    """Initialize the existing quest runner from a prepared, stationary NPC dialogue."""

    def __init__(self, key, planner, kinds):
        self.key, self.planner, self.kinds = key, planner, kinds
        self.runner = None
        self.OUTCOME = None

    def on_enter(self, ctx):
        self.started = time.monotonic()

    def tick(self, ctx):
        if self.runner is not None:
            self.runner.tick(ctx)
            self.OUTCOME = self.runner.OUTCOME
            return
        channel = ctx.cfg.channel
        sample = decode_quest(ctx.capture.grab(*quest_region(channel.x, channel.y)))
        pos = ctx.pos()
        if sample and sample.tracking and sample.quest_id > 0 and pos:
            if sample.state != QuestState.PENDING:
                self.OUTCOME = "stopped:quest_already_accepted"
                return
            if sample.dialogue not in PICKUP_DIALOGUE or sample.dialogue_id != sample.quest_id:
                self.OUTCOME = "stopped:quest_pickup_dialogue_not_open"
                return
            route = Route.from_dict({
                "name": "current_quest_npc", "ui_map_id": sample.map_id, "mode": "ground",
                "points": [list(pos)], "arrival_radius": .0015,
            })
            step = QuestStep(sample.quest_id, self.key, route, route, None, None)
            self.runner = RunQuests(sample.map_id, [step], self.kinds, self.planner, accept_only=True)
            self.runner.on_enter(ctx)
            ctx.log_event("quest_accept_test_start", {"id": sample.quest_id, "key": self.key, "map": sample.map_id})
        elif time.monotonic() - self.started > 5:
            self.OUTCOME = "stopped:quest_pickup_not_prepared"


class QuestAcceptModule(QuestModule):
    name = "quest_accept"
    initial_state = "AcceptCurrentQuest"

    def __init__(self, ctx, key, planner, kinds):
        self.key, self.planner, self.kinds = key, planner, kinds
        Module.__init__(self, ctx)

    def build_states(self):
        self.machine.add(AcceptCurrentQuest(self.key, self.planner, self.kinds))


class NavigateCurrentQuest(State):
    def __init__(self, planner, kinds, travel_only, require_prepared=False):
        self.planner, self.kinds, self.travel_only = planner, kinds, travel_only
        self.require_prepared = require_prepared
        self.runner = None
        self.OUTCOME = None
        self.sequence = None
        self.waiting_for_focus = False

    def on_enter(self, ctx):
        self.started = time.monotonic()
        self.focus_deadline = self.started + PREPARED_FOCUS_WAIT_S

    def tick(self, ctx):
        if self.runner is not None:
            self.runner.tick(ctx)
            self.OUTCOME = self.runner.OUTCOME
            return
        if self.require_prepared and ctx.input.name != "mock":
            now = time.monotonic()
            if not game_is_frontmost():
                if now >= self.focus_deadline:
                    self.OUTCOME = "stopped:game_not_frontmost"
                    return
                if not self.waiting_for_focus:
                    self.waiting_for_focus = True
                    self.sequence = None
                    log.info("等待魔兽回到前台，回到游戏后会继续启动任务。")
                return
            if self.waiting_for_focus:
                self.waiting_for_focus = False
                self.started = now
                log.info("魔兽已回到前台，继续核对任务色条。")
        channel = ctx.cfg.channel
        navigation = decode_travel(ctx.capture.grab(*travel_region(channel.x, channel.y)))
        if navigation and navigation.quest_id > 0 and navigation.map_id > 0:
            if self.require_prepared:
                previous = self.sequence
                self.sequence = navigation.quest_id, navigation.sequence
                if previous is None or previous[0] != navigation.quest_id or previous == self.sequence:
                    if time.monotonic() - self.started > 5:
                        self.OUTCOME = "stopped:quest_navigation_channel_stale"
                    return
                if not navigation.task & TaskFlags.PREPARED or not navigation.flags & TravelFlags.KEYS_VERIFIED:
                    self.OUTCOME = "stopped:quest_automatic_not_prepared"
                    return
                if not self.travel_only and not navigation.npc_range_supported:
                    self.OUTCOME = "stopped:quest_turn_in_range_addon_required"
                    return
            targets = QUEST_TARGETS.get(navigation.quest_id, frozenset())
            step = QuestStep(navigation.quest_id, "9", None, None, None, None, targets)
            self.runner = RunQuests(navigation.map_id, [step], self.kinds, self.planner,
                                    auto_route=True, travel_only=self.travel_only,
                                    require_prepared=self.require_prepared)
            self.runner.on_enter(ctx)
        elif time.monotonic() - self.started > 5:
            self.OUTCOME = "stopped:quest_navigation_not_prepared"


class QuestNavigateModule(QuestModule):
    name = "quest_navigate"
    initial_state = "NavigateCurrentQuest"

    def __init__(self, ctx, planner, kinds, travel_only, require_prepared=False):
        self.planner, self.kinds, self.travel_only = planner, kinds, travel_only
        self.require_prepared = require_prepared
        Module.__init__(self, ctx)

    def build_states(self):
        self.machine.add(NavigateCurrentQuest(self.planner, self.kinds, self.travel_only, self.require_prepared))
