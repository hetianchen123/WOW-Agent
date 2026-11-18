"""Execute a validated same-map quest plan with live dialogue confirmation."""
from __future__ import annotations

import logging
import math
import re
import sys
import time
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml

from ..control.navigation import Route, RouteFollower, heading_error
from ..control.camera import CameraController
from ..control.pathfinding import GridRouter
from ..control.ground_trace import GroundTrace
from ..control.quest_travel import QuestTrip
from ..control.building_access import BuildingAccess, building_target_receipt
from ..control.quest_search import QuestAreaSearch, inside_quest_search_area
from ..control.quest_terrain import (SCENE_BOUNDS, SCENE_ERRORS, SceneRequest, TerrainDetour, capture_scene,
                                     local_vision_available)
from ..core.statemachine import State
from ..core.paths import SCREENSHOT_DIR, DATA_DIR
from ..decision.startlux import DECISION_WAIT_S
from ..decision.route_planner import available as cloud_route_available
from ..perception.capture import CaptureError, ScreenCapture, frontmost_game_bounds, game_ui_bounds
from ..perception.channel_spec import REGION_H, REGION_W, StatusFlags
from ..perception.combat_channel import combat_region, decode_combat
from ..perception.cv import hostile_nameplate_points, minimap_quest_dots, pumpkin_probe_points, save_snapshot
from ..perception.observation import ObservationTracker
from ..perception.player_identity import profile_from_sample
from ..control.approach import ApproachAttempt, ApproachGoal, ApproachObservation, ActionState
from ..perception.quest_channel import (
    RESOURCE_PROGRESS_MAX, Dialogue, Objective, QuestKind, QuestState, decode_quest, quest_region,
)
from ..perception.travel_channel import (LifeState, ObjectiveAction, PetState, RoundStage, TaskFlags, TravelFlags,
                                         decode_travel, travel_region)
from .base import Module
from .combat import Fight, QuestDefense, QuestHunt, TURN_INTERVAL, TURN_SETTLE, tap_combat_key
from .recovery import ClassPreparation, QuestRecovery
from .loot import LootSweep

STORY_KINDS = frozenset({QuestKind.CAMPAIGN, QuestKind.MAINLINE, QuestKind.SIDE})
UNSAFE_FLAGS = StatusFlags.DEAD | StatusFlags.GHOST | StatusFlags.FLYING
PICKUP_DIALOGUE = {Dialogue.OFFER, Dialogue.DETAIL}
TURN_IN_DIALOGUE = {Dialogue.TURN_IN, Dialogue.PROGRESS, Dialogue.REWARD}
QUEST_TARGETS = {90882: frozenset({244669, 244670, 244671, 244672})}
ARRIVAL_QUESTS = frozenset({90883})
PREPARED_FOCUS_WAIT_S = 45
NPC_SCAN_ANGLES = tuple(math.radians(angle) for angle in range(0, 360, 30))
OBJECT_APPROACH_YARDS = 2.5
OBJECT_CLOSE_RECHECK_YARDS = 16
OBJECT_REFINE_YARDS = 1.25
CATAPULT_APPROACH_YARDS = 4.5
CATAPULT_MARKER_AREA_YARDS = 150
TASK_FAILURES = {
    "quest_building_access_unconfirmed",
    "quest_segmented_setup_unavailable",
    "quest_action_no_progress", "quest_accept_not_confirmed", "quest_classification_unknown",
    "quest_combat_target_not_found", "quest_combat_fight_timeout", "quest_combat_no_cast_or_damage_receipt",
    "quest_combat_no_progress", "quest_combat_no_quest_credit", "quest_combat_resource_no_progress",
    "quest_combat_approach_timeout", "quest_combat_approach_blocked", "quest_combat_line_of_sight_blocked", "quest_combat_facing_unconfirmed",
    "quest_npc_player_origin_unresolved",
    "quest_npc_camera_composition_unresolved", "quest_npc_camera_restore_unconfirmed",
    "quest_npc_alignment_timeout", "quest_npc_approach_direction_unconfirmed", "quest_npc_marker_range_unconfirmed",
    "quest_combat_facing_turn_timeout", "quest_combat_facing_feedback_unavailable",
    "quest_cross_map_route_required", "quest_destination_unavailable", "quest_interact_position_unavailable",
    "quest_object_approach_blocked", "quest_object_hover_exhausted", "quest_object_interaction_unconfirmed",
    "quest_object_not_found", "quest_object_progress_unconfirmed", "quest_object_visual_target_required",
    "quest_objective_dialogue_choice_required", "quest_objective_no_progress", "quest_objective_npc_not_found",
    "quest_objective_profile_required", "quest_objective_unsupported", "quest_combat_needs_measurable_objective",
    "quest_offer_not_open", "quest_phase_timeout",
    "quest_pickup_dialogue_not_open", "quest_pickup_npc_not_found", "quest_pickup_npc_selection_unconfirmed",
    "quest_ready_state_unconfirmed", "quest_resource_search_area_exhausted", "quest_reward_choice_required",
    "quest_route_blocked", "quest_turn_in_approach_blocked", "quest_turn_in_approach_exhausted",
    "quest_turn_in_npc_selection_unconfirmed", "quest_turn_in_npc_lost", "quest_flight_route_blocked",
    "quest_skyriding_landing_blocked", "quest_skyriding_timeout",
    "quest_patrol_blocking_route",
    "quest_npc_visual_unavailable", "quest_npc_route_unconfirmed",
    "quest_npc_escape_evidence_insufficient", "quest_npc_escape_time_budget",
    "quest_npc_escape_observation_timeout", "quest_npc_escape_player_identity_unresolved",
}
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
        self.building_access = None
        self.ground_router = None
        self.terrain = None
        self.terrain_attempts = []
        self.object_scene = None
        self.travel_keys: frozenset[str] = frozenset()
        self.nav_sequence, self.nav_at = None, 0.0
        self.search_waypoint, self.searches = None, 0
        self.area_search = None
        self.loot_sweep = None
        self.batch_for_fight = None
        self.defense_had_output = False
        self.defense_loot = []
        self.defense_restore_until = None
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
        self.npc_step: ApproachAttempt | None = None
        self.observations = ObservationTracker()
        self.observation = None
        self.observation_logged_at = float("-inf")
        self.npc_steps = 0
        self.npc_ground_wait = None
        self.npc_obstacle_reason = None
        self.npc_detouring = False
        self.npc_rejoin = False
        self.npc_obstacle_attempts = []
        self.npc_scan_started: float | None = None
        self.npc_scan_aligned_at: float | None = None
        self.npc_scan_direction = 0
        self.npc_scan_heading = 0.0
        self.npc_turn_at = float("-inf")
        self.npc_alignment = None
        self.npc_alignment_sequence = None
        self.npc_direction_retries = {}
        self.npc_interaction_attempts = {}
        self.mouse_turn_at = float("-inf")
        self.npc_select_attempted = False
        self.npc_dialogue_retries = 0
        self.npc_dialogue_recovery_at: float | None = None
        self.mount_clicked_at: float | None = None
        self.mount_key_attempted = self.mount_style_attempted = False
        self.last_action = self.last_progress = 0.0
        self.camera_restore_reason = "start"
        self.camera = CameraController()
        self.camera_settle_until = float("-inf")
        self.camera_receipt = None
        self.camera_unavailable_logged = False
        self.camera_close_since = None
        self.camera_close_handled = False
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
        self.round_navigation = None
        self.unusable_reason = None
        self.suspend_request = None
        self.suspend_camera = None
        self.suspended_tasks = []
        self.objective_index = None
        self.combat_frame = None
        self.last_combat_profile = None
        self.combat_adjusted = None
        self.accepted_from_offer = False
        self.hunt: QuestHunt | None = None
        self.defense: QuestDefense | None = None
        self.recovery: QuestRecovery | None = None
        self.recovery_deferred_at: float | None = None
        self.pet_preparation: ClassPreparation | None = None
        self.pet_pause_at: float | None = None
        self.pet_dismount_at: float | None = None
        self.pet_mend_at = float("-inf")
        self.pet_mend_receipt = None
        self.safe_ground = []
        self.ground_trace = GroundTrace()
        self.danger_zones = []
        self.avoidance_trip = None
        self.patrol_wait_until = None
        self.last_patrol_check = float("-inf")
        self.patrol_seen_until = {}
        self.loot_for_fight = None
        self.loot_attempt = None
        self.fetch_for_fight = None
        self.fetch_attempt: tuple[float, int, bool] | None = None
        self.corpse_wait_at: float | None = None
        self.corpse_approach = None
        self.corpse_restore: tuple[Fight, float] | None = None
        self.object_probe = 0
        self.object_probe_at = 0.0
        self.object_probe_sequence: int | None = None
        self.object_points: tuple[tuple[float, float], ...] | None = None
        self.object_target: tuple[float, float] | None = None
        self.apr_waypoint: tuple[float, float] | None = None
        self.object_check_origin: tuple[float, float] | None = None
        self.object_check_pending: tuple[int, float] | None = None
        self.object_targets_tried: list[tuple[float, float]] = []
        self.object_completed: list[tuple[float, float]] = []
        self.object_sightings: dict[tuple[float, float], float] = {}
        self.object_lost_origin = None
        self.object_rescan_at = float("-inf")
        self.object_lost_revisited = False
        self.object_minimap_checked = False
        self.object_approach_yards = OBJECT_APPROACH_YARDS
        self.object_click_at: float | None = None
        self.object_click_target: tuple[float, float] | None = None
        self.object_click_attempts = 0
        self.quest_keys = {key for step in steps for key in (step.dialog_key, step.objective_key) if key}
        self.OUTCOME = None

    def on_enter(self, ctx):
        self.last_frame = self.last_target = time.monotonic()
        self.run_started = self.last_frame

    def stop(self, ctx, reason):
        self.ground_trace.clear()
        self.cancel_scene_requests()
        self.camera.forget()
        ctx.input.release_all()
        self.suspend_camera = None
        if reason == "game_not_frontmost":
            ctx.log_event("quest_focus_lost", foreground_app_details())
        ctx.log_event("quest_stop_requested", {"reason": reason, "phase": self.phase.name if self.phase else None,
                                               "id": self.steps[self.index].quest_id})
        nav = self.round_navigation
        if (reason in TASK_FAILURES and nav and nav.suspend_ready and self.suspend_request is None
                and nav.suspend_epoch is not None
                and self.round_stage not in {RoundStage.NONE, RoundStage.DONE}):
            self.travel_keys, self.forward = frozenset(), False
            self.execution_started = True
            self.suspend_request = (nav.quest_id, nav.phase, nav.suspend_epoch, reason, time.monotonic(), "pending")
            if self.camera_restore_reason == "terrain_finished":
                self.suspend_camera = {"started": None, "requested": False, "result": None}
            return
        self.OUTCOME = f"stopped:{reason}"

    def suspend_task(self, ctx, now, nav, in_combat):
        task, phase, epoch, reason, started, state = self.suspend_request
        self.set_forward(ctx, False)
        if (not nav or nav.quest_id != task or nav.suspend_epoch is None
                or not nav.task & TaskFlags.PREPARED):
            return self.stop(ctx, "quest_suspend_identity_lost")
        if state == "pending" and self.suspend_camera is not None:
            if self.finish_suspend_camera(ctx, now, nav, in_combat):
                return
            # Camera cleanup has its own budget; leave the full F5 receipt budget.
            self.suspend_request = task, phase, epoch, reason, now, state
            return
        if nav.phase != phase and nav.suspend_epoch == epoch:
            self.suspend_request = None
            self.unusable_since = None
            return
        if state != "pending" and nav.suspend_epoch == (epoch + 1) % 8 and nav.current_suspended:
            if state == "pressed":
                entry = {"id": task, "phase": phase.name, "reason": reason}
                self.suspended_tasks.append(entry)
                ctx.stats["quests_suspended"] = len(self.suspended_tasks)
                ctx.log_event("quest_suspended", entry)
                self.suspend_request = task, phase, epoch, reason, started, "acknowledged"
            if nav.round_advance:
                self.suspend_request = None
                self.unusable_since = None
                return self.advance_round(ctx, now, nav, 0)
        elif nav.suspend_epoch != epoch:
            return self.stop(ctx, "quest_suspend_unexpected_ack")
        if now - started > 12:
            return self.stop(ctx, "quest_suspend_ack_timeout")
        if state == "pending" and not in_combat and nav.suspend_ready and not nav.flags & (
                TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.CASTING):
            self.suspend_request = task, phase, epoch, reason, started, "pressed"
            ctx.input.tap("f5")

    def finish_suspend_camera(self, ctx, now, nav, in_combat):
        cleanup = self.suspend_camera
        if cleanup["started"] is None:
            cleanup["started"] = now
        unsafe = in_combat or nav.flags & (TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.CASTING
                                          | TravelFlags.SWIMMING | TravelFlags.MOUNTED)
        result = None
        if unsafe or not nav.camera or nav.camera.paused or not nav.camera_restore_available:
            result = "unavailable_or_unsafe"
        elif now - cleanup["started"] >= 10:
            result = "deadline"
        else:
            def requested():
                cleanup["requested"] = True
                self.camera_restore_reason = None
            def completed(value):
                cleanup["result"] = value["result"]
            self.camera.tick(ctx, now, nav,
                reason=None if cleanup["requested"] else "terrain_finished",
                before_start=requested, after_result=completed)
            result = cleanup["result"]
            if result is None and cleanup["requested"] and self.camera.pending is None:
                result = "interrupted"
            if result is None:
                return True
        self.camera.cancel(ctx, now, "suspend_camera_finished")
        ctx.log_event("quest_suspend_camera_result", {"id": nav.quest_id, "result": result,
            "elapsed_s": round(now - cleanup["started"], 2), "composition_verified": False})
        self.suspend_camera = None
        self.camera_restore_reason = None
        return False

    def read_combat(self, ctx):
        channel = ctx.cfg.channel
        image = self.combat_frame
        if image is None:
            image = ctx.capture.grab(*combat_region(channel.x, channel.y))
        sample = (self.observation.combat.value if self.combat_frame is not None and self.observation is not None
                  else decode_combat(image))
        if sample and sample.key_verified and sample.identity != self.last_combat_profile:
            self.last_combat_profile = sample.identity
            ctx.log_event("quest_combat_profile", {"build": sample.build, "class": sample.class_id,
                          "spec": sample.spec_id, "config": sample.config_id,
                          "cast_feedback": sample.cast_epoch is not None})
        return sample

    def remember_defense_corpse(self, fight, sample):
        if (fight and fight.presses and sample and sample.target_dead and sample.target_id == fight.target_id
                and sample.state_epoch == fight.state_epoch and fight not in self.defense_loot):
            self.defense_loot.append(fight)

    def finish_defense_loot(self, ctx, now, nav):
        if not self.defense_loot or not nav or not nav.task & TaskFlags.PREPARED:
            return False
        sample = self.read_combat(ctx)
        if sample is None:
            return False
        if self.hunt is not None and self.hunt.fight is not None:
            quest_fight = self.hunt.fight
            if sample.target_dead and sample.target_id == quest_fight.target_id and sample.state_epoch == quest_fight.state_epoch:
                return False
        fight = next((candidate for candidate in self.defense_loot if sample.target_dead
                      and candidate.target_id == sample.target_id and candidate.state_epoch in
                      {sample.state_epoch, sample.previous_target_epoch}), None)
        if fight is not None:
            if sample.previous_target_epoch == fight.state_epoch:
                self.corpse_restore = fight, now
            hunt, self.hunt = self.hunt, QuestHunt(frozenset())
            self.hunt.fight = fight
            try:
                if self.loot_corpse(ctx, now, 0, sample, nav):
                    return True
            finally:
                self.hunt = hunt
            self.defense_loot.remove(fight)
        elif (self.defense_restore_until is None and nav.last_target_verified and not sample.target_id
              and sample.key_verified and not sample.unsafe):
            self.defense_restore_until = now + 1.5
            ctx.input.tap("p")
            return True
        elif self.defense_restore_until and now < self.defense_restore_until:
            return True
        if self.defense_loot:
            ctx.log_event("quest_defense_corpses_unconfirmed", {
                "targets": [(candidate.target_id, candidate.state_epoch) for candidate in self.defense_loot],
            })
            self.defense_loot.clear()
        self.defense_restore_until = None
        return False

    def set_forward(self, ctx, enabled):
        if self.auto_route and not enabled:
            self.set_travel_keys(ctx, frozenset())
            return
        if self.forward != enabled:
            (ctx.input.down if enabled else ctx.input.up)("w")
            self.forward = enabled

    def set_travel_keys(self, ctx, keys):
        self.ground_trace.note_input(keys)
        previous = self.travel_keys
        for key in sorted(self.travel_keys - keys):
            ctx.input.up(key)
        for key in sorted(keys - self.travel_keys):
            ctx.input.down(key)
        self.travel_keys = keys
        if previous != keys:
            ctx.log_event("quest_movement_input", {"released": sorted(previous - keys),
                "pressed": sorted(keys - previous), "held": sorted(keys), "result": "input_sent"})

    def apply_travel_command(self, ctx, command):
        if self.camera.pending and (command.tap or command.camera_yaw_px or command.keys - {"w"}):
            self.camera.cancel(ctx, time.monotonic(), "travel_input_priority")
        self.set_travel_keys(ctx, command.keys)
        if command.camera_yaw_px is not None:
            if command.keys!=frozenset({'f4'}) or command.tap or not 0 < abs(command.camera_yaw_px) <= 220:
                raise ValueError('automatic_camera_command_invalid')
            x,y,w,h=frontmost_game_bounds()
            ctx.input.move_to(round(x+w*.5),round(y+h*.4),jitter_px=0)
            try:
                ctx.input.rmb_turn(command.camera_yaw_px)
            finally:
                # Release the calibration lease immediately, including errors.
                self.set_travel_keys(ctx,frozenset())
            ctx.log_event('quest_calibration_camera_orbit',{'pixels':command.camera_yaw_px,
                'movement_confirmed':False,'result':'input_sent'})
        if command.tap:
            self.ground_trace.note_input({command.tap})
            if command.precise:
                ctx.input.tap_precise(command.tap,command.hold_s)
            elif command.hold_s is None:
                ctx.input.tap(command.tap)
            else:
                ctx.input.tap(command.tap, hold_s=command.hold_s)
            ctx.log_event("quest_movement_input", {"tap": command.tap, "hold_s": command.hold_s,
                                                   "result": "input_sent"})

    def cancel_scene_requests(self):
        if self.building_access:
            self.building_access.close()
        self.npc_step = None
        self.npc_alignment = self.npc_alignment_sequence = None
        self.corpse_approach = None
        if self.npc_detouring:
            self.npc_rejoin, self.arrived = True, False
        if self.terrain:
            if getattr(self.terrain, "probe_view_used", False):
                self.camera_restore_reason = "terrain_finished"
            self.terrain.close()
            self.terrain = None
        self.npc_detouring = False
        self.npc_obstacle_reason = self.npc_ground_wait = None
        if self.object_scene:
            self.object_scene[0].close()
            self.object_scene = None

    def restore_camera(self, ctx, now, nav, *, dialogue_open=False, in_combat=False, before_detour=False):
        if self.building_access and self.building_access.locked and not in_combat and not dialogue_open:
            self.camera.cancel(ctx,now,'building_access')
            return False
        if nav.camera is not None:
            return self.adjust_scene_camera(ctx, now, nav, dialogue_open=dialogue_open, in_combat=in_combat)
        if not nav.camera_restore_available:
            if self.require_prepared and not self.camera_unavailable_logged:
                ctx.log_event("quest_camera_unavailable", {"id": nav.quest_id, "key": "f8"})
                self.camera_unavailable_logged = True
            return False
        if nav.flags & TravelFlags.FLYING:
            self.camera_restore_reason = "landing"
        busy = TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING | TravelFlags.CASTING | TravelFlags.IN_COMBAT
        if (dialogue_open or in_combat or nav.flags & busy or self.object_click_at is not None
                or self.npc_press_at is not None or self.loot_sweep is not None
                or (self.hunt and self.hunt.fight)):
            self.camera_close_since = None
            if self.camera_receipt is not None:
                self.camera_receipt = None
                self.camera_restore_reason = self.camera_restore_reason or "interrupted"
            return False
        if self.camera_receipt is not None:
            epoch, deadline = self.camera_receipt
            if nav.camera_epoch != epoch:
                self.camera_receipt = None
                self.camera_settle_until = now + .45
                ctx.log_event("quest_camera_view_reported", {"id": nav.quest_id})
                return True
            if now < deadline:
                return True
            self.camera_receipt = None
            self.camera_settle_until = now
            ctx.log_event("quest_camera_restore_unconfirmed", {"id": nav.quest_id, "key": "f8"})
        if now < self.camera_settle_until:
            return True
        if self.terrain or self.object_scene:
            return False
        if self.travel_keys:
            return False
        if not nav.camera_zoom_close:
            self.camera_close_since = None
            self.camera_close_handled = False
        elif self.camera_close_since is None:
            self.camera_close_since = now
        reason = self.camera_restore_reason
        if now >= self.camera_settle_until + 3:
            if before_detour and nav.camera_zoom_close:
                reason = reason or "before_detour"
            elif (not self.camera_close_handled and self.camera_close_since is not None
                  and now - self.camera_close_since >= .5):
                reason = reason or "camera_too_close"
        if not reason:
            return False
        self.set_forward(ctx, False)
        if self.trip:
            self.trip.motion_position = None
        self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None
        self.object_check_pending = None
        self.camera_restore_reason = None
        self.camera_close_handled = nav.camera_zoom_close
        self.camera_receipt = nav.camera_epoch, now + 3.8
        self.execution_started = True
        ctx.log_event("quest_camera_restore_requested", {"id": nav.quest_id, "reason": reason, "key": "f8"})
        ctx.input.tap("f8")
        return True

    def adjust_scene_camera(self, ctx, now, nav, *, dialogue_open=False, in_combat=False):
        airborne = bool(nav.flags & TravelFlags.FLYING)
        fighting = in_combat or bool(nav.flags & TravelFlags.IN_COMBAT)
        phase = self.trip.phase if self.trip else None
        flight_window = (phase in {"cruise", "sky_glide"}
                         and not (self.trip and getattr(self.trip, "sky_pending_cast", None))
                         and self.travel_keys <= {"w"})
        locked = bool(dialogue_open or nav.flags & (TravelFlags.CASTING | TravelFlags.SWIMMING)
                      or nav.flags & TravelFlags.FALLING and not airborne
                      or self.object_click_at is not None or self.npc_press_at is not None
                      or self.loot_sweep is not None or self.terrain or self.object_scene
                      or self.object_check_pending is not None
                      or self.loot_attempt is not None or self.fetch_attempt is not None
                      or self.npc_ground_wait is not None or self.npc_obstacle_reason is not None
                      or self.defense and self.defense.retreat
                      or phase and phase.startswith("recover_")
                      or airborne and not flight_window
                      or not airborne and phase in {"mount", "takeoff", "climb", "sky_launch", "sky_align",
                                                    "style"}
                      or phase in {"sky_dismount", "dismount", "change_mount"}
                      and bool(nav.flags & TravelFlags.MOUNTED))
        if airborne:
            self.camera_restore_reason = "landing"
        background = airborne or fighting
        reason = None if background else self.camera_restore_reason

        def prepare():
            if not background:
                self.set_forward(ctx, False)
                if self.trip:
                    self.trip.motion_position = None
                self.camera_restore_reason = None
            # Screen points cannot survive a change of camera projection.
            self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None
            self.object_check_pending = None
            self.execution_started = True
            if not background:
                self.camera_evidence(ctx, nav, "before")

        def completed(result):
            if not background:
                self.camera_evidence(ctx, nav, "after")

        waiting = self.camera.tick(ctx, now, nav, locked=locked, background=background,
                                   reason=reason, before_start=prepare, after_result=completed)
        self.camera_settle_until = self.camera.settle_until
        if waiting:
            self.set_forward(ctx, False)
            if self.trip:
                self.trip.motion_position = None
        return waiting

    def camera_evidence(self, ctx, nav, stage):
        if not isinstance(ctx.capture, ScreenCapture):
            return
        try:
            bounds = game_ui_bounds(frontmost_game_bounds(), nav.minimap)
            image = ctx.capture.grab(*bounds)
            path = save_snapshot(image, f"camera_{stage}_{nav.camera.scene.name.lower()}", directory=SCREENSHOT_DIR)
            ctx.log_event("quest_camera_evidence", {"stage": stage, "id": nav.quest_id,
                "scene": nav.camera.scene.name, "zoom": nav.camera.zoom, "sequence": nav.sequence,
                "position": nav.position, "image": str(path) if path else None,
                "composition_verified": False})
        except (CaptureError, ValueError, OSError) as exc:
            ctx.log_event("quest_camera_evidence_unavailable", {"reason": str(exc)})

    def nav3d_enabled(self, ctx):
        return getattr(getattr(ctx.cfg, "nav3d", None), "mode", "visual") != "visual"

    def travel_nav3d(self, ctx, now, navigation):
        from ..nav3d.runtime import Nav3DCoordinator
        from ..nav3d import NavigationError
        try:
            if not hasattr(self, "nav3d_coordinator"):
                self.nav3d_coordinator = Nav3DCoordinator(ctx.cfg.nav3d, now=now, log_event=ctx.log_event,
                    resume_runs=DATA_DIR/'runs')
            command = self.nav3d_coordinator.step(now, getattr(self, "nav3d_sample", None), navigation,
                goal_override=self.object_target or self.search_waypoint,
                target_receipt=building_target_receipt(navigation,self.observation,now) if navigation else None,
                travel_only=self.travel_only)
        except (NavigationError, OSError, ValueError) as exc:
            return self.stop(ctx, "nav3d_initialization_failed:" + str(exc))
        if command.tap or command.keys:
            self.execution_started = True
        self.apply_travel_command(ctx, command)
        if command.outcome == "nav3d_route_arrived":
            self.arrived, self.last_progress = True, now
            ctx.log_event("nav3d_route_arrived", {"task_receipt_confirmed": False})
            if self.travel_only:
                self.OUTCOME = "completed"
            else:
                self.stop(ctx, "nav3d_route_arrived_waiting_task_receipt")
        elif command.outcome == "nav3d_target_confirmed":
            self.arrived,self.last_progress=True,now
            self.stop(ctx,"nav3d_target_reached_waiting_task_execution")
        elif command.outcome:
            self.stop(ctx, command.outcome)

    def travel(self, ctx, now, navigation, heading):
        if self.nav3d_enabled(ctx):
            return self.travel_nav3d(ctx, now, navigation)
        if self.terrain:
            command = self.terrain.step(ctx, now, navigation, heading)
            self.apply_travel_command(ctx, command)
            if command.outcome is None:
                return
            if getattr(self.terrain, "probe_view_used", False):
                self.camera_restore_reason = "terrain_finished"
            self.terrain.close()
            self.terrain = None
            ctx.log_event("quest_terrain_result", {"id": navigation.quest_id, "position": navigation.position,
                                                   "result": command.outcome})
            if command.outcome == "detoured":
                self.trip.detours = 0
                self.trip.stall_position, self.trip.stall_count = None, 0
                self.trip.enter("choose", now)
                self.object_check_origin = None
                return
            return self.route_blocked(ctx, navigation)
        if self.check_patrol(ctx, now, navigation):
            return

        return self.travel_to_destination(ctx, now, navigation, heading)

    def check_patrol(self, ctx, now, navigation):
        recovery = navigation.recovery
        if self.patrol_wait_until is not None:
            if recovery and recovery.patrol_id:
                self.set_forward(ctx, False)
                if now >= self.patrol_wait_until:
                    self.stop(ctx, "quest_patrol_blocking_route")
                return True
            self.patrol_wait_until = None
        if (self.require_prepared and not self.travel_only and navigation.position and navigation.world_size
                and not navigation.flags & (TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
                                             | TravelFlags.CASTING)
                and now - self.last_patrol_check >= .35):
            self.last_patrol_check = now
            if recovery and recovery.patrol_id and recovery.patrol_danger:
                return self.start_patrol_avoidance(ctx, now, navigation, recovery.patrol_id, recovery.patrol_danger)
        return False

    def travel_to_destination(self, ctx, now, navigation, heading):
        if (navigation.quest_id == 90895 and self.search_waypoint is not None and self.object_target is None
                and navigation.precise_object and not navigation.objective_pin
                and not navigation.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING)
                and now - self.object_rescan_at >= .8):
            self.object_rescan_at = now
            try:
                bounds = game_ui_bounds(frontmost_game_bounds(), navigation.minimap)
                point = self.object_minimap_target(ctx, navigation, bounds, heading)
            except ValueError:
                point = None
            if point is not None:
                self.start_object_route(point)
        if self.object_target is not None:
            airborne = navigation.flags & (TravelFlags.FLYING | TravelFlags.FALLING)
            flight_mount = (self.trip and self.trip.mode in {"fly", "skyriding"}
                            and navigation.flags & TravelFlags.MOUNTED)
            if not (airborne or flight_mount) and not self.recheck_object_target(ctx, now, navigation, heading):
                return
        destination = self.object_target or self.search_waypoint or navigation.destination
        local_search = self.search_waypoint is not None
        live_object = bool(self.object_target or navigation.objective_pin and not local_search)
        if self.trip is None or self.trip.router.distance(self.trip.destination, destination) > (4 if live_object else 8):
            # A kill objective is an area marker. Start checking targets before
            # walking into whatever occupies the exact center of that area.
            if self.phase == QuestState.READY and not self.travel_only:
                # This is only the map-marker search area. Actual NPC range has
                # its own receipt; walking onto the pin cannot prove arrival.
                radius = 4
            elif live_object and not self.travel_only:
                radius = self.object_approach_yards
            else:
                radius = 14 if self.hunt is not None and self.search_waypoint is None else 4
            self.trip = QuestTrip(navigation.world_size, destination, ctx.cfg.navigation.facing_convention,
                                  arrival_radius=radius, local_travel=live_object or local_search,
                                  walk_only=bool(self.building_access and self.building_access.ground_only),
                                  approach_on_foot=live_object or self.phase in {QuestState.READY, QuestState.PENDING},
                                  log_event=ctx.log_event)
            map_key = (navigation.map_id, navigation.world_size)
            if self.ground_router is None or self.ground_router[0] != map_key:
                self.ground_router = (map_key, self.trip.router)
                self.terrain_attempts.clear()
            else:
                self.trip.router = self.ground_router[1]
            source = ("object_minimap" if self.object_target else
                      "object_pin" if live_object else "local_search" if local_search else
                      "apr_objective" if navigation.apr_waypoint else "quest_waypoint")
            ctx.log_event("quest_route", {"id": navigation.quest_id, "map": navigation.map_id,
                                          "destination": destination, "arrival_radius": radius,
                                          "source": source})
        previous = self.trip.mode, self.trip.phase
        command = self.trip.step(now, navigation, heading, heading_age=ctx.heading_age())
        if previous != (self.trip.mode, self.trip.phase):
            remaining = (round(self.trip.router.distance(navigation.position, destination), 1)
                         if navigation.position is not None else None)
            ctx.log_event("quest_travel", {"id": navigation.quest_id, "mode": self.trip.mode,
                                           "phase": self.trip.phase, "remaining_yards": remaining,
                                           "can_fly": bool(navigation.flags & TravelFlags.CAN_FLY),
                                           "can_mount": bool(navigation.flags & TravelFlags.CAN_MOUNT)})
        if command.outcome == "route_blocked" and self.npc_rejoin:
            return self.begin_npc_detour(ctx, now, navigation, "rejoin_route_blocked",
                                         movement_result=command.movement_result)
        if command.outcome == "route_blocked" and self.start_detour(ctx, now, navigation, heading):
            return
        if command.outcome:
            if command.outcome == "route_blocked":
                return self.route_blocked(ctx, navigation)
            if command.outcome != "arrived":
                return self.stop(ctx, "quest_" + command.outcome)
            self.set_travel_keys(ctx, frozenset())
            self.arrived, self.last_progress = True, now
            self.npc_rejoin = False
            ctx.log_event("quest_destination_reached", {"id": navigation.quest_id, "position": navigation.position})
            if live_object:
                ctx.log_event("quest_object_approach_reached", {"id": navigation.quest_id,
                                                               "distance_yards": round(self.trip.router.distance(
                                                                   navigation.position, destination), 2)})
            if self.travel_only:
                ctx.stats["destinations_reached"] = 1
                self.OUTCOME = "completed"
            return
        self.apply_travel_command(ctx, command)

    def route_blocked(self, ctx, navigation):
        self.set_forward(ctx, False)
        if self.object_target is not None:
            ctx.log_event("quest_object_approach_blocked", {"id": navigation.quest_id,
                                                           "destination": self.object_target})
            self.finish_object_approach()
        elif not (navigation.quest_id == 90895 and navigation.phase == QuestState.ACTIVE and navigation.precise_object):
            if self.search_waypoint and (navigation.apr_waypoint or self.hunt) and self.search_quest_area(ctx, navigation):
                return
            return self.stop(ctx, "quest_route_blocked")
        self.search_waypoint, self.trip, self.arrived = None, None, True
        self.object_minimap_checked = False

    @property
    def building_pending(self):
        return bool(self.building_access and self.building_access.ground_only
                    and self.building_access.state != 'inside')

    def process_building_access(self, ctx, now, nav, heading):
        if self.nav3d_enabled(ctx):
            return False
        config=getattr(ctx.cfg,'segmented_navigation',None)
        if not (self.auto_route and self.require_prepared and not self.travel_only
                and config and config.enabled and config.building_access):
            return False
        if self.building_access is None:
            from ..control.segmented_detour import SegmentedDetour
            from ..control.quest_terrain import capture_building_overview
            def entry(at, sample, context):
                return SegmentedDetour(at,sample,GridRouter(*sample.world_size),
                    ctx.cfg.navigation.facing_convention,config,capture_scene,deadline=at+180,
                    goal_mode='building_entry',ground_trace=self.ground_trace,
                    overview_capture=capture_building_overview)
            self.building_access=BuildingAccess(config,capture=capture_scene,
                convention=ctx.cfg.navigation.facing_convention,entry_factory=entry,
                memory_path=DATA_DIR/'navigation/exterior-ground.json')
        before=self.building_access.ground_only
        try:
            command=self.building_access.step(ctx,now,nav,heading,
                target_receipt=building_target_receipt(nav,self.observation,now))
        except SCENE_ERRORS as exc:
            ctx.log_event('quest_building_error',{'error':str(exc)})
            self.stop(ctx,'quest_building_access_unconfirmed')
            return True
        if before!=self.building_access.ground_only or self.building_pending:
            self.arrived=False
            if before!=self.building_access.ground_only:
                self.trip=None;self.search_waypoint=None
        if self.building_access.target_receipt:
            self.arrived=True;self.trip=None;self.search_waypoint=None
        if command is None:return False
        self.apply_travel_command(ctx,command)
        if command.outcome:
            ctx.log_event('quest_building_failed',dict(reason=command.outcome,state=self.building_access.state,
                position=nav.position,quest_id=nav.quest_id))
            self.stop(ctx, 'quest_building_telemetry_stale' if command.outcome=='building_telemetry_stale'
                      else 'quest_building_access_unconfirmed')
        return True

    def start_detour(self, ctx, now, nav, heading):
        client = getattr(self.planner, "client", None)
        if (ctx.input.name != "helper" or not local_vision_available()
                or self.trip.mode not in {"walk", "mount"} or nav.position is None or len(self.terrain_attempts) >= 4
                or any(self.trip.router.distance(nav.position, p) < 12 for p in self.terrain_attempts)):
            return False
        self.set_forward(ctx, False)
        if self.restore_camera(ctx, now, nav, before_detour=True):
            return True
        self.terrain_attempts.append(nav.position)
        self.trip.router.block_ahead(nav.position, self.trip.motion_goal or self.trip.destination)
        self.terrain = TerrainDetour(max(now, self.camera_settle_until), nav, heading, self.trip, client,
                                     ground_trace=self.ground_trace, player_identity=self.terrain_identity(now))
        ctx.log_event("quest_terrain_probe", {"id": nav.quest_id, "position": nav.position, "height": nav.height,
                                              "destination": self.trip.destination, "contacts": self.trip.stall_count})
        return True

    def terrain_identity(self, now):
        channel = getattr(getattr(self, "observation", None), "combat", None)
        return profile_from_sample(channel.value) if channel and channel.fresh(now, 1.5) else None

    def turn(self, ctx, error, now):
        cfg = ctx.cfg.input
        if abs(error) > math.radians(cfg.turn_deadband_deg) and now - self.mouse_turn_at >= TURN_INTERVAL:
            limit = min(40, cfg.mouse_turn_max_px)
            pixels = int(max(-limit, min(error * cfg.mouse_turn_gain, limit)))
            ctx.input.rmb_turn(-pixels if cfg.invert_turn else pixels)
            self.mouse_turn_at = now

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
            self.turn(ctx, decision.turn_err, now)
        self.set_forward(ctx, decision.move_forward)

    def recover(self, ctx, now, navigation, status):
        sample = navigation.recovery if navigation else None
        life = sample.life if sample else LifeState.UNKNOWN
        dead = life in {LifeState.DEAD, LifeState.GHOST}
        if life == LifeState.UNKNOWN and status:
            dead = bool(status[0] & (StatusFlags.DEAD | StatusFlags.GHOST))
        if self.recovery is None:
            if not dead:
                return False
            if navigation and sample is None:
                self.stop(ctx, "quest_recovery_addon_required")
                return True
            self.set_forward(ctx, False)
            self.cancel_scene_requests()
            self.pet_preparation = self.pet_pause_at = self.pet_dismount_at = None
            self.execution_started = True
            self.recovery = QuestRecovery(now)
            self.avoidance_trip, self.patrol_wait_until = None, None
        in_combat = bool((status and status[0] & StatusFlags.IN_COMBAT)
                         or navigation and navigation.flags & TravelFlags.IN_COMBAT)
        if (self.require_prepared and not self.travel_only and life == self.recovery.life == LifeState.ALIVE
                and in_combat and now - self.recovery.life_at >= 1.8):
            if self.recovery_deferred_at is None:
                self.recovery_deferred_at = now
                ctx.log_event("quest_recovery_defense_first", {"id": navigation.quest_id})
            return False
        if self.recovery_deferred_at is not None:
            paused = now - self.recovery_deferred_at
            self.recovery.life_at += paused
            self.recovery.preparation.started += paused
            self.recovery_deferred_at = None
        if navigation and navigation.camera is not None:
            self.camera.tick(ctx, now, navigation, background=True,
                             locked=life != LifeState.GHOST or not sample or sample.dialog)
        result = self.recovery.tick(ctx, now, navigation, ctx.heading(),
                                    lambda keys: self.set_travel_keys(ctx, keys), frontmost_game_bounds)
        if result == "done":
            self.recovery = None
            self.camera_restore_reason = "resurrection"
            self.phase = self.sample = self.defense = None
            self.trip = self.hunt = self.loot_sweep = None
            self.defense_loot.clear()
            self.defense_restore_until = None
            self.arrived = self.defense_had_output = False
            self.last_sequence = self.nav_sequence = None
            self.last_frame = self.last_target = now
            self.unusable_since = None
            self.last_action = 0
            if self.round_advance_at is not None:
                self.round_advance_at = now
            if self.followup_started is not None:
                self.followup_started = now
            ctx.stats["quest_recoveries"] = ctx.stats.get("quest_recoveries", 0) + 1
            ctx.log_event("quest_recovery_complete", {"id": self.steps[self.index].quest_id})
        elif result:
            self.stop(ctx, "quest_recovery_" + result)
        return True

    def maintain_pet(self, ctx, now, navigation, frame, in_combat):
        sample = navigation.recovery if navigation else None
        if (not self.require_prepared or self.travel_only or sample is None or sample.life != LifeState.ALIVE
                or not sample.pet_expected or navigation.quest_id != frame.quest_id
                or frame.quest_id != self.steps[self.index].quest_id or navigation.phase != frame.state
                or not navigation.task & TaskFlags.PREPARED or frame.state not in {QuestState.ACTIVE, QuestState.READY}):
            return False
        if (in_combat or frame.dialogue != Dialogue.CLOSED or navigation.flags & (
            TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
        )):
            if self.pet_preparation and self.pet_pause_at is None:
                self.pet_pause_at = now
            return False
        if self.pet_preparation is None:
            if (sample.pet not in {PetState.DEAD, PetState.ABSENT} or navigation.flags & TravelFlags.CASTING
                    or (sample.pet == PetState.ABSENT and navigation.flags & TravelFlags.MOUNTED)
                    or (self.object_click_at is not None and now - self.object_click_at < 5)):
                return False
            self.pet_preparation = ClassPreparation(now, pet_only=True)
            self.pet_dismount_at = None
            self.trip = None
            self.cancel_scene_requests()
            self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None
            ctx.log_event("quest_pet_missing", {"id": navigation.quest_id, "state": sample.pet.name,
                                                "revive_verified": sample.revive, "summon_verified": sample.summon})
        if self.pet_pause_at is not None:
            self.pet_preparation.started += now - self.pet_pause_at
            self.pet_pause_at = None
        self.set_forward(ctx, False)
        if navigation.flags & TravelFlags.MOUNTED:
            if self.pet_dismount_at is not None:
                if now - self.pet_dismount_at >= 4:
                    self.stop(ctx, "quest_pet_dismount_unconfirmed")
                return True
            key = navigation.sky_dismount_key
            if not key and navigation.flags & TravelFlags.KEYS_VERIFIED and not navigation.flags & TravelFlags.SKYRIDING:
                key = "8"
            if not key:
                self.stop(ctx, "quest_pet_dismount_binding_unverified")
                return True
            self.pet_dismount_at = now
            self.execution_started = True
            ctx.input.tap(key)
            return True
        self.execution_started = True
        result = self.pet_preparation.tick(ctx, now, navigation)
        if result == "done":
            self.pet_preparation = None
            ctx.log_event("quest_pet_ready", {"id": navigation.quest_id})
        elif result:
            self.stop(ctx, "quest_" + result)
        return True

    def mend_pet(self, ctx, now, nav, in_combat):
        pet = nav.recovery
        if not pet or pet.life != LifeState.ALIVE or pet.pet != PetState.ALIVE or not pet.mend_verified:
            self.pet_mend_receipt = None
            return False
        if self.pet_mend_receipt is not None:
            if pet.mend_epoch != self.pet_mend_receipt:
                ctx.log_event("quest_pet_mend_confirmed", {"id": nav.quest_id, "health": pet.pet_health})
                self.pet_mend_receipt = None
            elif now - self.pet_mend_at >= 3:
                ctx.log_event("quest_pet_mend_unconfirmed", {"id": nav.quest_id})
                self.pet_mend_receipt = None
        busy = TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.MOUNTED | TravelFlags.CASTING | TravelFlags.SWIMMING
        if (nav.flags & busy or pet.mend_unusable or pet.mend_active or now - self.pet_mend_at < 11
                or (pet.pet_health is None and not in_combat)
                or (pet.pet_health is not None and pet.pet_health > (75 if in_combat else 60))):
            return False
        self.pet_mend_at, self.pet_mend_receipt = now, pet.mend_epoch
        self.execution_started = True
        ctx.log_event("quest_pet_mend", {"id": nav.quest_id, "health": pet.pet_health})
        ctx.input.tap("f7")
        return True

    def remember_safe_ground(self, nav, in_combat, now):
        if not nav.position or not nav.world_size:
            return
        router = GridRouter(*nav.world_size)
        self.danger_zones = [(point, expires) for point, expires in self.danger_zones if expires > now]
        if in_combat or nav.flags & (TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING):
            return
        if any(router.distance(point, nav.position) < 20 for point, _ in self.danger_zones):
            return
        if nav.recovery and nav.recovery.patrol_id:
            return
        if not self.safe_ground or router.distance(self.safe_ground[-1], nav.position) >= 5:
            if self.safe_ground and router.distance(self.safe_ground[-1], nav.position) <= 9 and self.ground_router:
                self.ground_router[1].remember_passage((self.safe_ground[-1], nav.position))
            self.safe_ground.append(nav.position)
            del self.safe_ground[:-24]

    def combat_side_step(self, ctx, nav, sample):
        if (not sample or sample.facing is None or not nav.strafe_verified or not nav.position
                or not nav.world_size or nav.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING
                                                      | TravelFlags.FALLING | TravelFlags.CASTING)):
            return None
        router = GridRouter(*nav.world_size)
        px, py = router.metric(nav.position)
        facing = sample.facing
        flip = 1 if ctx.cfg.navigation.facing_convention == "ccw_west" else -1
        for past in reversed(self.safe_ground):
            x, y = router.metric(past)
            dx, dy = x - px, y - py
            forward = -flip * math.sin(facing) * dx - math.cos(facing) * dy
            side = -flip * math.cos(facing) * dx + math.sin(facing) * dy
            if 1.5 <= abs(side) <= 6 and abs(forward) <= 1.5:
                return "q" if side > 0 else "e"
        return None

    def start_patrol_avoidance(self, ctx, now, nav, target_id, danger):
        if now < self.patrol_seen_until.get(target_id, 0):
            return False
        self.patrol_seen_until[target_id] = now + 10
        router = GridRouter(*nav.world_size)
        waypoint = next((p for p in reversed(self.safe_ground) if 12 <= router.distance(p, nav.position) <= 45), None)
        self.danger_zones.append((nav.position, now + 45))
        self.set_forward(ctx, False)
        ctx.log_event("quest_patrol_avoid", {"id": nav.quest_id, "target": target_id,
                      "danger": int(danger), "waypoint": waypoint})
        if self.hunt:
            self.hunt.fight = None
            self.hunt.reset_search()
            if self.search_quest_area(ctx, nav):
                return True
        if waypoint is None:
            if nav.recovery and nav.recovery.patrol_id:
                self.patrol_wait_until = now + 8
                return True
            return False
        self.avoidance_trip = QuestTrip(nav.world_size, waypoint, ctx.cfg.navigation.facing_convention,
                                        walk_only=not bool(nav.flags & TravelFlags.MOUNTED))
        self.trip = None
        return True

    def avoid_patrol(self, ctx, now, nav, heading):
        command = self.avoidance_trip.step(now, nav, heading, heading_age=ctx.heading_age())
        self.apply_travel_command(ctx, command)
        if command.outcome:
            self.avoidance_trip = None
            if command.outcome != "arrived":
                ctx.log_event("quest_patrol_avoid_unconfirmed", {"id": nav.quest_id, "reason": command.outcome})
            if nav.recovery and nav.recovery.patrol_id:
                self.patrol_wait_until = now + 8

    def activity_reason(self, now, navigation):
        if self.building_access and self.building_access.locked:
            return 'building_' + self.building_access.state
        if navigation is None and self.auto_route:
            return "telemetry_wait"
        if self.unusable_since is not None:
            return self.unusable_reason or "telemetry_wait"
        if self.recovery is not None:
            return "death_recovery"
        if self.pet_preparation is not None:
            return "class_preparation"
        if navigation and navigation.flags & TravelFlags.IN_COMBAT:
            return "combat"
        if self.terrain:
            cloud = getattr(self.terrain, "model_detour", None)
            return "terrain_model_wait" if (getattr(self.terrain, "request", None) is not None
                or cloud is not None and cloud.request is not None) else "terrain_observation_or_move"
        if self.object_scene is not None or getattr(self.planner, "future", None) is not None:
            return "model_wait"
        if self.camera.pending:
            return "camera_receipt_wait"
        if self.npc_step is not None:
            return "npc_movement_receipt_wait"
        if self.npc_alignment is not None:
            return "npc_heading_alignment"
        if self.fetch_attempt is not None or self.loot_attempt is not None or self.loot_sweep is not None:
            return "loot_receipt_wait"
        if self.suspend_request is not None or self.round_advance_at is not None:
            return "suspend_camera_wait" if self.suspend_camera is not None else "round_receipt_wait"
        if navigation and navigation.flags & TravelFlags.CASTING:
            return "casting"
        if self.travel_keys or self.forward:
            return "travel"
        if self.npc_ground_wait is not None:
            return "ground_settle_wait"
        if self.patrol_wait_until is not None:
            return "patrol_wait"
        if now - self.last_action < 2:
            return "action_receipt_or_cooldown"
        return "unclassified_wait"

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
                if self.execution_started or self.had_focus or now - self.last_frame > focus_wait:
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
        self.combat_frame = None
        frame_image = nav_image = None
        if isinstance(ctx.capture, ScreenCapture):
            quest_rect = quest_region(channel.x, channel.y)
            combat_rect = combat_region(channel.x, channel.y)
            nav_rect = travel_region(channel.x, channel.y)
            left, top = channel.x, min(nav_rect[1], combat_rect[1]) if self.auto_route else combat_rect[1]
            right = nav_rect[0] + nav_rect[2] if self.auto_route else left + REGION_W
            if self.auto_route and self.nav3d_enabled(ctx):
                from ..perception.nav3d_channel import nav3d_region
                xyz_rect = nav3d_region(channel.x, channel.y)
                top, right = min(top, xyz_rect[1]), max(right, xyz_rect[0] + xyz_rect[2])
            snapshot = ctx.capture.grab(left, top, right - left, channel.y - top)

            def strip(rect):
                x, y, width, height = rect
                return snapshot[y - top:y - top + height, x - left:x - left + width]

            self.combat_frame, frame_image = strip(combat_rect), strip(quest_rect)
            if self.auto_route:
                nav_image = strip(nav_rect)
                if self.nav3d_enabled(ctx):
                    from ..perception.nav3d_channel import decode_nav3d
                    self.nav3d_sample = decode_nav3d(strip(xyz_rect))
        status = ctx.channel.status(max_age_s=1.5)
        navigation = None
        if self.auto_route:
            if nav_image is None:
                nav_image = ctx.capture.grab(*travel_region(channel.x, channel.y))
            navigation = decode_travel(nav_image)
            self.camera.guard(ctx, now, navigation)
        if frame_image is None:
            frame_image = ctx.capture.grab(*quest_region(channel.x, channel.y))
        frame = decode_quest(frame_image)
        observed_combat = decode_combat(self.combat_frame) if self.combat_frame is not None else None
        self.observation = self.observations.snapshot(now, frame, navigation, observed_combat)
        if navigation and navigation.addon_build != getattr(self, "loaded_addon_build", None):
            self.loaded_addon_build = navigation.addon_build
            ctx.log_event("quest_addon_loaded", {"build": navigation.addon_build,
                "protocol": navigation.protocol_version, "sequence": navigation.sequence,
                "extension_schema": navigation.extension_schema})
        if now - self.observation_logged_at >= 1:
            self.observation_logged_at = now
            ctx.log_event("quest_observation", {
                "activity": self.activity_reason(now, navigation),
                "id": navigation.quest_id if navigation else step.quest_id,
                "phase": frame.state.name if frame else None,
                "navigation": asdict(navigation) if navigation else None,
                "combat": asdict(observed_combat) if observed_combat else None,
                "channel_ages": {name: round(now - getattr(self.observation, name).changed_at, 3)
                                 if getattr(self.observation, name).value is not None else None
                                 for name in ("quest", "navigation", "combat")},
                "pending": {"terrain": getattr(self.terrain, "phase", None),
                            "building": getattr(self.building_access, "state", None),
                            "npc_step": self.npc_step is not None,
                            "travel": getattr(self.trip, "mode", None)},
            })
        if self.auto_route and self.nav3d_enabled(ctx):
            # One owner for all ground movement, including building/approach.
            # Shadow is intercepted before recovery, camera, combat or NPC taps.
            if (frame is None or navigation is None or frame.quest_id != navigation.quest_id
                    or frame.state != navigation.phase or frame.map_id != navigation.map_id):
                return self.stop(ctx, "nav3d_incoherent_quest")
            return self.travel_nav3d(ctx, now, navigation)
        self.ground_trace.observe(navigation, now, self.travel_keys)
        if status and status[0] & (StatusFlags.IN_COMBAT | StatusFlags.DEAD | StatusFlags.GHOST):
            self.ground_trace.clear()
        if self.auto_route and self.recover(ctx, now, navigation, status):
            return
        if frame is None:
            self.set_forward(ctx, False)
            if now - self.last_frame > 3:
                return self.stop(ctx, "quest_channel_lost")
            return
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
                ctx.log_event("quest_round_stage", {"stage": self.round_stage.name, "id": step.quest_id,
                                                    "pending_count": navigation.round_pending_count})
            if navigation.sequence != self.nav_sequence:
                self.nav_sequence, self.nav_at = navigation.sequence, now
            elif now - self.nav_at > 1.2:
                return self.stop(ctx, "quest_navigation_channel_stale")
            self.round_navigation = navigation
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
        pos, heading = ctx.pos(), ctx.heading()
        unsafe = UNSAFE_FLAGS & ~StatusFlags.FLYING if self.auto_route else UNSAFE_FLAGS
        if status and status[0] & unsafe:
            if navigation and navigation.recovery and navigation.recovery.life == LifeState.ALIVE:
                self.set_forward(ctx, False)
                return
            return self.stop(ctx, "quest_unsafe_character_state")
        if status is None or pos is None or heading is None or self.sample is None:
            return self.wait_for_data(ctx, now, "quest_player_telemetry_unavailable")
        if self.unusable_reason == "quest_player_telemetry_unavailable":
            self.unusable_since, self.unusable_reason = None, None
        sample = self.sample
        objective_index = navigation.objective_index if navigation and navigation.phase == sample.state else None
        progress = (navigation.objective_progress if objective_index and sample.state == QuestState.ACTIVE
                    else sample.progress)
        resource = navigation.objective_resource if objective_index else sample.objective == Objective.RESOURCE
        measurable = progress[1] > 0 if objective_index else sample.objective in {Objective.COUNTED, Objective.RESOURCE}
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
        if self.suspend_request and not in_combat and self.defense is None and self.loot_sweep is None:
            return self.suspend_task(ctx, now, navigation, in_combat)
        prepared = (navigation and navigation.quest_id == frame.quest_id == step.quest_id
                    and navigation.map_id == self.map_id and navigation.phase == frame.state
                    and navigation.task & TaskFlags.PREPARED and not self.travel_only)
        if prepared:
            self.remember_safe_ground(navigation, in_combat, now)
            if (frame.dialogue == Dialogue.CLOSED and self.loot_sweep is None
                    and self.object_click_at is None and not (self.defense and self.defense.retreat)
                    and self.mend_pet(ctx, now, navigation, in_combat)):
                return
            if not in_combat and self.avoidance_trip and frame.dialogue == Dialogue.CLOSED:
                return self.avoid_patrol(ctx, now, navigation, heading)
        if (navigation and navigation.map_id == self.map_id and navigation.quest_id == frame.quest_id == step.quest_id
                and navigation.phase == frame.state and frame.state in {QuestState.PENDING, QuestState.ACTIVE, QuestState.READY}
                and (not self.require_prepared or navigation.task & TaskFlags.PREPARED)
                and self.restore_camera(ctx, now, navigation, dialogue_open=frame.dialogue != Dialogue.CLOSED,
                                        in_combat=in_combat)):
            return
        if self.maintain_pet(ctx, now, navigation, frame, in_combat):
            return
        if not in_combat:
            self.corpse_wait_at = None
        if self.loot_sweep is not None:
            self.set_forward(ctx, False)
            if not in_combat and navigation and navigation.task & TaskFlags.PREPARED:
                if self.loot_sweep.tick(ctx, now, navigation):
                    return
                self.loot_sweep = None
            elif not in_combat:
                return self.wait_for_data(ctx, now, "quest_loot_preparation_unavailable")
        if (sample.state == QuestState.READY and self.hunt and navigation and not self.travel_only
                and navigation.quest_id == step.quest_id and navigation.phase == sample.state
                and frame.dialogue == Dialogue.CLOSED and not in_combat):
            combat = self.read_combat(ctx)
            if self.loot_corpse(ctx, now, sample.progress[0], combat, navigation):
                return
        if in_combat and self.require_prepared and self.auto_route and not self.travel_only:
            if self.building_access:
                self.building_access.interrupt()
            if not (navigation and navigation.quest_id == step.quest_id and navigation.phase == sample.state
                    and navigation.task & TaskFlags.PREPARED):
                return self.wait_for_data(ctx, now, "quest_defense_preparation_unavailable")
            if navigation.sequence != self.nav_sequence:
                self.nav_sequence, self.nav_at = navigation.sequence, now
            if now - self.nav_at > 1.2:
                return self.stop(ctx, "quest_navigation_channel_stale")
            combat = self.read_combat(ctx)
            fight = self.hunt and self.hunt.fight
            if (self.defense is None and self.arrived and fight and fight.presses
                    and self.phase == QuestState.ACTIVE and frame.dialogue == Dialogue.CLOSED
                    and (combat is None or not combat.target_id or combat.target_dead)
                    and not (navigation.recovery and navigation.recovery.attacker_count)):
                if self.corpse_wait_at is None:
                    self.corpse_wait_at = now
                    ctx.log_event("quest_corpse_wait", {"id": step.quest_id, "target": fight.target_id})
                if now - self.corpse_wait_at < 2.5:
                    self.set_forward(ctx, False)
                    return
            else:
                self.corpse_wait_at = None
            continuing_hunt = (not self.suspend_request and self.defense is None and self.arrived
                               and self.phase == sample.state == QuestState.ACTIVE
                               and frame.dialogue == Dialogue.CLOSED and combat and fight
                               and not combat.target_dead and not combat.target_danger
                               and combat.target_id == fight.target_id
                               and fight.state_epoch in {None, combat.state_epoch})
            if not continuing_hunt:
                return self.defend(ctx, now, navigation, frame.dialogue, combat)
        elif self.defense is not None:
            self.remember_defense_corpse(self.defense.fight, self.read_combat(ctx))
            ctx.log_event("quest_defense_resumed", {"id": step.quest_id})
            retreated = self.defense.retreat is not None
            self.phase_started += now - self.defense.started_at
            self.defense, self.unusable_since = None, None
            self.set_forward(ctx, False)
            if retreated:
                self.arrived = False
                if self.hunt:
                    self.hunt.fight, self.hunt.credit_deadline = None, None
                    self.hunt.reset_search()
                    self.search_quest_area(ctx, navigation)
            self.npc_step = self.npc_press_at = self.object_probe_sequence = None
            self.object_probe, self.object_points = 0, None
            if not self.arrived:
                self.trip = None
            if (not retreated and self.loot_sweep is None and self.defense_had_output and navigation
                    and navigation.sweep_ready and navigation.fetch_ready):
                self.defense_had_output = False
                self.loot_sweep = LootSweep(now)
                self.batch_for_fight = self.hunt.fight if self.hunt else None
                self.set_forward(ctx, False)
                self.loot_sweep.tick(ctx, now, navigation)
                return
            self.defense_had_output = False
        if not in_combat and frame.dialogue == Dialogue.CLOSED and self.finish_defense_loot(ctx, now, navigation):
            return
        if self.suspend_request and not in_combat:
            return self.suspend_task(ctx, now, navigation, in_combat)
        if self.round_stage != RoundStage.NONE:
            if self.round_stage == RoundStage.DONE:
                self.set_forward(ctx, False)
                pending_count = (navigation.round_pending_count if navigation.round_pending_count is not None
                                 else len(self.suspended_tasks))
                pending = self.suspended_tasks if pending_count else []
                inherited_count = max(0, pending_count - len(pending))
                ctx.stats["quests_pending"] = pending_count
                ctx.log_event("quest_round_summary", {
                    "pending": pending, "map": self.map_id,
                    "pending_count": pending_count, "pending_inherited_count": inherited_count,
                    "pending_count_verified": navigation.round_pending_count is not None,
                    "duration_s": round(now - self.run_started, 1),
                    "quests_done": ctx.stats.get("quests_done", 0),
                    "recoveries": ctx.stats.get("quest_recoveries", 0),
                })
                self.OUTCOME = "completed_with_pending" if pending_count else "completed"
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
            if sample.state != expected or navigation.round_blocked or navigation.current_suspended:
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
        targets = step.combat_targets if objective_index in (None, 0, 1) else frozenset()
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
            if sample.state == QuestState.ACTIVE and navigation.objectives_finished and not self.travel_only:
                if self.hunt and self.loot_corpse(ctx, now, progress[0], self.read_combat(ctx), navigation):
                    return
                return self.wait_for_data(ctx, now, "quest_ready_state_unconfirmed")
            if (sample.state == QuestState.ACTIVE and not self.travel_only and not targets
                    and step.quest_id not in ARRIVAL_QUESTS and not navigation.task & TaskFlags.USE_ITEM
                    and objective_action == ObjectiveAction.NONE):
                return self.wait_for_data(ctx, now, "quest_objective_profile_required")
        hunting = sample.state == QuestState.ACTIVE and not self.travel_only and (
            bool(targets) and objective_action in {ObjectiveAction.NONE, ObjectiveAction.COMBAT}
            or objective_action == ObjectiveAction.COMBAT
            or (objective_index == self.objective_index and self.phase == QuestState.ACTIVE
                and self.hunt is not None and objective_action == ObjectiveAction.NONE)
        )
        talk_objective = sample.state == QuestState.ACTIVE and objective_action == ObjectiveAction.NPC_TALK
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
            and objective_action not in {ObjectiveAction.ARRIVE, ObjectiveAction.NPC_TALK, ObjectiveAction.INTERACT}
            and not (objective_index and objective_action == ObjectiveAction.COMBAT and measurable)
        ):
            return self.wait_for_data(ctx, now, "quest_objective_unsupported")
        if hunting and not measurable:
            return self.wait_for_data(ctx, now, "quest_combat_needs_measurable_objective")
        if hunting and progress[0] >= progress[1] > 0:
            if resource and self.hunt and navigation and not in_combat:
                combat = self.read_combat(ctx)
                if self.loot_corpse(ctx, now, progress[0], combat, navigation):
                    return
            return self.wait_for_data(ctx, now, "quest_ready_state_unconfirmed")
        self.unusable_since = None
        if sample.state != self.phase or objective_index != self.objective_index:
            if (self.hunt and navigation and not in_combat and self.phase == sample.state == QuestState.ACTIVE
                    and self.loot_corpse(ctx, now, self.hunt.baseline, self.read_combat(ctx), navigation)):
                return
            self.cancel_scene_requests()
            if self.object_click_at is not None and sample.state == QuestState.READY:
                ctx.log_event("quest_object_progress", {"id": step.quest_id, "progress": sample.progress})
            self.phase, self.objective_index = sample.state, objective_index
            self.follower = RouteFollower(route, ctx.cfg.navigation, loop=False) if route else None
            self.trip, self.search_waypoint, self.searches = None, None, 0
            self.area_search = None
            self.loot_for_fight = None
            self.loot_attempt = None
            self.fetch_for_fight, self.fetch_attempt = None, None
            self.corpse_wait_at, self.corpse_approach = None, None
            self.corpse_restore = None
            self.arrived, self.attempts, self.action_signature = False, 0, None
            self.npc_press_at, self.npc_step, self.npc_steps = None, None, 0
            self.npc_rejoin = False
            self.npc_obstacle_attempts.clear()
            self.npc_range_wait_since = None
            self.npc_scan_started = self.npc_scan_aligned_at = None
            self.npc_scan_direction = 0
            self.npc_select_attempted = False
            self.npc_dialogue_retries, self.npc_dialogue_recovery_at = 0, None
            self.mount_clicked_at = None
            self.mount_key_attempted = self.mount_style_attempted = False
            self.phase_started = self.last_progress = now
            self.planned_phase = None
            self.progress = progress
            self.hunt = QuestHunt(targets, resource=resource) if hunting else None
            self.object_probe, self.object_probe_sequence = 0, None
            self.object_points = None
            self.object_target, self.object_targets_tried = None, []
            self.object_completed.clear()
            self.object_sightings.clear()
            self.object_lost_origin = None
            self.object_rescan_at = float("-inf")
            self.object_lost_revisited = False
            self.apr_waypoint = None
            self.object_check_origin = self.object_check_pending = None
            self.object_minimap_checked = False
            self.object_approach_yards = self.object_arrival_radius()
            self.object_click_at = None
            self.object_click_target = None
            self.object_click_attempts = 0
            ctx.log_event("quest_phase", {"id": step.quest_id, "kind": sample.kind.name, "state": self.phase.name,
                          "objective": objective_index, "action": objective_action.name, "progress": progress})
            if resource and self.phase == QuestState.ACTIVE:
                ctx.log_event("quest_resource_progress", {"id": step.quest_id, "percent": progress[0] / 10})
        if hunting and self.hunt is None:
            self.hunt = QuestHunt(targets, resource=resource)
        elif not hunting:
            self.hunt = None
        if now - self.phase_started > (600 if self.auto_route else 150):
            return self.stop(ctx, "quest_phase_timeout")

        if self.planner is not None and self.planned_phase != self.phase and not self.auto_route:
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
            if resource:
                state["resource_percent"] = 100 * sample.progress[0] / RESOURCE_PROGRESS_MAX
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
            if self.npc_detouring or self.npc_obstacle_reason or self.npc_ground_wait:
                self.cancel_scene_requests()
            if frame.quest_id != step.quest_id:
                return
            if talk_objective:
                if self.npc_press_at is None:
                    self.npc_press_at = now
                if now - self.npc_press_at >= 2.5:
                    return self.stop(ctx, "quest_objective_dialogue_choice_required")
                return  # Wait for the current objective or quest state to advance.
            allowed = PICKUP_DIALOGUE if self.phase == QuestState.PENDING else TURN_IN_DIALOGUE
            if self.phase == QuestState.ACTIVE or frame.dialogue_id != step.quest_id or frame.dialogue not in allowed:
                if (self.phase in {QuestState.PENDING, QuestState.READY} and navigation and not in_combat
                        and navigation.task & TaskFlags.PREPARED and self.npc_press_at is not None):
                    if self.npc_dialogue_recovery_at is not None:
                        if now - self.npc_dialogue_recovery_at < 3:
                            return
                    elif self.npc_dialogue_retries < 2 and now - self.npc_press_at < 5:
                        self.npc_dialogue_retries += 1
                        self.npc_dialogue_recovery_at = self.last_action = now
                        ctx.log_event("quest_npc_dialogue_recovery", {
                            "id": step.quest_id, "phase": self.phase.name,
                            "dialogue_id": frame.dialogue_id, "dialogue": frame.dialogue.name,
                        })
                        ctx.input.tap(step.dialog_key)
                        return
                return self.stop(ctx, "quest_dialogue_unexpected")
            self.npc_dialogue_recovery_at = None
            signature = (self.phase, frame.dialogue)
            if signature != self.action_signature:
                self.action_signature, self.attempts = signature, 0
            return self.act(ctx, step.dialog_key, now)
        if (navigation and not in_combat and not mount_tutorial and self.terrain is None
                and self.process_building_access(ctx,now,navigation,heading)):
            return
        if self.npc_dialogue_recovery_at is not None:
            self.npc_dialogue_recovery_at = self.npc_press_at = self.npc_step = None
            self.npc_range_wait_since = self.npc_scan_started = self.npc_scan_aligned_at = None
            self.npc_steps = self.npc_scan_direction = 0
            self.npc_select_attempted = False
        if (navigation and self.phase == QuestState.READY and self.action_signature
                and self.action_signature[1] in TURN_IN_DIALOGUE and now - self.last_action < 2.5):
            self.set_forward(ctx, False)
            return
        if mount_tutorial:
            self.set_forward(ctx, False)
            if frame.quest_id != step.quest_id:
                return
            return self.click_tutorial_mount(ctx, now, navigation, sample)
        if objective_action == ObjectiveAction.INTERACT and not self.travel_only:
            if self.object_click_at is not None and progress[0] > self.progress[0]:
                self.object_click_at = None
                self.object_lost_revisited = False
                self.search_waypoint = None
                if navigation.quest_id == 90895 and not navigation.objective_pin:
                    self.trip = None
                if navigation.quest_id != 90895:
                    self.searches = 0
                self.finish_object_approach()
                if navigation.quest_id == 90895:
                    if self.object_click_target is not None:
                        self.object_completed.append(self.object_click_target)
                    self.object_targets_tried = self.object_completed.copy()
                self.object_click_target = None
                self.progress, self.last_progress = progress, now
                ctx.log_event("quest_object_progress", {"id": step.quest_id, "progress": progress})
            if navigation.apr_waypoint and navigation.destination != self.apr_waypoint:
                if self.apr_waypoint is not None and self.object_click_at is None:
                    self.finish_object_approach()
                    self.search_waypoint, self.trip, self.arrived, self.searches = None, None, False, 0
                self.apr_waypoint = navigation.destination
            if (navigation.objective_pin and navigation.destination and self.arrived and self.trip
                    and self.object_click_at is None and self.object_target is None
                    and self.trip.router.distance(self.trip.destination, navigation.destination) > 1):
                self.search_waypoint, self.trip, self.arrived = None, None, False
                self.object_probe, self.object_probe_sequence = 0, None
                self.object_points = None
                self.object_approach_yards = self.object_arrival_radius()
            if (navigation.quest_id == 90895 and navigation.precise_object and not navigation.objective_pin
                    and not self.arrived and self.trip is None and self.search_waypoint is None
                    and self.object_target is None and navigation.position and navigation.destination
                    and navigation.world_size and not navigation.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING
                                                                          | TravelFlags.FALLING)
                    and GridRouter(*navigation.world_size).distance(navigation.position, navigation.destination)
                    <= CATAPULT_MARKER_AREA_YARDS):
                self.arrived = True
                ctx.log_event("quest_object_search_start_here", {"id": navigation.quest_id,
                                                                 "position": navigation.position})
        if (not self.arrived and not self.building_pending and self.hunt is not None and navigation and navigation.position
                and navigation.destination and navigation.world_size and self.search_waypoint is None
                and self.phase == QuestState.ACTIVE and not self.travel_only
                and not navigation.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING)):
            if inside_quest_search_area(navigation):
                self.set_forward(ctx, False)
                self.arrived, self.trip = True, None
                ctx.log_event("quest_hunt_start_here", {"position": navigation.position,
                    "source": "native_quest_blob" if navigation.search_area is not None else "bounded_marker_search"})
                return
        if not self.arrived:
            if navigation is not None:
                grounded = not navigation.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING)
                confirmed_npc_range = all(navigation.task & flag for flag in (
                    TaskFlags.NPC_SELECTED, TaskFlags.NPC_RANGE_KNOWN, TaskFlags.NPC_WITHIN_5_YARDS))
                if ((self.phase in {QuestState.PENDING, QuestState.READY} or talk_objective)
                        and not self.travel_only and not self.building_pending and grounded and (not self.npc_rejoin or confirmed_npc_range)
                        and navigation.task & (TaskFlags.NPC_CANDIDATE | TaskFlags.NPC_SELECTED)):
                    self.set_forward(ctx, False)
                    self.arrived, self.last_progress = True, now
                    self.npc_rejoin = False
                else:
                    return self.travel(ctx, now, navigation, heading)
            else:
                return self.walk(ctx, now, pos, heading)
        if frame.quest_id != step.quest_id:
            self.set_forward(ctx, False)
            return
        if self.phase == QuestState.ACTIVE:
            if talk_objective and not self.travel_only:
                return self.interact_quest_target(ctx, now, navigation, heading)
            if self.hunt is not None:
                if resource and progress[0] > self.progress[0]:
                    self.progress, self.last_progress = progress, now
                    ctx.log_event("quest_resource_progress", {"id": step.quest_id, "percent": progress[0] / 10})
                if (resource and not in_combat and navigation and navigation.position and navigation.destination
                        and navigation.world_size and not (self.hunt.fight and self.hunt.fight.presses)
                        and not inside_quest_search_area(navigation)):
                    self.hunt.fight = None
                    self.hunt.reset_search()
                    self.set_forward(ctx, False)
                    if not self.search_quest_area(ctx, navigation):
                        return self.stop(ctx, "quest_resource_search_area_exhausted")
                    return
                return self.hunt_tick(ctx, now, progress[0], in_combat, navigation)
            if progress[0] > self.progress[0]:
                self.progress, self.last_progress, self.attempts = progress, now, 0
            if navigation and navigation.task & TaskFlags.USE_ITEM:
                if navigation.flags & (TravelFlags.CASTING | TravelFlags.MOUNTED):
                    return
                return self.act(ctx, "j", now)
            if objective_action == ObjectiveAction.INTERACT:
                return self.search_object(ctx, now, navigation, heading)
            if step.objective_key is None:
                if now - self.last_progress > 10:
                    return self.stop(ctx, "quest_objective_no_progress")
                return
            return self.act(ctx, step.objective_key, now)
        if navigation:
            return self.interact_quest_target(ctx, now, navigation, heading)
        return self.act(ctx, "f", now)

    def interact_quest_target(self, ctx, now, navigation, heading):
        if self.action_signature != (self.phase, Dialogue.CLOSED):
            self.action_signature, self.attempts = (self.phase, Dialogue.CLOSED), 0
        if not navigation.interact_verified:
            return self.stop(ctx, "quest_interact_binding_unverified")
        if not navigation.task & TaskFlags.PREPARED:
            return self.stop(ctx, "quest_automatic_not_prepared")
        if not self.npc_ground_ready(ctx, now, navigation):
            return
        range_receipt = TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
        if navigation.npc_range_supported and navigation.task & range_receipt == range_receipt:
            # A fresh native range receipt outranks the unfinished detour. It
            # confirms approach only; interaction/quest completion still need receipts.
            if self.terrain:
                self.terrain.close()
                self.terrain = None
            self.npc_detouring = self.npc_rejoin = False
            self.npc_obstacle_reason = None
            self.set_travel_keys(ctx, frozenset())
            return self.interact_with_npc(ctx, now, navigation, heading)
        if self.npc_detouring:
            return self.tick_npc_detour(ctx, now, navigation, heading)
        if self.npc_obstacle_reason:
            return self.begin_npc_detour(ctx, now, navigation, self.npc_obstacle_reason)
        if self.npc_step is not None or navigation.task & TaskFlags.NPC_SELECTED:
            return self.interact_with_npc(ctx, now, navigation, heading)
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
        if self.defense is None:
            self.cancel_scene_requests()
            self.defense = QuestDefense(now)
            self.avoidance_trip = None
            self.patrol_wait_until = None
            if not (self.hunt and self.hunt.fight and self.hunt.fight.presses):
                self.hunt = None
            ctx.log_event("quest_defense_started", {
                "id": navigation.quest_id, "phase": navigation.phase.name, "flags": int(navigation.flags),
            })
        defense, flags = self.defense, navigation.flags
        airborne = flags & (TravelFlags.FLYING | TravelFlags.FALLING)
        if airborne or flags & TravelFlags.MOUNTED:
            self.set_forward(ctx, False)
            if airborne:
                if now - defense.started_at > 18:
                    return self.stop(ctx, "quest_defense_landing_unconfirmed")
                return
            if now - defense.dismount_at < 1.3:
                return
            if defense.dismount_attempts >= 2:
                return self.stop(ctx, "quest_defense_dismount_unconfirmed")
            key = navigation.sky_dismount_key
            if not key and flags & TravelFlags.KEYS_VERIFIED and not flags & TravelFlags.SKYRIDING:
                key = "8"
            if not key:
                return self.stop(ctx, "quest_defense_dismount_binding_unverified")
            defense.dismount_at = now
            defense.dismount_attempts += 1
            self.execution_started = True
            ctx.log_event("quest_defense_dismount", {"key": key, "attempt": defense.dismount_attempts})
            ctx.input.tap(key)
            return
        if defense.retreat is not None:
            return self.defense_retreat(ctx, now, navigation)
        if self.combat_adjusted is not None and self.hunt and self.combat_adjusted is self.hunt.fight:
            if now - self.hunt.fight.last_action < .75:
                self.set_forward(ctx, False)
                return
        self.set_forward(ctx, False)
        if flags & (TravelFlags.SWIMMING | TravelFlags.CASTING) or dialogue != Dialogue.CLOSED:
            combat = None
        if combat and str(combat.key) in self.quest_keys:
            return self.stop(ctx, "quest_combat_action_key_conflicts")
        if combat and combat.target_engaged and combat.target_id and not combat.target_dead:
            if defense.handoff_until:
                ctx.log_event("quest_attacker_handoff_selected", {"id": navigation.quest_id, "target": combat.target_id})
                defense.handoff_until = 0
            defense.handoff_probe = defense.handoff_scene = None
            defense.handoff_started = None
            defense.handoff_tried.clear()
        elif (combat and combat.key_verified and not combat.unsafe
              and self.handoff_attacker(ctx, now, navigation, defense)):
            return
        self.remember_defense_corpse(defense.fight, combat)
        status, key = defense.step(now, combat, navigation)
        if key:
            if status == "approach" and not navigation.flags & TravelFlags.KEYS_VERIFIED:
                return self.stop(ctx, "quest_navigation_bindings_unverified")
            self.execution_started = True
            event = "quest_defense_" + (status if status in {"turn", "approach"} else "action")
            ctx.log_event(event, {"id": navigation.quest_id, "key": key, "target": combat.target_id,
                                  "range": combat.in_range, "health": combat.target_health})
            if status == "action":
                self.defense_had_output = True
            tap_combat_key(ctx.input, status, key)
        elif status in {"no_progress", "no_cast_or_damage_receipt", "line_of_sight_blocked", "approach_timeout", "approach_blocked", "facing_turn_timeout",
                        "facing_unconfirmed", "facing_feedback_unavailable", "attacker_unconfirmed", "timeout",
                        "dangerous_attacker"}:
            side = None
            if not defense.repositioned and status in {"line_of_sight_blocked", "approach_timeout", "approach_blocked"}:
                side = self.combat_side_step(ctx, navigation, combat)
            if side is not None:
                defense.repositioned, defense.fight = True, None
                self.execution_started = True
                ctx.log_event("quest_defense_reposition", {"reason": status, "target": combat.target_id, "key": side})
                ctx.input.tap(side, hold_s=.18)
                return
            heading = ctx.heading()
            if (navigation.position is None or navigation.world_size is None or heading is None
                    or ctx.heading_age() > .75 or not flags & TravelFlags.KEYS_VERIFIED
                    or flags & (TravelFlags.SWIMMING | TravelFlags.CASTING) or dialogue != Dialogue.CLOSED):
                return self.stop(ctx, "quest_defense_" + status)
            convention = ctx.cfg.navigation.facing_convention
            router = GridRouter(*navigation.world_size)
            point = next((p for p in reversed(self.safe_ground) if 35 <= router.distance(p, navigation.position) <= 62), None)
            if point is None:
                return self.stop(ctx, "quest_defense_" + status)
            defense.retreat = QuestTrip(navigation.world_size, point, convention, walk_only=True)
            if self.ground_router:
                defense.retreat.router = self.ground_router[1]
            defense.retreat_at = now
            defense.retreat_origin = navigation.position
            self.danger_zones.append((navigation.position, now + 45))
            recovery = navigation.recovery
            if recovery and recovery.follow and recovery.pet == PetState.ALIVE:
                ctx.input.tap("f9")
            ctx.log_event("quest_defense_retreat", {"reason": status, "destination": point,
                          "target": combat.target_id if combat else None})
            return self.defense_retreat(ctx, now, navigation)
        elif status != "waiting":
            return self.stop(ctx, "quest_defense_" + status)

    def handoff_attacker(self, ctx, now, nav, defense):
        sample = nav.recovery
        if sample is None or not sample.attacker_scan_supported:
            return False
        if defense.handoff_started is None:
            defense.handoff_started = now
        if now - defense.handoff_started > 3:
            return False
        if now < defense.handoff_until:
            return True
        probe = defense.handoff_probe
        if probe:
            point, sequence, started = probe
            if now - started < .65:
                if nav.sequence != sequence and now - started >= .22 and sample.attacker_hovered:
                    actual = ctx.input.position()
                    if math.dist(actual, point) <= 4:
                        self.execution_started = True
                        ctx.input.click_ui()
                        defense.handoff_probe = None
                        defense.handoff_until = now + .45
                        ctx.log_event("quest_attacker_handoff_click", {"id": nav.quest_id, "point": point})
                return True
            defense.handoff_probe = None
        if len(defense.handoff_tried) >= 3:
            return False
        if not sample.attacker_cursor and ctx.input.name != "helper":
            return False
        def untried(point):
            return all(math.dist(point, old) > 16 for old in defense.handoff_tried)

        try:
            x, y, width, height = map(round, game_ui_bounds(frontmost_game_bounds(), nav.minimap))
            if sample.attacker_cursor:
                u, v = sample.attacker_cursor
                # The stock title bar is outside UIParent. Without a calibrated
                # minimap, the image coordinates are the safer click candidates.
                points = [(x + round(u * width), y + round(v * height))] if nav.minimap else []
                source = "nameplate_api"
            else:
                points = []
            points = [p for p in points if untried(p)]
            if not points and ctx.input.name == "helper":
                source = "nameplate_image"
                earlier = defense.handoff_scene
                if earlier and now - earlier[0] < .15:
                    return bool(earlier[1])
                observed = [(x + px, y + py) for px, py in hostile_nameplate_points(
                    ctx.capture.grab(x, y, width, height))]
                defense.handoff_scene = (now, observed)
                if not earlier or now - earlier[0] > 1:
                    return bool(observed)
                points = [p for p in observed if untried(p) and any(math.dist(p, old) <= 12 for old in earlier[1])]
        except (ValueError, CaptureError):
            return False
        if not points:
            return False
        point = points[0]
        defense.handoff_tried.append(point)
        defense.handoff_probe = point, nav.sequence, now
        self.execution_started = True
        ctx.input.move_to(*point, jitter_px=0)
        ctx.log_event("quest_attacker_handoff_probe", {"id": nav.quest_id, "source": source,
                      "point": point, "known_attackers": sample.attacker_count})
        return True

    def defense_retreat(self, ctx, now, navigation):
        if now - self.defense.retreat_at >= 24:
            return self.stop(ctx, "quest_defense_retreat_unconfirmed")
        heading = ctx.heading()
        if heading is None or navigation.flags & (TravelFlags.CASTING | TravelFlags.FALLING):
            self.set_forward(ctx, False)
            return
        command = self.defense.retreat.step(now, navigation, heading, heading_age=ctx.heading_age(), retreating=True)
        if command.outcome == "arrived":
            route = self.defense.retreat
            origin = self.defense.retreat_origin
            distance = route.router.distance(navigation.position, origin)
            if distance < 1:
                return self.stop(ctx, "quest_defense_retreat_no_displacement")
            point = next((past for past in reversed(self.safe_ground)
                          if distance + 6 < route.router.distance(past, origin)
                          and route.router.distance(past, navigation.position) <= 25), None)
            if point is not None:
                route.destination = point
                route.enter("choose", now)
                ctx.log_event("quest_defense_retreat_extend", {"id": navigation.quest_id, "destination": point})
        elif command.outcome:
            return self.stop(ctx, "quest_defense_retreat_" + command.outcome)
        self.execution_started = True
        self.apply_travel_command(ctx, command)

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

    def npc_ground_ready(self, ctx, now, nav):
        """A transient fall releases W first; only fresh grounded frames resume."""
        unsafe = nav.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING)
        keys = bool(nav.flags & TravelFlags.KEYS_VERIFIED)
        if unsafe or not keys:
            self.set_forward(ctx, False)
            self.camera.cancel(ctx, now, "npc_approach_state")
            if self.npc_ground_wait is None or unsafe != TravelFlags.FALLING or not keys:
                ctx.log_event("quest_npc_approach_state", {
                    "id": nav.quest_id, "phase": nav.phase.name, "sequence": nav.sequence,
                    "flags": int(nav.flags), "unsafe": [flag.name for flag in TravelFlags if flag & unsafe],
                    "keys_verified": keys, "position": nav.position, "height": nav.height,
                    "step": self.npc_steps, "selected": bool(nav.task & TaskFlags.NPC_SELECTED),
                })
            if not keys or unsafe != TravelFlags.FALLING:
                self.stop(ctx, "quest_turn_in_approach_unsafe")
                return False
            if self.npc_ground_wait is None:
                self.npc_ground_wait = (now, None, None)
                self.npc_obstacle_reason = "falling_during_approach"
                self.npc_step = self.npc_press_at = None
                if self.terrain:
                    self.terrain.close()
                    self.terrain = None
                    self.npc_detouring = False
            started, _, _ = self.npc_ground_wait
            self.npc_ground_wait = started, None, None
            if now - started >= 3:
                ctx.log_event("quest_npc_ground_wait_exhausted", {"id": nav.quest_id, "flags": int(nav.flags)})
                self.stop(ctx, "quest_npc_ground_not_stable")
            return False
        if self.npc_ground_wait is not None:
            self.set_forward(ctx, False)
            started, stable_at, sequence = self.npc_ground_wait
            if now - started >= 3:
                self.stop(ctx, "quest_npc_ground_not_stable")
                return False
            if nav.flags & (TravelFlags.CASTING | TravelFlags.IN_COMBAT):
                return False
            if stable_at is None:
                self.npc_ground_wait = started, now, nav.sequence
                return False
            if nav.sequence == sequence or now - stable_at < .3:
                return False
            self.npc_ground_wait = None
            ctx.log_event("quest_npc_ground_stable", {"id": nav.quest_id, "sequence": nav.sequence,
                                                     "position": nav.position})
        if nav.flags & (TravelFlags.CASTING | TravelFlags.IN_COMBAT):
            self.set_forward(ctx, False)
            self.npc_step = None
            return False
        return True

    def begin_npc_detour(self, ctx, now, nav, reason, *, movement_result=None):
        self.set_forward(ctx, False)
        self.camera.cancel(ctx, now, "npc_visual_probe")
        self.arrived, self.npc_rejoin = True, False
        self.npc_obstacle_reason = reason
        identity = (nav.quest_id, nav.map_id, nav.phase, nav.destination, nav.position, reason)
        pending = getattr(self, "npc_obstacle_feedback", None)
        if movement_result is not None:
            self.npc_obstacle_feedback = (identity, now, movement_result)
        elif pending and pending[0] == identity and 0 <= now-pending[1] <= 2:
            movement_result = pending[2]
        contact_confirmed = (movement_result is not None and movement_result.state == ActionState.RECOVERABLE
                             and movement_result.reason == "no_displacement")
        if not self.npc_ground_ready(ctx, now, nav):
            return
        if (nav.position is None or nav.destination is None or nav.world_size is None
                or nav.destination_map != nav.map_id):
            return self.stop(ctx, "quest_interact_position_unavailable")
        if (nav.task & TaskFlags.NPC_SELECTED and nav.task & TaskFlags.NPC_RANGE_KNOWN
                and nav.task & TaskFlags.NPC_WITHIN_5_YARDS and reason != "interaction_unconfirmed"):
            self.npc_obstacle_reason = None
            self.npc_step = self.npc_press_at = None
            return
        heading = ctx.heading()
        if heading is None or ctx.heading_age() > .55:
            return self.wait_for_data(ctx, now, "quest_player_telemetry_unavailable")
        cloud_available = cloud_route_available(getattr(ctx.cfg, "route_planner", None))
        segmented_config=getattr(ctx.cfg,'segmented_navigation',None)
        segmented_available=bool(segmented_config and segmented_config.enabled)
        if segmented_available:
            from ..control.segmented_detour import preflight
            try:
                preflight(segmented_config,nav)
            except (ValueError,OSError,KeyError) as exc:
                ctx.log_event('quest_segmented_preflight_failed',{'reason':str(exc)})
                return self.stop(ctx,'quest_segmented_setup_unavailable')
        if ctx.input.name != "helper" or (not contact_confirmed and not local_vision_available() and not cloud_available and not segmented_available):
            ctx.log_event("quest_npc_visual_unavailable", {"id": nav.quest_id, "trigger": reason,
                "input": ctx.input.name, "model_available": local_vision_available()})
            return self.stop(ctx, "quest_npc_visual_unavailable")
        # Use a finer local grid at an NPC; a six-yard cell can contain both
        # the barricade and the NPC's usable standing point.
        router = self.trip.router if self.trip and self.npc_obstacle_attempts else GridRouter(*nav.world_size, cell_size=2)
        if len(self.npc_obstacle_attempts) >= 2 or any(
                router.distance(nav.position, point) < 3 for point in self.npc_obstacle_attempts):
            ctx.log_event("quest_npc_detour_exhausted", {"id": nav.quest_id, "position": nav.position,
                                                      "attempts": len(self.npc_obstacle_attempts)})
            return self.stop(ctx, "quest_npc_route_unconfirmed")
        self.trip = QuestTrip(nav.world_size, nav.destination, ctx.cfg.navigation.facing_convention,
                              walk_only=True, approach_on_foot=True, arrival_radius=3, log_event=ctx.log_event)
        self.trip.router, self.trip.mode = router, "walk"
        router.block_ahead(nav.position, nav.destination)
        self.npc_obstacle_attempts.append(nav.position)
        self.npc_step = self.npc_press_at = None
        self.npc_detouring, self.npc_obstacle_reason = True, None
        self.npc_obstacle_feedback = None
        self.terrain = TerrainDetour(now, nav, heading, self.trip, getattr(self.planner, "client", None),
                                     ground_trace=self.ground_trace, player_identity=self.terrain_identity(now),
                                     contact_release=contact_confirmed, npc_bypass=contact_confirmed)
        if (cloud_available or segmented_available) and not contact_confirmed:
            self.terrain.begin_model_route(ctx, now, nav, heading, reason)
        ctx.log_event("quest_npc_visual_probe", {"id": nav.quest_id, "trigger": reason,
            "position": nav.position, "destination": nav.destination, "attempt": len(self.npc_obstacle_attempts),
            "contact_confirmed": contact_confirmed,
            "movement_feedback": movement_result.reason if movement_result else None})

    def tick_npc_detour(self, ctx, now, nav, heading):
        command = self.terrain.step(ctx, now, nav, heading)
        self.apply_travel_command(ctx, command)
        if command.outcome is None:
            return
        if getattr(self.terrain, "probe_view_used", False):
            self.camera_restore_reason = "terrain_finished"
        self.terrain.close()
        self.terrain, self.npc_detouring = None, False
        self.set_forward(ctx, False)
        ctx.log_event("quest_npc_visual_result", {"id": nav.quest_id, "result": command.outcome,
                                                 "position": nav.position})
        if command.outcome != "detoured":
            if command.outcome == "probe_telemetry_stale":
                return self.stop(ctx,"quest_navigation_channel_stale")
            if command.outcome.startswith("escape_"):
                return self.stop(ctx, "quest_npc_" + command.outcome)
            if command.outcome in {"camera_composition_unresolved", "camera_restore_unconfirmed"}:
                return self.stop(ctx, "quest_npc_" + command.outcome)
            return self.stop(ctx, "quest_npc_player_origin_unresolved" if command.outcome == "player_origin_unresolved"
                             else "quest_npc_route_unconfirmed")
        self.trip.detours = 0
        self.trip.stall_position, self.trip.stall_count = None, 0
        self.trip.enter("choose", now)
        self.arrived, self.npc_rejoin = False, True
        self.npc_step = self.npc_press_at = None
        self.npc_steps = self.npc_scan_direction = 0
        self.npc_scan_started = self.npc_scan_aligned_at = self.npc_range_wait_since = None
        self.action_signature, self.attempts = None, 0
        self.last_progress = now

    def align_npc_approach(self, ctx, now, nav, heading):
        """The marker is a steering candidate, never proof of NPC arrival."""
        identity = (nav.quest_id, nav.map_id, nav.phase, nav.destination)
        if self.npc_alignment is None or self.npc_alignment[0] != identity:
            self.npc_alignment = identity, now
            self.npc_alignment_sequence = None
        if now - self.npc_alignment[1] >= 15:
            self.stop(ctx, "quest_npc_alignment_timeout")
            return False
        if heading is None or ctx.heading_age() > .55:
            self.wait_for_data(ctx, now, "quest_player_telemetry_unavailable")
            return False
        if not nav.destination or nav.destination_map != nav.map_id:
            self.stop(ctx, "quest_interact_position_unavailable")
            return False
        trip = QuestTrip(nav.world_size, nav.destination, ctx.cfg.navigation.facing_convention)
        distance = trip.router.distance(nav.position, nav.destination)
        if distance <= 1.5:
            # Reaching the marker without a real range receipt cannot justify
            # walking through it and away, or calling it a physical obstruction.
            self.stop(ctx, "quest_npc_marker_range_unconfirmed")
            return False
        if self.npc_alignment_sequence is not None and (
                nav.sequence == self.npc_alignment_sequence or now - self.npc_turn_at < TURN_SETTLE
                or now - ctx.heading_age() < self.npc_turn_at + .2):
            return False
        error = trip.turn_error(nav.position, nav.destination, heading)
        turn = trip.turn_keys(error, math.radians(12))
        if turn:
            if now - self.npc_turn_at >= TURN_INTERVAL:
                self.npc_turn_at, self.npc_alignment_sequence = now, nav.sequence
                key = next(iter(turn))
                ctx.input.tap(key, hold_s=.06)
                ctx.log_event("quest_npc_align", {"id": nav.quest_id, "key": key,
                    "heading_error_deg": round(math.degrees(error), 1), "marker_yards": round(distance, 2),
                    "sequence": nav.sequence})
            return False
        self.npc_alignment = None
        return True

    def replan_npc_approach(self, ctx, now, nav, before, after):
        identity = (nav.quest_id, nav.map_id, nav.phase, nav.destination)
        attempt = self.npc_direction_retries.get(identity, 0) + 1
        self.npc_direction_retries[identity] = attempt
        self.npc_step = self.npc_press_at = self.npc_alignment = None
        self.set_travel_keys(ctx, frozenset())
        ctx.log_event("quest_npc_approach_replan", {"id": nav.quest_id, "attempt": attempt,
            "reason": "marker_distance_not_decreasing", "before_yards": before, "after_yards": after})
        if attempt > 2:
            return self.stop(ctx, "quest_npc_approach_direction_unconfirmed")
        self.trip = QuestTrip(nav.world_size, nav.destination, ctx.cfg.navigation.facing_convention,
                              walk_only=True, approach_on_foot=True, arrival_radius=3, log_event=ctx.log_event)
        self.arrived, self.npc_rejoin, self.npc_steps = False, True, 0
        self.last_progress = now

    def interact_with_npc(self, ctx, now, navigation, heading):
        selected = bool(navigation.task & TaskFlags.NPC_SELECTED)
        range_known = bool(navigation.task & TaskFlags.NPC_RANGE_KNOWN)
        within_range = range_known and bool(navigation.task & TaskFlags.NPC_WITHIN_5_YARDS)
        if not self.npc_ground_ready(ctx, now, navigation):
            return
        if not navigation.npc_range_supported:
            return self.stop(ctx, "quest_turn_in_range_addon_required")
        if (navigation.position is None or navigation.world_size is None) and not (selected and within_range):
            return self.stop(ctx, "quest_interact_position_unavailable")
        identity = (navigation.quest_id, navigation.map_id, int(navigation.phase), "npc", selected,
                    navigation.destination)
        observation = ApproachObservation(identity, navigation.sequence, within_range if range_known else None,
                                          selected, navigation.position, navigation.world_size)
        if self.npc_step is not None:
            origin = self.npc_step.origin
            result = self.npc_step.poll(now, observation)
            self.set_travel_keys(ctx, frozenset({"w"}) if result.hold_forward else frozenset())
            if result.state == ActionState.WAITING:
                return
            ctx.log_event("quest_turn_in_approach_result", {"id": navigation.quest_id,
                "moved_yards": result.moved_yards, "reason": result.reason, "sequence": navigation.sequence})
            if result.state == ActionState.CANCELLED:
                self.npc_step = None
                return self.stop(ctx, "quest_turn_in_npc_lost")
            if result.state == ActionState.RECOVERABLE:
                self.npc_step = None
                if result.reason == "feedback_stale":
                    return self.stop(ctx, "quest_navigation_channel_stale")
                if result.reason in {"movement_evidence_missing", "displacement_unexpected"}:
                    return self.stop(ctx, "quest_navigation_position_invalid")
                return self.begin_npc_detour(ctx, now, navigation, "approach_no_displacement", movement_result=result)
            if not within_range and navigation.destination and origin.position:
                router = GridRouter(*navigation.world_size)
                before = router.distance(origin.position, navigation.destination)
                after = router.distance(navigation.position, navigation.destination)
                ctx.log_event("quest_npc_distance_feedback", {"id": navigation.quest_id,
                    "before_yards": before, "after_yards": after, "sequence": navigation.sequence})
                if after >= before - .15:
                    return self.replan_npc_approach(ctx, now, navigation, before, after)
            self.npc_step, self.npc_press_at = None, None
        self.set_travel_keys(ctx, frozenset())
        if navigation.flags & TravelFlags.CASTING:
            return
        if not range_known and self.npc_steps == 0 and self.npc_press_at is None:
            if self.npc_range_wait_since is None:
                self.npc_range_wait_since = now
            if now - self.npc_range_wait_since < 1:
                return
        if within_range:
            if self.npc_press_at is None or now - self.npc_press_at >= 1.2:
                if self.npc_interaction_attempts.get(identity, 0) >= 3:
                    return self.begin_npc_detour(ctx, now, navigation, "interaction_unconfirmed")
                self.npc_interaction_attempts[identity] = self.npc_interaction_attempts.get(identity, 0) + 1
                self.npc_press_at = self.last_action = now
                ctx.log_event("quest_action", {"id": navigation.quest_id, "key": "f"})
                ctx.input.tap("f")
        elif self.npc_press_at is None or now - self.npc_press_at >= 1.2:
            if self.npc_steps >= 3:
                if not range_known:
                    return self.stop(ctx, "quest_turn_in_range_unknown")
                if not within_range:
                    return self.begin_npc_detour(ctx, now, navigation, "range_not_reached")
                return self.begin_npc_detour(ctx, now, navigation, "interaction_unconfirmed")
            if not self.align_npc_approach(ctx, now, navigation, heading):
                return
            self.npc_steps += 1
            self.npc_step = ApproachAttempt(ApproachGoal("npc", identity), now, observation)
            ctx.log_event("quest_turn_in_approach", {
                "id": navigation.quest_id, "step": self.npc_steps,
                "range_known": range_known, "within_five_yards": within_range,
            })
            self.set_travel_keys(ctx, frozenset({"w"}))

    def search_quest_npc(self, ctx, now, navigation, heading):
        if not self.npc_ground_ready(ctx, now, navigation):
            return
        if not (navigation.flags & TravelFlags.KEYS_VERIFIED and navigation.position
                and navigation.destination and navigation.world_size):
            return self.stop(ctx, "quest_interact_position_unavailable")
        if self.npc_scan_started is None:
            self.npc_scan_started, self.npc_scan_heading = now, heading
            ctx.log_event("quest_npc_search", {
                "id": navigation.quest_id, "phase": self.phase.name,
                "npc_configured": bool(navigation.task & TaskFlags.NPC_CONFIGURED),
                "candidate": bool(navigation.task & TaskFlags.NPC_CANDIDATE),
                "selected": bool(navigation.task & TaskFlags.NPC_SELECTED),
                "marker_yards": round(GridRouter(*navigation.world_size).distance(
                    navigation.position, navigation.destination), 1),
            })
        if now - self.npc_scan_started > 35 or self.npc_scan_direction >= len(NPC_SCAN_ANGLES):
            if self.phase == QuestState.PENDING:
                reason = ("quest_pickup_npc_selection_unconfirmed" if self.npc_select_attempted
                          else "quest_pickup_npc_not_found")
            elif self.phase == QuestState.READY:
                reason = ("quest_turn_in_npc_selection_unconfirmed" if self.npc_select_attempted
                          else "quest_turn_in_npc_not_in_interact_range")
            else:
                reason = "quest_objective_npc_not_found"
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
        self.set_travel_keys(ctx, frozenset())
        if turn:
            self.npc_scan_aligned_at = None
            if now - self.npc_turn_at >= TURN_INTERVAL:
                self.npc_turn_at = now
                ctx.input.tap(next(iter(turn)), hold_s=.06)
        elif now - self.npc_turn_at < TURN_SETTLE:
            return
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
        self.object_check_origin = self.object_check_pending = None
        self.object_lost_origin = None
        self.object_minimap_checked = False
        self.object_approach_yards = self.object_arrival_radius()
        self.object_click_attempts = 0

    def object_arrival_radius(self):
        return CATAPULT_APPROACH_YARDS if self.steps[self.index].quest_id == 90895 else OBJECT_APPROACH_YARDS

    def start_object_route(self, point):
        self.object_target = point
        self.search_waypoint, self.trip, self.arrived = None, None, False
        self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None

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
        catapult = navigation.quest_id == 90895
        marker_limit = CATAPULT_MARKER_AREA_YARDS if catapult else 110
        visible_limit = minimap.radius * .8 if catapult else 85
        now = time.monotonic()
        if catapult:
            self.object_sightings = {point: seen for point, seen in self.object_sightings.items() if now - seen <= 90}
        candidates = []
        for dx, dy in dots:
            east = (dx * cosine + dy * sine) * minimap.radius
            south = (-dx * sine + dy * cosine) * minimap.radius
            distance = math.hypot(east, south)
            point = (px + east / map_width, py + south / map_height)
            if (distance <= visible_limit and all(0 < v < 1 for v in point)
                    and (navigation.destination is None or router.distance(point, navigation.destination) <= marker_limit)
                    and all(router.distance(point, tried) > 6 for tried in self.object_targets_tried)):
                if catapult:
                    previous = next((seen for seen in self.object_sightings if router.distance(point, seen) <= 5), None)
                    if previous is not None:
                        self.object_sightings.pop(previous)
                    self.object_sightings[point] = now
                if near is None or router.distance(point, near) <= 8:
                    candidates.append((router.distance(point, near) if near is not None else distance, point, distance))
        remembered = False
        if not candidates and catapult and near is None:
            for point in self.object_sightings:
                distance = router.distance(navigation.position, point)
                if distance <= visible_limit and all(router.distance(point, old) > 6 for old in self.object_targets_tried):
                    candidates.append((distance, point, distance))
            remembered = bool(candidates)
        if candidates:
            _, point, distance = min(candidates)
            event = "quest_object_minimap_recheck" if near is not None else "quest_object_minimap_target"
            ctx.log_event(event, {"id": navigation.quest_id, "destination": point,
                                  "distance_yards": round(distance, 2), "remembered": remembered})
            return point

    def recheck_object_target(self, ctx, now, navigation, heading):
        if navigation.position is None or navigation.world_size is None:
            self.set_forward(ctx, False)
            return False
        router = GridRouter(*navigation.world_size)
        position = navigation.position
        remaining = router.distance(position, self.object_target)
        if self.object_check_origin is None:
            self.object_check_origin = position
        walked = router.distance(position, self.object_check_origin)
        if self.object_check_pending is None:
            if walked < 12 and (walked < 3 or remaining > 6):
                return True
            self.set_forward(ctx, False)
            self.object_check_pending = navigation.sequence, now
            return False
        sequence, started = self.object_check_pending
        if navigation.sequence == sequence and now - started < .75:
            return False
        updated = None
        if navigation.sequence != sequence:
            try:
                bounds = game_ui_bounds(frontmost_game_bounds(), navigation.minimap)
                updated = self.object_minimap_target(ctx, navigation, bounds, heading, near=self.object_target)
            except ValueError:
                pass
        if updated is None and remaining > 6 and now - started < .75:
            return False
        self.object_check_pending = None
        self.object_check_origin = position
        if updated is None:
            if remaining <= OBJECT_CLOSE_RECHECK_YARDS:
                # Terrain or the player arrow can obscure the dot. Finish only
                # this short approach and require a world tooltip before clicking.
                ctx.log_event("quest_object_minimap_nearby", {"id": navigation.quest_id,
                                                                "distance_yards": round(remaining, 2)})
                return True
            if navigation.quest_id == 90895:
                if self.object_lost_origin is None:
                    self.object_lost_origin = position, remaining
                origin, previous_remaining = self.object_lost_origin
                if router.distance(origin, position) <= 24 and remaining <= previous_remaining + 3:
                    return True
            ctx.log_event("quest_object_minimap_lost", {"id": navigation.quest_id,
                                                      "distance_yards": round(remaining, 2)})
            last_seen = self.object_target
            self.finish_object_approach()
            if navigation.apr_waypoint and not self.object_lost_revisited:
                self.object_lost_revisited = True
                self.search_waypoint, self.trip, self.arrived = last_seen, None, False
                ctx.log_event("quest_object_last_seen_search", {"id": navigation.quest_id,
                                                               "destination": last_seen})
                return False
            self.arrived = True
            return False
        self.object_lost_origin = None
        if router.distance(updated, self.object_target) > (4 if navigation.quest_id == 90895 else 1):
            self.object_target, self.trip = updated, None
        return True

    def refine_object_approach(self, ctx, navigation, bounds, heading):
        if self.object_approach_yards == OBJECT_REFINE_YARDS:
            return False
        if self.object_target is not None:
            updated = self.object_minimap_target(ctx, navigation, bounds, heading, near=self.object_target)
            self.object_target = updated or self.object_target
        target = self.object_target or (navigation.destination if navigation.objective_pin else None)
        if target is None or navigation.position is None or navigation.world_size is None:
            return False
        distance = GridRouter(*navigation.world_size).distance(navigation.position, target)
        if not OBJECT_REFINE_YARDS < distance <= OBJECT_CLOSE_RECHECK_YARDS:
            return False
        self.object_approach_yards = OBJECT_REFINE_YARDS
        self.search_waypoint, self.trip, self.arrived = None, None, False
        self.object_probe, self.object_points, self.object_probe_sequence = 0, None, None
        ctx.log_event("quest_object_approach_refine", {"id": navigation.quest_id,
                                                       "distance_yards": round(distance, 2)})
        return True

    def catapult_candidate(self, ctx, now, nav, heading, bounds):
        if self.object_scene is None:
            if ctx.input.name != "helper" or not local_vision_available() or nav.position is None or nav.world_size is None:
                return ()
            try:
                frame = capture_scene(ctx.capture, bounds)
                request = SceneRequest({"target": frame}, target="catapult", metadata={
                    "id": nav.quest_id, "map": nav.map_id, "position": nav.position,
                    "heading": heading, "sequence": nav.sequence,
                    "run_id": getattr(getattr(ctx, "evidence", None), "run_id", None)})
                self.object_scene = request, now, nav.position, heading, bounds
                ctx.log_event("quest_object_visual_request", {"id": nav.quest_id,
                    "evidence": str(getattr(request, "evidence_dir", ""))})
            except SCENE_ERRORS as exc:
                ctx.log_event("quest_object_visual_error", {"id": nav.quest_id, "error": str(exc)})
                return ()
            return None
        request, captured_at, position, facing, captured_bounds = self.object_scene
        if not request.future.done():
            return None
        self.object_scene = None
        if (now - captured_at > 22 or captured_bounds != bounds or nav.position is None or nav.world_size is None
                or GridRouter(*nav.world_size).distance(nav.position, position) > 1
                or abs(heading_error(facing, heading)) > math.radians(6)):
            return ()
        try:
            views, _ = request.future.result()
            evidence = views["target"]
        except SCENE_ERRORS as exc:
            ctx.log_event("quest_object_visual_error", {"id": nav.quest_id, "error": str(exc)})
            return ()
        point = evidence["point"]
        ctx.log_event("quest_object_visual_candidates", {"id": nav.quest_id, "observation": evidence})
        if point is None:
            return ()
        left, top, right, bottom = SCENE_BOUNDS
        return ((left + point[0] / 1000 * (right - left), top + point[1] / 1000 * (bottom - top)),)

    def search_object(self, ctx, now, navigation, heading):
        self.set_forward(ctx, False)
        busy = (TravelFlags.CASTING | TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING
                | TravelFlags.SWIMMING | TravelFlags.IN_COMBAT)
        if navigation.flags & busy or now - self.last_action < 2:
            return
        if self.object_click_at is not None:
            if now - self.object_click_at < 5:
                return
        pumpkin = navigation.quest_id == 90885
        hover_wait = .50
        if self.object_probe_sequence is not None and (
            navigation.sequence == self.object_probe_sequence or now - self.object_probe_at < hover_wait
        ):
            return
        try:
            bounds = game_ui_bounds(frontmost_game_bounds(), navigation.minimap)
        except ValueError:
            return
        x, y, width, height = bounds
        channel = ctx.cfg.channel
        if not (x <= channel.x < x + width and y <= channel.y < y + height):
            return
        precise = navigation.precise_object or navigation.objective_pin or pumpkin
        if self.object_click_at is not None:
            ctx.log_event("quest_object_progress_unconfirmed", {"id": navigation.quest_id})
            self.object_click_at = None
            if self.object_click_attempts >= 2:
                return self.stop(ctx, "quest_object_interaction_unconfirmed")
            if precise and self.refine_object_approach(ctx, navigation, bounds, heading):
                return
            if (self.object_probe_sequence is None or navigation.objective_cursor is None
                    or math.dist(navigation.objective_cursor, self.object_points[self.object_probe - 1]) > .012):
                return self.stop(ctx, "quest_object_interaction_unconfirmed")
        if precise and not navigation.objective_pin and not self.object_minimap_checked:
            self.object_minimap_checked = True
            point = self.object_minimap_target(ctx, navigation, bounds, heading)
            if point is not None:
                self.start_object_route(point)
                return
            ctx.log_event("quest_object_minimap_no_target", {"id": navigation.quest_id,
                                                            "calibrated": navigation.minimap is not None})
            if not navigation.apr_waypoint:
                if not self.search_quest_area(ctx, navigation):
                    return self.stop(ctx, "quest_object_not_found")
                self.finish_object_approach()
                return
        if self.object_points is None:
            hints = ()
            cursor = navigation.objective_cursor
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
                    log.warning("南瓜画面采样失败，取消鼠标探测。")
                ctx.log_event("quest_object_visual_candidates", {"id": navigation.quest_id, "points": hints})
            elif navigation.quest_id == 90895 and not cursor:
                hints = self.catapult_candidate(ctx, now, navigation, heading, bounds)
                if hints is None:
                    return
                if not hints and self.object_target is not None:
                    self.finish_object_approach()
                    return
            # Only observed candidates may be hovered. A map point alone does
            # not identify a clickable screen location; never raster-scan it.
            self.object_points = tuple(dict.fromkeys(((cursor,) if cursor else ()) + tuple(hints)))[:6]
            if not self.object_points:
                ctx.log_event("quest_object_detection_missing", {"id": navigation.quest_id})
                return self.stop(ctx, "quest_object_visual_target_required")
        cursor = navigation.objective_cursor
        if self.object_probe_sequence is not None and cursor:
            expected = self.object_points[self.object_probe - 1]
            # Require the game to report the object at the probe we just sent.
            target = self.object_target or (navigation.destination if navigation.objective_pin else None)
            reach = self.object_approach_yards + .5
            if target is None and navigation.apr_waypoint:
                target = self.search_waypoint or navigation.destination
                reach = 5  # APR waypoints mark a search area, not an exact object location.
            nearby = target and navigation.position and navigation.world_size and GridRouter(
                *navigation.world_size).distance(navigation.position, target) <= reach
            if math.dist(cursor, expected) <= .012 and (not precise or nearby):
                ctx.input.click_world_object()
                self.object_click_at = self.last_action = now
                self.object_click_target = target
                self.object_click_attempts += 1
                self.object_probe_sequence = navigation.sequence
                ctx.log_event("quest_object_interact", {"id": navigation.quest_id, "cursor": cursor,
                                                        "attempt": self.object_click_attempts,
                                                        "input": ctx.input.name})
                return
            ctx.log_event("quest_object_hover_rejected", {"id": navigation.quest_id, "cursor": cursor,
                                                          "probe": expected, "nearby": bool(nearby)})
        if self.object_probe >= len(self.object_points):
            ctx.log_event("quest_object_hover_exhausted", {"id": navigation.quest_id,
                                                           "probes": self.object_probe})
            if precise and self.refine_object_approach(ctx, navigation, bounds, heading):
                return
            if self.object_target is not None:
                self.finish_object_approach()
                return
            if not self.search_quest_area(ctx, navigation):
                return self.stop(ctx, "quest_object_not_found")
            self.object_probe, self.object_probe_sequence = 0, None
            self.object_points = None
            self.object_minimap_checked = False
            return
        u, v = self.object_points[self.object_probe]
        ctx.input.move_to(round(x + u * width), round(y + v * height), jitter_px=0)
        self.object_probe += 1
        self.object_probe_at, self.object_probe_sequence = now, navigation.sequence

    def search_quest_area(self, ctx, navigation):
        if (navigation and navigation.quest_id == 90895 and navigation.precise_object
                and navigation.position and navigation.world_size):
            prior = self.area_search
            if prior is None or prior.router.distance(prior.center, navigation.position) > 60:
                self.area_search = QuestAreaSearch(navigation, center=navigation.position)
                if not self.area_search.native and navigation.destination:
                    self.area_search.points = tuple(point for point in self.area_search.points
                        if self.area_search.router.distance(point, navigation.destination) <= CATAPULT_MARKER_AREA_YARDS)
                if prior is not None:
                    self.area_search.visited, self.area_search.steps = prior.visited, prior.steps
            if self.area_search.steps >= 16:
                return False
            point = self.area_search.next(navigation.position, ctx.heading(), ctx.cfg.navigation.facing_convention)
            if point is None:
                return False
            self.search_waypoint, self.trip, self.arrived = point, None, False
            ctx.log_event("quest_object_local_search", {"waypoint": point, "origin": navigation.position,
                                                        "attempt": self.area_search.steps})
            return True
        if self.hunt is not None and navigation and navigation.position and navigation.destination and navigation.world_size:
            if (self.area_search is None or self.area_search.quest_id != navigation.quest_id
                    or self.area_search.map_id != navigation.map_id
                    or self.area_search.router.distance(self.area_search.center, navigation.destination) > 30):
                self.area_search = QuestAreaSearch(navigation)
            elif navigation.search_area is not None:
                self.area_search.native = True
                self.area_search.points = navigation.search_area
            waypoint = self.area_search.next(navigation.position, ctx.heading(), ctx.cfg.navigation.facing_convention,
                                              avoid=tuple(point for point, _ in self.danger_zones))
            if waypoint is None:
                return False
            self.searches += 1
            self.search_waypoint, self.trip, self.arrived = waypoint, None, False
            ctx.log_event("quest_search_area", {"waypoint": waypoint, "attempt": self.area_search.steps,
                "source": "native_quest_blob" if self.area_search.native else "bounded_90_yard_fallback",
                "points": len(self.area_search.points)})
            return True
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
        restored = self.corpse_restore is not None and self.corpse_restore[0] is fight
        same_corpse = (sample and not sample.unsafe and sample.target_dead and sample.target_id == fight.target_id
                       and (sample.state_epoch == fight.state_epoch or restored
                            and sample.previous_target_epoch == fight.state_epoch))
        if not same_corpse or navigation.flags & TravelFlags.IN_COMBAT:
            self.corpse_approach = None
        if self.fetch_for_fight is fight and self.fetch_attempt is not None:
            started, baseline, observed_lootable = self.fetch_attempt
            # A pre-existing false flag also occurs on corpses Fetch can loot.
            # Only a positive-to-negative observation on this corpse is evidence
            # of loot removal; an unchanged false must not confirm our keypress.
            if progress > baseline or (same_corpse and observed_lootable
                                        and navigation.corpse_lootable is False):
                self.fetch_attempt, self.loot_for_fight = None, fight
                ctx.log_event("quest_fetch_confirmed", {"target": fight.target_id, "progress": progress})
                if getattr(navigation, "sweep_ready", False) and navigation.loot_epoch is not None:
                    self.loot_sweep = LootSweep(now)
                    self.batch_for_fight = fight
                    return True
                return False
            if now - started < 8:
                if same_corpse and navigation.corpse_lootable is True:
                    self.fetch_attempt = started, baseline, True
                return True
            self.fetch_attempt = None
            ctx.log_event("quest_fetch_unconfirmed", {"target": fight.target_id, "progress": progress,
                                                      "lootable": navigation.corpse_lootable})
        blocked = (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
                   | TravelFlags.CASTING | TravelFlags.IN_COMBAT)
        if (getattr(navigation, "sweep_ready", False) and navigation.fetch_ready
                and navigation.loot_epoch is not None and self.batch_for_fight is not fight
                and not navigation.flags & blocked and progress > self.hunt.baseline
                and (same_corpse or sample and sample.target_id == 0)):
            self.batch_for_fight = fight
            self.loot_sweep = LootSweep(now)
            self.set_forward(ctx, False)
            return True
        if not same_corpse and progress <= self.hunt.baseline:
            if restored and now - self.corpse_restore[1] < 1.5:
                return True
            if (not restored and navigation.last_target_verified and not navigation.flags & blocked
                    and sample and not sample.unsafe and sample.key_verified and sample.target_id == 0):
                self.set_forward(ctx, False)
                self.corpse_restore = fight, now
                self.hunt.credit_deadline = now + 7
                ctx.log_event("quest_corpse_restore_target", {"target": fight.target_id})
                ctx.input.tap("p")
                return True
        nearby_fetch = (restored and not same_corpse and sample and not sample.unsafe
                        and not navigation.flags & blocked
                        and (sample.target_id == 0 or sample.target_dead and sample.target_id == fight.target_id))
        if (navigation.fetch_ready and not navigation.flags & blocked
                and self.fetch_for_fight is not fight and progress <= self.hunt.baseline
                and (same_corpse or nearby_fetch)):
            self.set_forward(ctx, False)
            self.fetch_for_fight = fight
            self.fetch_attempt = now, progress, bool(same_corpse and navigation.corpse_lootable is True)
            self.hunt.credit_deadline = now + 11
            ctx.log_event("quest_fetch", {"target": fight.target_id, "corpse_verified": bool(same_corpse),
                                         "lootable": navigation.corpse_lootable})
            ctx.input.tap("l")
            return True
        attempts = 0
        current_loot_epoch = getattr(navigation, "loot_epoch", None)
        if self.loot_attempt is not None and self.loot_attempt[0] is fight:
            _, started, loot_epoch, observed_lootable, attempts = self.loot_attempt
            receipt = same_corpse and (
                loot_epoch is not None and current_loot_epoch is not None and loot_epoch != current_loot_epoch
                or observed_lootable and navigation.corpse_lootable is False
            )
            if receipt:
                self.loot_attempt, self.loot_for_fight = None, fight
                ctx.log_event("quest_loot_confirmed", {"target": fight.target_id, "progress": progress})
                return False
            if now - started < 2.5:
                return True
            if attempts >= 2 or not same_corpse or navigation.corpse_lootable is not True:
                self.loot_attempt, self.loot_for_fight = None, fight
                ctx.log_event("quest_loot_unconfirmed", {"target": fight.target_id, "attempts": attempts})
                return False
        if not same_corpse or self.loot_for_fight is fight or navigation.corpse_lootable is False:
            return False
        if navigation.task & TaskFlags.LOOT_IN_RANGE and not navigation.flags & blocked:
            if current_loot_epoch is None:
                # Older addons cannot report cleared slots.
                self.loot_for_fight = fight
            else:
                self.loot_attempt = (fight, now, current_loot_epoch,
                                     navigation.corpse_lootable is True, attempts + 1)
            if self.hunt.resource:
                self.hunt.credit_deadline = max(self.hunt.credit_deadline or 0, now + 4)
            ctx.log_event("quest_loot", {"target": fight.target_id})
            ctx.input.tap("f")
            return True
        if (navigation.corpse_lootable is True and navigation.flags & TravelFlags.KEYS_VERIFIED
                and not navigation.flags & blocked):
            if self.corpse_approach is None or self.corpse_approach[0] is not fight:
                self.corpse_approach = fight, now, None
                self.hunt.credit_deadline = now + 8
                ctx.log_event("quest_corpse_approach", {"target": fight.target_id})
            _, started, attempt = self.corpse_approach
            if now - started < 4:
                identity = (navigation.map_id, fight.target_id, sample.state_epoch)
                observation = ApproachObservation(identity, navigation.sequence, False, True,
                                                  navigation.position, navigation.world_size)
                if attempt is not None:
                    result = attempt.poll(now, observation)
                    if result.state == ActionState.WAITING:
                        return True
                    ctx.log_event("quest_approach_result", {"kind": "corpse", "target": fight.target_id,
                        "reason": result.reason, "moved_yards": result.moved_yards})
                    if result.state != ActionState.CONFIRMED:
                        # Exhaust this corpse approach; never turn no displacement into loot success.
                        self.corpse_approach = fight, now - 4, attempt
                        return False
                if navigation.position is None or navigation.world_size is None:
                    return False
                attempt = ApproachAttempt(ApproachGoal("corpse", identity, pulse_s=.18), now, observation)
                self.set_forward(ctx, False)
                ctx.log_event("quest_approach_issued", {"kind": "corpse", "target": fight.target_id,
                    "sequence": navigation.sequence, "position": navigation.position})
                ctx.input.tap("w", hold_s=.18)
                self.corpse_approach = fight, started, attempt
                return True
        return False

    def hunt_tick(self, ctx, now, progress, in_combat, navigation=None):
        if navigation and navigation.flags & TravelFlags.CASTING:
            self.set_forward(ctx, False)
            return
        sample = self.read_combat(ctx)
        if navigation and not in_combat and self.loot_corpse(ctx, now, progress, sample, navigation):
            return
        if not in_combat and navigation and self.hunt.fight is None and self.check_patrol(ctx, now, navigation):
            return
        if (not in_combat and navigation and navigation.position and navigation.world_size
                and self.hunt.fight is None and sample and sample.target_danger and sample.in_range is True
                and self.start_patrol_avoidance(ctx, now, navigation, sample.target_id, sample.target_danger)):
            return
        if sample and sample.key_verified and str(sample.key) in self.quest_keys:
            return self.stop(ctx, "quest_combat_action_key_conflicts")
        verified_target = navigation.objective_target if navigation else 0
        status, key = self.hunt.step(now, sample, progress, in_combat, verified_target, navigation)
        if key:
            if status == "approach" and (not navigation or not navigation.flags & TravelFlags.KEYS_VERIFIED):
                return self.stop(ctx, "quest_navigation_bindings_unverified")
            self.set_forward(ctx, False)
            event = "quest_combat_" + (status if status in {"turn", "search_turn", "approach"} else "action")
            ctx.log_event(event, {"id": self.steps[self.index].quest_id, "target": sample.target_id, "key": key})
            tap_combat_key(ctx.input, status, key)
        elif status == "credited":
            ctx.log_event("quest_combat_credit", {"id": self.steps[self.index].quest_id, "progress": progress})
        elif status == "resource_try_next":
            ctx.log_event("quest_resource_try_next", {"id": self.steps[self.index].quest_id,
                "percent": progress / 10, "uncredited_kills": self.hunt.uncredited_kills})
        elif status == "target_not_found":
            if self.search_quest_area(ctx, navigation):
                self.hunt.reset_search()
            else:
                self.stop(ctx, "quest_combat_target_not_found")
        elif status != "waiting":
            if status in {"line_of_sight_blocked", "approach_blocked"} and self.combat_adjusted is self.hunt.fight:
                if now - self.hunt.fight.last_action < .75:
                    return
            elif status in {"line_of_sight_blocked", "approach_blocked"} and navigation and self.hunt.fight:
                side = self.combat_side_step(ctx, navigation, sample)
                if side is not None:
                    self.combat_adjusted = self.hunt.fight
                    self.hunt.fight.last_action = now + .65
                    self.hunt.fight.approach_attempt = None
                    self.set_forward(ctx, False)
                    ctx.log_event("quest_combat_reposition", {"target": sample.target_id, "key": side})
                    ctx.input.tap(side, hold_s=.18)
                    return
            ctx.log_event("quest_combat_failed", {"id": self.steps[self.index].quest_id, "reason": status,
                          "target": sample.target_id if sample else None,
                          "target_epoch": sample.state_epoch if sample else None,
                          "range": sample.in_range if sample else None, "facing": sample.facing if sample else None,
                          "facing_error": sample.facing_error if sample else None,
                          "line_of_sight_blocked": sample.line_of_sight_blocked if sample else None,
                          "cast_epoch": sample.cast_epoch if sample else None,
                          "health": sample.target_health if sample else None, "progress": progress})
            if status in {"no_quest_credit", "resource_no_progress"} and navigation and self.hunt.fight:
                ctx.log_event("quest_corpse_unconfirmed", {"expected": self.hunt.fight.target_id,
                              "target": sample.target_id if sample else None,
                              "dead": sample.target_dead if sample else None,
                              "lootable": navigation.corpse_lootable, "fetch_ready": navigation.fetch_ready})
            self.stop(ctx, f"quest_combat_{status}")

    def wait_for_data(self, ctx, now, reason):
        self.camera.cancel(ctx, now, reason)
        self.set_forward(ctx, False)
        if self.unusable_since is None or self.unusable_reason != reason:
            self.unusable_since, self.unusable_reason = now, reason
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


def foreground_app_details() -> dict:
    """Diagnostic only; failures must never interfere with input release."""
    if sys.platform != "darwin":
        return {"application": None, "bundle_id": None}
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return {"application": str(app.localizedName()) if app else None,
                "bundle_id": str(app.bundleIdentifier()) if app else None}
    except Exception as exc:
        return {"application": None, "bundle_id": None, "error": type(exc).__name__}


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
            from ..perception.vision_worker import close_worker
            close_worker()
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
            try:
                controller_version = version("wowagnet")
            except PackageNotFoundError:
                controller_version = "source"
            ctx.log_event("quest_navigation_start", {"id": navigation.quest_id, "map": navigation.map_id,
                          "controller": controller_version, "protocol": navigation.protocol_version,
                          "phase": navigation.phase.name, "mode": "prepared" if self.require_prepared else "travel"})
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

    def _finish(self, status, t0):
        state = self.machine.current
        if isinstance(state, NavigateCurrentQuest) and state.runner is not None:
            state.runner.cancel_scene_requests()
        return super()._finish(status, t0)
