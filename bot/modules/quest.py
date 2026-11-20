"""Execute a validated same-map quest plan with live dialogue confirmation."""
from __future__ import annotations

import logging
import math
import re
import sys
import time
from dataclasses import asdict, dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml

from ..control.navigation import Route, RouteFollower, heading_error
from ..control.camera import CameraController
from ..control.pathfinding import GridRouter
from ..control.ground_trace import GroundTrace
from ..control.quest_travel import QuestTrip
from ..control.road_route import projection
from ..control.road_scan import RoadScanSession
from ..control.building_access import BuildingAccess, building_target_receipt
from ..control.quest_search import QuestAreaSearch, inside_quest_search_area
from ..control.quest_terrain import (SCENE_BOUNDS, SCENE_ERRORS, SceneRequest, TerrainDetour, capture_scene,
                                     local_vision_available)
from ..core.statemachine import State
from ..core.paths import SCREENSHOT_DIR, DATA_DIR
from ..data.combat import client_edition
from ..data.grail import cached_npc_location
from ..decision.quest_modes import QUEST_MODE_INSTRUCTIONS, QUEST_MODES, executable_objective_mode
from ..decision.startlux import DECISION_WAIT_S
from ..control.input_helper import HelperActionRejected, HelperForegroundPaused
from ..decision.route_planner import available as cloud_route_available
from ..perception.capture import CaptureError, ScreenCapture, frontmost_game_bounds, game_ui_bounds
from ..perception.channel_spec import REGION_H, REGION_W, StatusFlags
from ..perception.combat_channel import combat_region, decode_combat
from ..perception.cv import hostile_nameplate_points, minimap_quest_dots, pumpkin_probe_points, save_snapshot
from ..perception.observation import ObservationTracker
from ..perception.player_identity import profile_from_sample
from ..control.approach import ApproachAttempt, ApproachGoal, ApproachObservation, ActionState
from ..control.input_mock import MockInput
from ..perception.quest_channel import (
    RESOURCE_PROGRESS_MAX, Dialogue, Objective, QuestKind, QuestState, decode_quest, quest_region,
)
from ..perception.quest_text import QuestTextStream, decode_quest_text, quest_text_region
from ..perception.road_map import decode_road_status, road_region
from ..perception.travel_channel import (CAMERA_BINDING_BUILD, CameraResult, ChoiceStep, CorpseSource, ForeverStep, LifeState,
                                         ObjectiveAction, PetState, RoundStage,
                                         TaskFlags, TravelFlags, camera_binding_key, decode_travel, travel_region)
from .base import Module
from .combat import Fight, QuestDefense, QuestHunt, TURN_INTERVAL, TURN_SETTLE, tap_combat_key
from .recovery import ClassPreparation, ConsumableRecovery, QuestRecovery
from .loot import CORPSE_LOOT_BUILD, LOOT_BLOCKED, LootSweep, NearbyCorpseScan, RequiredCorpseLoot, loot_prepared

STORY_KINDS = frozenset({QuestKind.CAMPAIGN, QuestKind.MAINLINE, QuestKind.SIDE})
UNSAFE_FLAGS = StatusFlags.DEAD | StatusFlags.GHOST | StatusFlags.FLYING
PICKUP_DIALOGUE = {Dialogue.OFFER, Dialogue.DETAIL}
TURN_IN_DIALOGUE = {Dialogue.TURN_IN, Dialogue.PROGRESS, Dialogue.REWARD}
QUEST_TARGETS = {90882: frozenset({244669, 244670, 244671, 244672})}
ARRIVAL_QUESTS = frozenset({90883})
CHOICE_HANDOFFS = {(2451, 90911): (85, 91281), (85, 88719): (2432, 88719)}
CHOICE_HANDOFF_WAIT_S = 600
CONFIGURED_CHOICE_WAIT_S = 45
PREPARED_FOCUS_WAIT_S = 45
TIMEOUT_DECISION_WAIT_S = 36
QUEST_CONNECTION_LOSSES = frozenset({
    "quest_channel_lost", "quest_channel_stale", "quest_navigation_channel_stale",
    "quest_navigation_channel_unavailable", "quest_player_telemetry_unavailable",
    "quest_automatic_preparation_lost", "quest_round_unexpected_switch", "quest_wrong_map",
})
FOREVER_TARGET_LEVEL = 15
FOREVER_SERVICE_TRAVEL = {ForeverStep.TRAVEL, ForeverStep.REPAIR_TRAVEL}
# Exterior mesh support for the Northshire Abbey approach, in Elwynn map units.
NORTHSHIRE_ABBEY_ENTRY = (0.485587, 0.422709)
NORTHSHIRE_ABBEY_GIVER = (0.489, 0.416)
# Outdoor mesh support on the approach from the kobold camps.
NORTHSHIRE_NORTH_REJOIN = (0.4887, 0.403)
# Clear the building perimeter before continuing to the outdoor goal.
NORTHSHIRE_EXIT_NORTH = (158.0, -8872.0)
NORTHSHIRE_EXIT_NORTH_EAST = (174.0, -8851.0)
NORTHSHIRE_EXIT_TRAINER = (239.5, -8888.5)
NORTHSHIRE_EXIT_SOUTH = (145.0, -8927.0)
# Dry road through the Northshire fence. Each leg was checked against the
# matching Forever client collision mesh; the runtime follows map coordinates.
NORTHSHIRE_SOUTH_ROAD = ((.475, .466), (.469741, .474602), (.466745, .478058), (.462538, .479268),
                         (.456833, .480909), (.456027, .486785), (.4562, .497845))
# Connected lower ground by the door, west stair, southwest ledge, east alcove and stable fence.
NORTHSHIRE_REJOINS = ((159.0, -8912.5), (133.4, -8907.6), (143.0, -8919.1),
                     (186.7, -8918.4), (211.7, -8930.0), (205.6, -8925.7))
NORTHSHIRE_ABBEY_TURN_INS = frozenset({783, 7, 15, 21})
NORTHSHIRE_ABBEY_PICKUPS = frozenset({7, 15, 21, 54, 92479})
GOLDSHIRE_INN = (0.433, 0.658)
GOLDSHIRE_INN_EXTERIOR = (0.422, 0.659)
GOLDSHIRE_HANDOFF_XY = (-70.7, -9464.9)
GOLDSHIRE_DUGHAN_APPROACH_XY = (-72.22, -9465.52)
GOLDSHIRE_INN_DOOR_XY = (-40.5, -9461.0)
GOLDSHIRE_INN_DOOR_REJOIN = (-40.8, -9460.45)
GOLDSHIRE_SMITHY_APPROACH = (.4176, .66553)
GOLDSHIRE_SMITHY_APPROACH_XY = (-86.0, -9480.0)
GOLDSHIRE_REPAIR_NPCS = {54, 2046}
FARGODEEP_SOUTH_ENTRY = (.39011, .82202)
FARGODEEP_SOUTH_ENTRY_XY = (-181.44, -9842.17)
FARGODEEP_SCOUT_XY = (-149.86, -9793.80)
ECHO_RIDGE_EXTERIOR = (0.478, 0.322)
ECHO_RIDGE_HANDOFF_XY = (123.64, -8684.88)
ECHO_RIDGE_HUNT = (0.484, 0.29)
ECHO_RIDGE_HUNT_XY = (144.47, -8610.81)
ECHO_RIDGE_WALL_REJOIN_XY = (140.15, -8623.45)
NPC_SCAN_ANGLES = tuple(math.radians(angle) for angle in range(0, 360, 30))
NPC_MARKER_APPROACH_YARDS = 8
NPC_MARKER_APPROACH_RADIUS = 1.25
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
    "quest_camp_progress_unconfirmed",
    "quest_mode_unconfirmed",
    "quest_offer_not_open", "quest_phase_timeout",
    "quest_pickup_dialogue_not_open", "quest_pickup_npc_not_found", "quest_pickup_npc_selection_unconfirmed",
    "quest_ready_state_unconfirmed", "quest_resource_search_area_exhausted", "quest_reward_choice_required",
    "quest_route_blocked", "quest_turn_in_approach_blocked", "quest_turn_in_approach_exhausted",
    "quest_turn_in_npc_selection_unconfirmed", "quest_turn_in_npc_not_in_interact_range",
    "quest_turn_in_npc_lost", "quest_flight_route_blocked",
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


@dataclass(frozen=True)
class TimeoutReview:
    quest_id: int
    phase: QuestState | None
    reason: str
    attempt: int
    started_at: float
    poll_after: float


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
        self.goldshire_npc_trip = None
        self.goldshire_npc_reposition_tried = False
        self.road_scan = RoadScanSession()
        self.road_status = None
        self.road_trip_identity = None
        self.quest_text = QuestTextStream()
        self.quest_mode_identity = None
        self.quest_mode_started = 0.0
        self.quest_mode_result = None
        self.building_access = None
        self.ground_router = None
        self.terrain = None
        self.terrain_attempts = []
        self.object_scene = None
        self.travel_keys: frozenset[str] = frozenset()
        self.travel_pulse_pending = False
        self.nav_sequence, self.nav_at = None, 0.0
        self.search_waypoint, self.searches = None, 0
        self.area_search = None
        self.loot_sweep = None
        self.batch_for_fight = None
        self.defense_had_output = False
        self.defense_loot = []
        self.required_loot = []
        self.nearby_loot_select_until = None
        self.nearby_loot_scan = None
        self.status_wire_at = None
        self.status_wire_failed = False
        self.status_wire_code = None
        self.continuous = False
        self.timeout_review = None
        self.timeout_attempts = {}
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
        self.nav3d_released = False
        self.nav3d_exit_started = False
        self.choice_wait = None
        self.choice_action_sent = ChoiceStep.WAIT
        self.accepted_turn_in_pin = None
        self.npc_press_at: float | None = None
        self.npc_marker_approach = None
        self.npc_marker_anchor = None
        self.npc_grail_hint = None
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
        self.camp_attempt_at = None
        self.camp_attempts = 0
        self.camera_restore_reason = "start"
        self.camera = CameraController()
        self.camera_bindings = None
        self.expect_camera_bindings = False
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
        self.forever_level_receipt = None
        self.forever_target_level_verified = False
        self.forever_progression_receipt = None
        self.forever_entry = None
        self.forever_entry_trip = None
        self.forever_scene_wait_at = None
        self.forever_exit_rejoins = set()
        self.forever_building_segment = None
        self.forever_building_complete = set()
        self.forever_corpse_goal = None
        self.cave_retreat_failed = False
        self.consumables = ConsumableRecovery()
        self.forever_progress = None
        self.forever_progress_trip = None
        self.forever_progress_since = None
        self.forever_progress_at = float("-inf")
        self.forever_npc_scan = 0
        self.forever_npc_selects = 0
        self.forever_repair_interacts = 0
        self.forever_smithy_return = False
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
        self.corpse_loot_wait: tuple[Fight, float] | None = None
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
        self.travel_pulse_pending = False
        self.suspend_camera = None
        if reason == "game_not_frontmost":
            ctx.log_event("quest_focus_lost", foreground_app_details())
        ctx.log_event("quest_stop_requested", {"reason": reason, "phase": self.phase.name if self.phase else None,
                                               "id": self.steps[self.index].quest_id})
        nav = self.round_navigation
        if self.continuous and self.planner is not None and (
                reason in TASK_FAILURES or reason == 'quest_loot_unconfirmed'
                or reason in {'quest_defense_no_cast_or_damage_receipt', 'quest_defense_no_progress',
                              'quest_defense_attacker_unconfirmed', 'quest_defense_landing_unconfirmed',
                              'quest_defense_dismount_unconfirmed'}
                or reason.endswith(('_timeout', '_time_budget', '_exhausted'))):
            self.travel_keys, self.forward = frozenset(), False
            if self.timeout_review is None:
                task = nav.quest_id if nav else self.steps[self.index].quest_id
                phase = nav.phase if nav else self.phase
                key = (task, phase, reason)
                count = self.timeout_attempts.get(key, 0) + 1
                self.timeout_attempts[key] = count
                now = time.monotonic()
                self.timeout_review = TimeoutReview(task, phase, reason, count, now, now)
                ctx.log_event('quest_timeout_review_started', {'id': task, 'reason': reason, 'attempt': count})
            return
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

    def review_timeout(self, ctx, now, frame, nav, combat, status):
        review = self.timeout_review
        if review is None:
            return False
        task, phase, reason = review.quest_id, review.phase, review.reason
        self.set_travel_keys(ctx, frozenset())
        if not frame or not nav or not nav.task & TaskFlags.PREPARED:
            return True
        if (frame.quest_id, frame.map_id, frame.state) != (nav.quest_id, nav.map_id, nav.phase):
            return True
        if nav.quest_id != task or phase is not None and nav.phase != phase:
            self.timeout_review = None
            return False
        if status and status[0] & (StatusFlags.DEAD | StatusFlags.GHOST):
            self.timeout_review = None
            return False  # The normal life-state recovery has priority.
        if now < review.poll_after:
            return True
        in_combat = bool(nav.flags & TravelFlags.IN_COMBAT or status and status[0] & StatusFlags.IN_COMBAT)
        if reason.startswith("quest_defense_") and not in_combat:
            self.timeout_review = None
            ctx.log_event("quest_timeout_review_recovered", {"id": task, "reason": reason})
            return False
        safe_interact = not in_combat and not nav.flags & LOOT_BLOCKED and frame.dialogue == Dialogue.CLOSED
        verified_defense = bool(in_combat and combat and combat.target_id and combat.target_engaged
            and not combat.target_dead and not combat.unsafe and combat.key_verified and combat.in_range is True
            and not combat.target_danger and not combat.line_of_sight_blocked
            and not nav.flags & (LOOT_BLOCKED & ~TravelFlags.IN_COMBAT) and frame.dialogue == Dialogue.CLOSED)
        criteria = {
            'retry': ('当前目标仍在攻击，键位和射程有效，使用新帧复核后恢复自卫' if verified_defense
                      else '重新核对当前目标和路径，重试仍有效的操作'),
            'wait': '当前缺少合适的安全动作，短暂等待新的游戏回执再判断',
        }
        if (safe_interact and not self.required_loot and self.suspend_request is None and nav.suspend_ready
                and nav.suspend_epoch is not None and nav.round_advance):
            criteria['next_task'] = '保留当前任务并转向已核对的下一项任务'
        retreat = None
        heading = ctx.heading()
        if (in_combat and self.defense and self.defense.retreat is None and nav.position and nav.world_size
                and heading is not None and ctx.heading_age() <= .75 and nav.flags & TravelFlags.KEYS_VERIFIED
                and not nav.flags & (LOOT_BLOCKED & ~TravelFlags.IN_COMBAT) and frame.dialogue == Dialogue.CLOSED):
            router = GridRouter(*nav.world_size)
            retreat = next((point for point in reversed(self.safe_ground)
                            if 35 <= router.distance(point, nav.position) <= 62), None)
            if retreat is not None:
                criteria['retreat'] = '沿已走过的安全地面撤出战斗，脱战后继续原任务和待拾取事项'
        signature = ('timeout_recovery', task, nav.map_id, int(nav.phase), reason, review.attempt, in_combat,
                     bool(self.required_loot), verified_defense, tuple(criteria),
                     combat and (combat.target_id, combat.state_epoch))
        state = {'quest_id': task, 'map_id': nav.map_id, 'quest_phase': nav.phase.name, 'timeout': reason,
                 'attempt': review.attempt, 'in_combat': in_combat, 'target_id': combat.target_id if combat else 0,
                 'target_engaged': bool(combat and combat.target_engaged), 'required_loot': len(self.required_loot),
                 'verified_defense_available': verified_defense, 'attack_in_range': combat.in_range if combat else None,
                 'attack_binding_verified': bool(combat and combat.key_verified),
                 'quest_progress': nav.objective_progress, 'position': nav.position, 'health_bucket': status[1] if status else None}
        result = None
        if now - review.started_at <= TIMEOUT_DECISION_WAIT_S:
            try:
                result = self.planner.poll(signature, state, criteria, max_age_s=TIMEOUT_DECISION_WAIT_S,
                    instructions=('任务步骤超时。选择当前证据支持的下一动作。保留待拾取事项。'
                                  '战斗中已核对自卫条件时优先恢复自卫，有验证过的撤退路线也可撤离；'
                                  '证据不足且缺少可执行动作时等待。'))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                result = {'reason': str(exc)}
            if result is None:
                return True
        choice = (result or {}).get('recommendation')
        fallback = choice is None and verified_defense
        if fallback:
            choice = 'retry'
        ctx.log_event('quest_timeout_review_decision', {'id': task, 'reason': reason,
                      'choice': choice, 'fallback_verified_defense': fallback,
                      'result': result or {'reason': 'decision_budget'}})
        if choice not in criteria or choice == 'wait':
            self.timeout_review = replace(review, started_at=now + 3, poll_after=now + 3)
            return True
        self.timeout_review = None
        if choice == 'next_task':
            self.suspend_request = (task, nav.phase, nav.suspend_epoch, reason, now, 'pending')
            return True
        elif choice == 'retreat':
            self.start_defense_retreat(ctx, now, nav, retreat, combat, reason)
            return True
        for corpse in self.required_loot:
            corpse.started = now
        if self.suspend_request is not None:
            task, phase, epoch, old_reason, _, old_state = self.suspend_request
            self.suspend_request = task, phase, epoch, old_reason, now, old_state
        if in_combat:
            if self.defense is not None:
                self.defense.grounded_at = now
                self.defense.last_progress_at = now
                self.defense.retreat_at = now if self.defense.retreat else None
                self.defense.select_started_at, self.defense.selects = None, 0
            if self.hunt is not None and self.hunt.fight is not None:
                self.hunt.started_at = now
            for fight in (self.defense and self.defense.fight, self.hunt and self.hunt.fight):
                if fight and combat and (fight.target_id, fight.state_epoch) == (combat.target_id, combat.state_epoch):
                    fight.pending_action_at = fight.first_output_at = None
                    fight.no_receipts = fight.facing_probes = 0
            self.phase_started = now
        elif not self.required_loot and self.suspend_request is None:
            self.phase, self.trip, self.defense = None, None, None
            self.unusable_since = None
        return True

    def wait_for_choice_handoff(self, ctx, now, frame, navigation):
        if self.travel_only or self.accept_only or ctx.cfg.nav3d.mode == "nav3d-shadow":
            return False
        coherent = (frame is not None and navigation is not None and frame.tracking
                    and (frame.quest_id, frame.map_id, frame.state) ==
                    (navigation.quest_id, navigation.map_id, navigation.phase))
        if self.choice_wait is None:
            if not (coherent and frame.map_id == self.map_id and frame.state == QuestState.ACTIVE
                    and navigation.objective_action == ObjectiveAction.CHOICE and not navigation.current_suspended
                    and navigation.task & TaskFlags.PREPARED):
                return False
            source = (frame.map_id, frame.quest_id)
            configured = source in CHOICE_HANDOFFS and (navigation.addon_build or 0) >= 283
            if configured and navigation.choice_step == ChoiceStep.APPROACH:
                return False
            budget = CONFIGURED_CHOICE_WAIT_S if configured else CHOICE_HANDOFF_WAIT_S
            self.choice_wait = (*source, now + budget)
            self.choice_action_sent = ChoiceStep.WAIT
            self.cancel_scene_requests()
            self.camera.cancel(ctx, now, "choice_handoff")
            self.set_forward(ctx, False)
            ctx.log_event("quest_choice_waiting", {"id": frame.quest_id, "map": frame.map_id,
                                                   "step": navigation.choice_step.name})
            log.info("任务 %s 正在等待游戏内的冒险目的地和后续任务回执。", frame.quest_id)
        source_map, source_quest, deadline = self.choice_wait
        expected_handoff = CHOICE_HANDOFFS.get((source_map, source_quest))
        if coherent and navigation.task & TaskFlags.PREPARED:
            destination = (frame.map_id, frame.quest_id)
            ready = (navigation.round_stage == RoundStage.TURN_IN and frame.state == QuestState.READY
                     if frame.quest_id == source_quest else navigation.round_stage == RoundStage.PICKUP)
            if ready and destination == expected_handoff:
                self.choice_wait = None
                self.map_id, self.nav3d_released = frame.map_id, True
                self.round_advance_at = now
                self.round_stage = RoundStage.NONE
                self.phase = self.sample = self.last_sequence = self.nav_sequence = None
                self.arrived, self.trip, self.hunt = False, None, None
                self.last_frame = self.last_target = now
                self.ground_trace.clear()
                ctx.log_event("quest_choice_handoff", {"id": source_quest, "map": frame.map_id,
                                                      "next_id": frame.quest_id})
                return False
            if destination == (source_map, source_quest):
                sent = self.choice_action_sent
                if (frame.state in {QuestState.READY, QuestState.TURNED_IN}
                        and not (expected_handoff and sent in {ChoiceStep.SELECT, ChoiceStep.STAND})):
                    self.choice_wait = None
                    self.last_frame = self.last_target = now
                    return False
                choice_step = navigation.choice_step
                if now < deadline and sent == ChoiceStep.STAND and choice_step == ChoiceStep.APPROACH:
                    self.choice_wait = None
                    self.npc_press_at = None
                    self.last_frame = self.last_target = now
                    return False
                stand = (sent == ChoiceStep.SELECT and choice_step == ChoiceStep.STAND
                         and navigation.flags & TravelFlags.KEYS_VERIFIED and navigation.position is not None
                         and (navigation.recovery is None or navigation.recovery.life == LifeState.ALIVE))
                next_action = (sent == ChoiceStep.WAIT and choice_step == ChoiceStep.OPEN_PANEL
                               or sent != ChoiceStep.SELECT and choice_step == ChoiceStep.SELECT)
                if (now < deadline and expected_handoff and (next_action or stand)
                        and frame.state == QuestState.ACTIVE and navigation.objective_action == ObjectiveAction.CHOICE
                        and not navigation.current_suspended
                        and not navigation.flags & (TravelFlags.IN_COMBAT | TravelFlags.MOUNTED | TravelFlags.FLYING
                                                    | TravelFlags.FALLING | TravelFlags.CASTING | TravelFlags.SWIMMING)):
                    self.choice_action_sent = choice_step
                    self.execution_started = True
                    ctx.log_event("quest_transport_stand" if stand else "quest_transport_option", {
                        "id": source_quest, "map": source_map, "step": choice_step.name,
                        "sequence": navigation.sequence,
                    })
                    if stand:
                        ctx.input.tap("w", hold_s=.10)
                    else:
                        ctx.input.tap(self.steps[self.index].dialog_key)
        if now >= deadline:
            self.stop(ctx, "quest_choice_handoff_unconfirmed")
        return True

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

    def strict_loot(self, ctx, navigation):
        return (not self.travel_only and (ctx.input.name != 'mock' and not isinstance(ctx.input, MockInput)
                or (getattr(navigation, 'addon_build', None) or 0) >= CORPSE_LOOT_BUILD))

    def process_required_loot(self, ctx, now, frame, nav, combat, status):
        if not self.strict_loot(ctx, nav) or ctx.cfg.nav3d.mode == 'nav3d-shadow':
            return False
        encounter = getattr(self, 'nav3d_encounter', None)
        hunt_fight = self.hunt and self.hunt.fight
        encounter_fight = getattr(encounter, 'fight', None)
        fights = (hunt_fight, self.defense and self.defense.fight,
                   encounter_fight, getattr(encounter, 'defense', None))
        for fight in fights:
            if not fight or not fight.presses or getattr(fight, 'loot_recorded', False):
                continue
            corpse = RequiredCorpseLoot(fight.target_id, fight.state_epoch, allow_approach=not self.nav3d_enabled(ctx))
            death_verified = corpse.matches(combat)
            lost = combat and not combat.unsafe and combat.target_id == 0
            credit_before_death = (nav and (fight is hunt_fight or fight is encounter_fight)
                and (nav.phase == QuestState.READY or nav.objectives_finished))
            if death_verified or lost or credit_before_death:
                fight.loot_recorded = True
                if any((item.target, item.epoch) == (corpse.target, corpse.epoch) for item in self.required_loot):
                    continue
                self.required_loot.append(corpse)
                ctx.log_event('quest_required_loot_pending', {'target': corpse.target, 'target_epoch': corpse.epoch,
                    'death_verified': death_verified})
        if (status and status[0] & (StatusFlags.IN_COMBAT | StatusFlags.DEAD | StatusFlags.GHOST)
                or nav and (nav.flags & TravelFlags.IN_COMBAT or nav.recovery and nav.recovery.life in
                           {LifeState.DEAD, LifeState.GHOST})):
            self.nearby_loot_select_until = None
            self.nearby_loot_scan = None
            return False
        coherent = (frame and nav and frame.quest_id == nav.quest_id and frame.state == nav.phase
                    and frame.map_id == nav.map_id and nav.map_id == self.map_id and frame.dialogue == Dialogue.CLOSED)
        if not self.required_loot and coherent and (nav.addon_build or 0) >= 325 and loot_prepared(nav):
            if self.check_nearby_loot(ctx, now, nav, combat):
                return True
        if not self.required_loot:
            return False
        # Prefer the visible corpse. Reacquisition can use only the verified
        # last-target path, and quest credit never clears a pending corpse.
        self.set_travel_keys(ctx, frozenset())
        corpse = next((item for item in self.required_loot if item.matches(combat)), self.required_loot[0])
        cmd = corpse.step(now, nav if coherent else None, combat)
        self.apply_travel_command(ctx, cmd)
        details = {'target': corpse.target, 'target_epoch': corpse.epoch}
        if cmd.tap:
            self.execution_started = True
            ctx.log_event('quest_required_loot_action', dict(details, key=cmd.tap))
        if cmd.outcome:
            ctx.log_event('quest_required_loot_result', dict(details, result=cmd.outcome))
            if cmd.outcome in {'corpse_cleared', 'corpse_empty'}:
                self.required_loot.remove(corpse)
            else:
                self.stop(ctx, 'quest_' + cmd.outcome)
        return True

    def check_nearby_loot(self, ctx, now, nav, combat):
        if nav.flags & LOOT_BLOCKED or not combat or combat.unsafe or nav.phase not in {
                QuestState.PENDING, QuestState.ACTIVE, QuestState.READY}:
            return False
        if (combat.target_id and combat.target_dead and nav.corpse_lootable is True
                and nav.task & TaskFlags.LOOT_IN_RANGE):
            epoch = combat.previous_target_epoch
            if epoch is None:
                epoch = combat.state_epoch
            self.required_loot.append(RequiredCorpseLoot(combat.target_id, epoch, allow_approach=False))
            self.nearby_loot_select_until = None
            if self.nearby_loot_scan is None:
                self.nearby_loot_scan = NearbyCorpseScan(now, nav, combat)
            ctx.log_event('quest_nearby_loot_pending', {'target': combat.target_id, 'target_epoch': epoch})
            return False  # Submit the confirmed corpse through the regular receipt check this tick.
        if self.nearby_loot_select_until is not None and now < self.nearby_loot_select_until:
            self.set_travel_keys(ctx, frozenset())
            return True
        if combat.target_id and not combat.target_dead:
            self.nearby_loot_scan = self.nearby_loot_select_until = None
            return False
        scan = self.nearby_loot_scan
        if scan is not None and scan.view:
            cmd = scan.step(now, nav, combat)
            if cmd.outcome in {'facing_unavailable', 'scan_budget', 'views_checked'}:
                ctx.log_event('quest_nearby_loot_scan_finished', {'reason': cmd.outcome, 'views': scan.view})
                self.nearby_loot_scan = None
                return False
            if cmd.outcome is None:
                self.apply_travel_command(ctx, cmd)
                return True
        if nav.nearby_lootable_corpse and self.nearby_loot_select_until is None:
            self.set_travel_keys(ctx, frozenset())
            self.nearby_loot_select_until = now + 1.2
            if self.nearby_loot_scan is None and combat.facing is not None and combat.turn_verified:
                self.nearby_loot_scan = NearbyCorpseScan(now, nav, combat)
            self.execution_started = True
            ctx.log_event('quest_nearby_loot_select', {'key': 'k', 'sequence': nav.sequence})
            ctx.input.tap('k')
            return True
        if scan is not None:
            scan.view += 1
            self.nearby_loot_select_until = None
            self.set_travel_keys(ctx, frozenset())
            return True
        if not nav.nearby_lootable_corpse:
            self.nearby_loot_select_until = None
        return False

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

    def set_travel_keys(self, ctx, keys, *, continue_pulse=False):
        if self.travel_pulse_pending and not continue_pulse:
            ctx.input.cancel_navigation_pulse()
            self.travel_pulse_pending = False
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
        continue_pulse = (command.keys == self.travel_keys and command.tap is None
                          and command.camera_yaw_px is None and command.outcome is None)
        self.set_travel_keys(ctx, command.keys, continue_pulse=continue_pulse)
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
            dispatch = "controller"
            if command.precise:
                start_pulse = getattr(ctx.input, "start_navigation_pulse", None)
                if start_pulse is not None and start_pulse(command.tap, command.hold_s):
                    self.travel_pulse_pending = True
                    dispatch = "native_timer"
                else:
                    ctx.input.tap_precise(command.tap, command.hold_s)
            elif command.hold_s is None:
                ctx.input.tap(command.tap)
            else:
                ctx.input.tap(command.tap, hold_s=command.hold_s)
            ctx.log_event("quest_movement_input", {"tap": command.tap, "hold_s": command.hold_s,
                                                   "result": "input_sent", "dispatch": dispatch})

    def cancel_scene_requests(self):
        if self.building_access:
            self.building_access.close()
        self.goldshire_npc_trip = None
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

    def pause_online(self):
        self.cancel_scene_requests()
        self.camera.forget()
        self.camera_receipt = self.suspend_camera = None
        self.ground_trace.clear()
        self.travel_keys, self.forward, self.travel_pulse_pending = frozenset(), False, False
        self.trip = self.forever_progress_trip = self.avoidance_trip = None
        self.forever_entry = self.forever_entry_trip = self.forever_building_segment = None
        self.arrived = False
        self.object_points = self.object_probe_sequence = self.object_check_pending = None
        self.nearby_loot_scan = self.nearby_loot_select_until = None
        if self.recovery:
            self.recovery.trip = self.recovery.near_probe = self.recovery.hover = None

    def resume_online(self, now, elapsed):
        self.sample = self.last_sequence = self.nav_sequence = None
        self.last_frame = self.last_target = now
        self.unusable_since = self.unusable_reason = None
        self.phase_started += elapsed
        self.last_progress += elapsed
        if self.round_advance_at is not None:
            self.round_advance_at += elapsed
        if self.suspend_request is not None:
            task, phase, epoch, reason, started, stage = self.suspend_request
            self.suspend_request = task, phase, epoch, reason, started + elapsed, stage
        if self.timeout_review is not None:
            review = self.timeout_review
            self.timeout_review = replace(review, started_at=review.started_at + elapsed,
                                          poll_after=review.poll_after + elapsed)
        if self.forever_progress_since is not None:
            self.forever_progress_since += elapsed
        self.OUTCOME = None

    def restore_camera(self, ctx, now, nav, *, dialogue_open=False, in_combat=False, before_detour=False):
        if self.building_access and self.building_access.locked and not in_combat and not dialogue_open:
            self.camera.cancel(ctx,now,'building_access')
            return False
        if nav.camera is not None:
            return self.adjust_scene_camera(ctx, now, nav, dialogue_open=dialogue_open, in_combat=in_combat)
        key = camera_binding_key(nav)
        if not nav.camera_restore_available or key is None:
            if self.require_prepared and not self.camera_unavailable_logged:
                ctx.log_event("quest_camera_unavailable", {"id": nav.quest_id, "key": key})
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
            ctx.log_event("quest_camera_restore_unconfirmed", {"id": nav.quest_id, "key": key})
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
        ctx.log_event("quest_camera_restore_requested", {"id": nav.quest_id, "reason": reason, "key": key})
        ctx.input.tap(key)
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

    def nav3d_mesh_quest_id(self):
        mesh = getattr(getattr(self, "nav3d_coordinator", None), "mesh", None)
        frame = getattr(mesh, "frame", None)
        if frame is None:
            return None
        parts = frame.phase_id.split(":")
        return int(parts[1]) if len(parts) > 2 and parts[0] == "quest" else None

    def nav3d_enabled(self, ctx):
        # After the mesh-exit handoff the legacy stack owns all remaining travel.
        return (getattr(getattr(ctx.cfg, "nav3d", None), "mode", "visual") != "visual"
                and not getattr(self, "nav3d_released", False))

    def release_nav3d_outside_mesh(self, ctx, sample, reason):
        from ..control.quest_travel import TravelCommand

        self.nav3d_released = True
        ctx.log_event("nav3d_exit_skipped_outside_mesh", {
            "position_xy": sample.position_xy, "world_id": sample.world_id,
        })
        ctx.log_event("quest_encounter_handoff_legacy", {"reason": reason})
        return TravelCommand()

    def begin_nav3d_exit(self, ctx, now):
        """Hand ground travel to the legacy stack via a bounded mesh exit.

        2026-11-18 split policy (user decision): Nav3D covers mesh scenes;
        open-field legs (turn-in, follow-ups) reuse the legacy straight-line
        stack. Returns a released TravelCommand when the exit segment (or a
        direct outside-mesh release) started; None falls back to the old
        stop-at-kill run end.
        """
        from ..nav3d import NavigationError
        from ..control.quest_travel import TravelCommand
        sample = getattr(self, 'nav3d_sample', None)
        position = sample.position_xy if sample is not None else None
        goal = (sample.goal_xy if sample is not None and sample.goal_xy is not None
                and sample.goal_world_id == sample.world_id else None)
        if position is not None and not self.nav3d_coordinator.tracker.candidates(position):
            return self.release_nav3d_outside_mesh(ctx, sample, "outside_mesh")
        if goal is None:
            if not getattr(self, 'nav3d_exit_goal_wait', False):
                ctx.log_event("nav3d_exit_goal_unavailable", {})
                self.nav3d_exit_goal_wait = True
            return None
        self.nav3d_exit_goal_wait = False
        try:
            self.nav3d_coordinator.begin_exit(goal, now=now, sample=sample)
        except (NavigationError, ValueError) as exc:
            ctx.log_event("nav3d_exit_begin_failed", {"reason": str(exc)})
            return None
        self.nav3d_exit_started = True
        return TravelCommand()

    def travel_nav3d(self, ctx, now, navigation):
        from ..nav3d.runtime import Nav3DCoordinator
        from ..nav3d import NavigationError
        try:
            if not hasattr(self, "nav3d_coordinator"):
                self.nav3d_coordinator = Nav3DCoordinator(ctx.cfg.nav3d, now=now, log_event=ctx.log_event,
                    resume_runs=DATA_DIR/'runs')
            command = None
            if (ctx.cfg.nav3d.mode == 'nav3d' and not self.travel_only
                    and (getattr(self, 'nav3d_encounter', None) is not None
                         or navigation is not None and navigation.quest_id == 90896)):
                from ..nav3d.encounter import RespawnEncounter
                if not hasattr(self, 'nav3d_encounter'):
                    self.nav3d_encounter = RespawnEncounter(self.nav3d_coordinator.mesh.mesh_id,
                                                          wait_budget_s=ctx.cfg.nav3d.encounter_wait_s,
                                                          log_event=ctx.log_event)
                if not getattr(self, 'nav3d_exit_started', False):
                    command = self.nav3d_encounter.step(now, getattr(self, 'nav3d_sample', None),
                        navigation, self.observation, loot_pending=bool(self.required_loot))
                    if (command and command.tap and command.tap.isdigit() and self.strict_loot(ctx, navigation)
                            and not loot_prepared(navigation)):
                        return self.stop(ctx, 'quest_loot_addon_required')
                    if (command is not None
                            and command.outcome == "quest_encounter_objective_complete_ready_for_turn_in"
                            and getattr(ctx.cfg.nav3d, "legacy_handoff", False)):
                        released = self.begin_nav3d_exit(ctx, now)
                        if released is not None:
                            command = released
                    elif (command is None and navigation.phase == QuestState.READY
                            and getattr(ctx.cfg.nav3d, "legacy_handoff", False)):
                        # Fresh run resuming with the objective already complete
                        # outside the holding region: the encounter never
                        # activates, so start the mesh exit (or a direct
                        # outside-mesh release) from the current position.
                        released = self.begin_nav3d_exit(ctx, now)
                        if released is not None:
                            command = released
                        elif getattr(self, 'nav3d_exit_goal_wait', False):
                            # Wait for the turn-in goal receipt instead of
                            # stopping with a misleading frame-mismatch reason.
                            from ..control.quest_travel import TravelCommand
                            command = TravelCommand()
            if (command is None and ctx.cfg.nav3d.mode == 'nav3d' and not self.travel_only
                    and not self.nav3d_released and not self.nav3d_exit_started
                    and navigation is not None
                    and navigation.quest_id != (self.nav3d_mesh_quest_id() or navigation.quest_id)
                    and getattr(ctx.cfg.nav3d, "legacy_handoff", False)):
                # Split policy for a quest outside this mesh's scene: release
                # to the legacy stack when off-mesh, exit the mesh first when
                # still on it; a goal inside another quest's mesh scene is not
                # configured and stops clearly (run 20261119T011845 died on
                # the mesh's quest label instead).
                sample = getattr(self, 'nav3d_sample', None)
                if (sample is None or sample.position_xy is None or
                        (sample.sequence, sample.quest_id, sample.phase, sample.ui_map_id) !=
                        (navigation.sequence, navigation.quest_id, int(navigation.phase), navigation.map_id)):
                    return self.wait_for_data(ctx, now, "nav3d_handoff_telemetry_unavailable")
                frame = self.nav3d_coordinator.mesh.frame
                if sample.world_id != frame.world_id:
                    if self.nav3d_coordinator.sequence is not None:
                        return self.stop(ctx, "nav3d_world_frame_changed")
                    command = self.release_nav3d_outside_mesh(ctx, sample, "outside_mesh_world")
                else:
                    if str(sample.client_build) != frame.client_build.rsplit('.', 1)[-1]:
                        return self.stop(ctx, "nav3d_world_frame_changed")
                    goal = sample.goal_xy if sample.goal_world_id == sample.world_id else None
                    if goal is not None and self.nav3d_coordinator.tracker.candidates(goal):
                        return self.stop(ctx, "nav3d_scene_quest_mismatch")
                    command = self.begin_nav3d_exit(ctx, now)
                    if command is None and getattr(self, 'nav3d_exit_goal_wait', False):
                        return self.wait_for_data(ctx, now, "nav3d_exit_goal_unavailable")
                if self.unusable_reason in {"nav3d_handoff_telemetry_unavailable", "nav3d_exit_goal_unavailable"}:
                    self.unusable_since, self.unusable_reason = None, None
            if command is None and not getattr(self, 'nav3d_released', False):
                command = self.nav3d_coordinator.step(now, getattr(self, "nav3d_sample", None), navigation,
                    goal_override=None if getattr(self, 'nav3d_exit_started', False)
                        else (self.object_target or self.search_waypoint),
                    target_receipt=building_target_receipt(navigation,self.observation,now) if navigation else None,
                    travel_only=self.travel_only, fast_pose=getattr(self, 'nav3d_fast_pose', None))
        except (NavigationError, OSError, ValueError) as exc:
            return self.stop(ctx, "nav3d_initialization_failed:" + str(exc))
        if command is None:
            return
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
        elif command.outcome == "nav3d_exit_reached":
            # Split policy: from the mesh exit the legacy stack owns all travel.
            self.nav3d_released = True
            self.arrived, self.trip = False, None
            ctx.log_event("quest_encounter_handoff_legacy",
                          {"position_xy": getattr(getattr(self, "nav3d_sample", None), "position_xy", None)})
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

    def forever_building_mode(self, ctx, nav):
        if (not nav or ctx.cfg.game_version != "1.60.1" or nav.map_id != 1429 or self.travel_only
                or not nav.position or not nav.destination or not nav.world_size):
            return None
        completed = self.forever_building_complete
        router = GridRouter(*nav.world_size)
        config = getattr(ctx.cfg, "nav3d", None)
        if nav.forever_step == ForeverStep.REPAIR_TRAVEL:
            exits = (
                ("fargodeep", FARGODEEP_SOUTH_ENTRY, 85, config and config.forever_fargodeep_mesh_id),
                ("echo_ridge", ECHO_RIDGE_EXTERIOR, 120, config and config.forever_echo_ridge_mesh_id),
                ("goldshire", GOLDSHIRE_INN, 32, config and config.forever_goldshire_mesh_id),
                ("abbey", NORTHSHIRE_ABBEY_ENTRY, 64, True),
            )
            for scene, entrance, radius, available in exits:
                key = nav.quest_id, nav.phase, f"{scene}_repair_exit"
                if available and key not in completed and (
                    self.forever_building_segment == (*key, "exit")
                    or (not nav.flags & TravelFlags.OUTDOORS
                        and router.distance(nav.position, entrance) <= radius)
                ):
                    return scene, "exit"
            smithy = nav.quest_id, nav.phase, "goldshire_repair_enter"
            if (config and config.forever_goldshire_mesh and config.forever_goldshire_mesh_id
                    and nav.forever_value in GOLDSHIRE_REPAIR_NPCS and smithy not in completed
                    and (self.forever_building_segment == (*smithy, "enter")
                         or router.distance(nav.position, GOLDSHIRE_SMITHY_APPROACH) <= 14)):
                return "goldshire", "enter"
            return None
        if self.forever_smithy_return and nav.forever_step == ForeverStep.NONE:
            return "goldshire", "exit"
        if (config and config.forever_fargodeep_mesh and config.forever_fargodeep_mesh_id
                and nav.quest_id == 62 and (62, nav.phase, "fargodeep") not in completed):
            distance = router.distance(nav.position, FARGODEEP_SOUTH_ENTRY)
            in_cave = not nav.flags & TravelFlags.OUTDOORS and distance <= 85
            if nav.phase == QuestState.ACTIVE and (distance <= 9 or in_cave
                    or self.forever_building_segment == (62, nav.phase, "fargodeep", "enter")):
                return "fargodeep", "enter"
            if nav.phase == QuestState.READY and (in_cave
                    or self.forever_building_segment == (62, nav.phase, "fargodeep", "exit")):
                return "fargodeep", "exit"
        if (config and config.forever_echo_ridge_mesh and config.forever_echo_ridge_mesh_id
                and nav.quest_id == 21 and (21, nav.phase, "echo_ridge") not in completed):
            entrance_distance = router.distance(nav.position, ECHO_RIDGE_EXTERIOR)
            in_mine = not nav.flags & TravelFlags.OUTDOORS and entrance_distance <= 120
            cave_objective = (nav.phase == QuestState.ACTIVE and nav.forever_step != ForeverStep.TRAVEL
                              and router.distance(nav.destination, ECHO_RIDGE_HUNT) <= 60)
            if cave_objective and (entrance_distance <= 82 or in_mine):
                return "echo_ridge", "enter"
            exiting = self.forever_building_segment == (nav.quest_id, nav.phase, "echo_ridge", "exit")
            entered = ((21, QuestState.ACTIVE, "echo_ridge") in completed
                       and nav.position[1] < ECHO_RIDGE_EXTERIOR[1] and entrance_distance <= 120)
            if nav.phase == QuestState.READY and (exiting or in_mine or entered):
                return "echo_ridge", "exit"
        if (config and config.forever_goldshire_mesh and config.forever_goldshire_mesh_id
                and (nav.quest_id, nav.phase, "goldshire") not in completed):
            inn_target = nav.quest_id == 60 and nav.phase in {QuestState.PENDING, QuestState.READY}
            inn_exit = (nav.quest_id, nav.phase, "goldshire", "exit") == self.forever_building_segment
            if inn_target and router.distance(nav.position, GOLDSHIRE_INN) <= 64:
                return "goldshire", "enter"
            inside_inn = (not nav.flags & TravelFlags.OUTDOORS
                          and router.distance(nav.position, GOLDSHIRE_INN) <= 32)
            if not inn_target and (inn_exit or inside_inn) and router.distance(nav.destination, GOLDSHIRE_INN) > 25:
                return "goldshire", "exit"
        abbey_route = "abbey_trainer" if nav.forever_step == ForeverStep.TRAVEL else "abbey"
        if (nav.quest_id, nav.phase, abbey_route) in completed:
            return None
        entrance_distance = router.distance(nav.position, NORTHSHIRE_ABBEY_ENTRY)
        in_perimeter = .476 <= nav.position[0] <= .516 and .397 <= nav.position[1] <= .437
        indoor = not nav.flags & TravelFlags.OUTDOORS
        exiting = self.forever_building_segment == (nav.quest_id, nav.phase, abbey_route, "exit")
        if (exiting or in_perimeter or (indoor and entrance_distance <= 64)) and (
                router.distance(nav.destination, NORTHSHIRE_ABBEY_ENTRY) > 55):
            return "abbey", "exit"
        giver_inside = (nav.phase in {QuestState.PENDING, QuestState.READY}
                        and router.distance(nav.destination, NORTHSHIRE_ABBEY_GIVER) <= 5)
        if entrance_distance <= 64 and (giver_inside
                or nav.phase == QuestState.READY and nav.quest_id in NORTHSHIRE_ABBEY_TURN_INS
                or nav.phase == QuestState.PENDING and nav.quest_id in NORTHSHIRE_ABBEY_PICKUPS):
            return "abbey", "enter"

    def approach_forever_building(self, ctx, now, nav, heading, *, corpse_pose=None, retreating=False):
        choice = self.forever_building_mode(ctx, nav)
        if not choice:
            return False
        scene, mode = choice
        event_prefix = f"forever_{scene}"
        scene_key = scene
        repair_shop = False
        if nav.forever_step == ForeverStep.REPAIR_TRAVEL:
            scene_key = f"{scene}_repair_{mode}"
            repair_shop = scene == "goldshire" and mode == "enter"
        elif scene == "goldshire" and self.forever_smithy_return:
            scene_key = "goldshire_smithy_exit"
        elif scene == "abbey" and nav.forever_step == ForeverStep.TRAVEL:
            scene_key = "abbey_trainer"
        if mode == "enter" and all(nav.task & flag for flag in (
                TaskFlags.NPC_SELECTED, TaskFlags.NPC_RANGE_KNOWN, TaskFlags.NPC_WITHIN_5_YARDS)):
            self.forever_building_complete.add((nav.quest_id, nav.phase, scene_key))
            return False
        pose = corpse_pose if corpse_pose is not None else self.nav3d_sample
        if (pose is None or pose.position_xy is None or pose.goal_xy is None or pose.goal_world_id != 0
                or not pose.grounded or not pose.bindings_verified
                or (pose.sequence, pose.world_id, pose.ui_map_id, pose.quest_id, pose.phase, pose.client_build)
                != (nav.sequence, 0, nav.map_id, nav.quest_id, int(nav.phase), 70959)):
            if retreating:
                return False
            self.wait_for_data(ctx, now, event_prefix + "_pose_unavailable")
            return True
        if scene == "echo_ridge" and mode == "enter" and corpse_pose is None:
            # The native hunt pin can be deep in the mine. Enter far enough
            # to search for workers on a connected floor before pursuing it.
            pose = replace(pose, goal_xy=ECHO_RIDGE_HUNT_XY)
        if scene == "fargodeep" and mode == "enter" and corpse_pose is None:
            pose = replace(pose, goal_xy=FARGODEEP_SCOUT_XY)
        from ..nav3d import NavigationError, Query
        from ..nav3d.follower import WorldRouteFollower
        from ..nav3d.runtime import Nav3DCoordinator
        segment = nav.quest_id, nav.phase, scene_key, mode

        def decline_retreat(reason):
            self.cave_retreat_failed = True
            self.set_travel_keys(ctx, frozenset())
            self.forever_building_segment = self.forever_entry = None
            ctx.log_event("forever_echo_ridge_exit_retreat_unconfirmed", {"reason": reason})
            return False

        if segment != self.forever_building_segment:
            self.camera.cancel(ctx, now, event_prefix + "_mesh")
            previous = self.forever_entry
            previous_scene = self.forever_building_segment and self.forever_building_segment[2]
            self.forever_entry_trip = None
            self.forever_scene_wait_at = None
            self.forever_building_segment = segment
            try:
                config = replace(ctx.cfg.nav3d, mode="nav3d")
                if scene == "goldshire":
                    config = replace(config, mesh=config.forever_goldshire_mesh, alignment_file="",
                                     supervised_trial_mesh_id=config.forever_goldshire_mesh_id)
                elif scene == "echo_ridge":
                    config = replace(config, mesh=config.forever_echo_ridge_mesh, alignment_file="",
                                     supervised_trial_mesh_id=config.forever_echo_ridge_mesh_id)
                elif scene == "fargodeep":
                    config = replace(config, mesh=config.forever_fargodeep_mesh, alignment_file="",
                                     supervised_trial_mesh_id=config.forever_fargodeep_mesh_id)
                short_steps = scene in {"echo_ridge", "fargodeep", "abbey"}
                self.forever_entry = Nav3DCoordinator(config, now=now,
                    log_event=ctx.log_event, resume_runs=DATA_DIR / "runs",
                    scene_phases=(f"quest:{nav.quest_id}:{int(nav.phase)}",) if mode == "enter" else (),
                    edge_tolerance=.23 if corpse_pose is not None or mode == "exit" and scene in {"abbey", "echo_ridge"}
                    else .14,
                    short_steps=short_steps)
                if mode == "exit":
                    same_surface = (previous_scene == scene_key or
                                    (previous_scene, scene_key) == ("goldshire_repair_enter", "goldshire_smithy_exit"))
                    if (previous and same_surface and previous.tracker.last
                            and math.dist(previous.tracker.last[:2], pose.position_xy) <= 2):
                        self.forever_entry.tracker.last = previous.tracker.last
                    if scene == "abbey":
                        if nav.destination[1] < .403:
                            handoff = (NORTHSHIRE_EXIT_NORTH_EAST if nav.position[0] > .498
                                       else NORTHSHIRE_EXIT_NORTH)
                        elif nav.destination[0] >= .505:
                            handoff = NORTHSHIRE_EXIT_TRAINER
                        else:
                            handoff = NORTHSHIRE_EXIT_SOUTH
                    elif scene == "fargodeep":
                        handoff = FARGODEEP_SOUTH_ENTRY_XY
                    elif scene_key == "goldshire_smithy_exit":
                        handoff = GOLDSHIRE_SMITHY_APPROACH_XY
                    else:
                        handoff = GOLDSHIRE_HANDOFF_XY if scene == "goldshire" else ECHO_RIDGE_HANDOFF_XY
                    self.forever_entry.begin_exit(pose.goal_xy, now=now, sample=pose, handoff=handoff)
            except (OSError, ValueError, NavigationError) as exc:
                if retreating:
                    return decline_retreat(str(exc))
                self.stop(ctx, event_prefix + "_mesh_unavailable:" + str(exc))
                return True
        coordinator = self.forever_entry
        tracker = coordinator.tracker
        inside_mine = scene in {"echo_ridge", "fargodeep"} and not nav.flags & TravelFlags.OUTDOORS
        at_inn_door = scene == "goldshire" and math.dist(pose.position_xy, GOLDSHIRE_INN_DOOR_XY) <= 8
        if at_inn_door and not coordinator.short_steps:
            coordinator.short_steps = True
            coordinator.follower = coordinator.route = None
            self.set_travel_keys(ctx, frozenset())
            ctx.log_event("forever_goldshire_door_precision", {"id": nav.quest_id, "position": pose.position_xy})

        def supports_to(goal, candidates):
            connected = []
            for point in candidates:
                try:
                    route = tracker.mesh.plan(Query(tracker.mesh.frame, point, goal,
                                                    horizontal_snap=tracker.edge_tolerance))
                    if scene in {"echo_ridge", "fargodeep"}:
                        WorldRouteFollower.validate_route(route)
                    if scene == "fargodeep" and route.length > 95:
                        continue
                except NavigationError:
                    continue
                connected.append(point)
            return connected

        interior_exit = inside_mine or scene_key == "goldshire_smithy_exit"
        if mode == "exit" and interior_exit and coordinator.sequence is None and tracker.last is None:
            candidates = tracker.candidates(pose.position_xy, edge_tolerance=tracker.edge_tolerance)
            connected = supports_to(coordinator.exit_candidates[0], candidates)
            if len(connected) != 1:
                if retreating:
                    return decline_retreat("interior_exit_support_unconfirmed")
                self.wait_for_data(ctx, now, event_prefix + "_exit_support_unconfirmed")
                return True
            tracker.last = connected[0]
            ctx.log_event(event_prefix + "_interior_exit_support", {"position": tracker.last})
        if mode == "exit" and self.forever_entry_trip is not None:
            command = self.forever_entry_trip.step(now, nav, heading, heading_age=ctx.heading_age())
            self.apply_travel_command(ctx, command)
            if command.outcome == "arrived":
                self.forever_entry_trip = None
                self.forever_building_segment = None
            elif command.outcome:
                self.stop(ctx, event_prefix + "_exit_rejoin_" + command.outcome)
            return True
        if mode == "enter" and coordinator.sequence is None:
            support = tracker.candidates(pose.position_xy, edge_tolerance=tracker.edge_tolerance)
            abbey_outside = scene == "abbey" and nav.flags & TravelFlags.OUTDOORS
            abbey_inside = scene == "abbey" and not abbey_outside
            inn_inside = at_inn_door and not nav.flags & TravelFlags.OUTDOORS
            from_north = abbey_outside and nav.position[1] < .416
            low_goal = None
            if (abbey_inside or inn_inside or repair_shop
                    or (abbey_outside or scene in {"echo_ridge", "fargodeep"})
                    and (len(support) == 1 or inside_mine)):
                # The roof may be the only support returned at a measured XY.
                # Confirm a route to the lower goal before entering its mesh.
                goals = tracker.candidates(pose.goal_xy, edge_tolerance=tracker.edge_tolerance)
                low_goal = goals[0] if goals else None
                support = supports_to(low_goal, support) if low_goal else []
                if (inside_mine or abbey_inside or inn_inside or repair_shop) and len(support) == 1 and tracker.last is None:
                    tracker.last = support[0]
                    ctx.log_event(event_prefix + "_interior_support", {"position": support[0]})
            # Each mesh has an exterior point with a complete route to the
            # lowest candidate surface at the indoor quest giver.
            if len(support) != 1:
                if self.forever_entry_trip is None:
                    radius = .9
                    if repair_shop:
                        exterior = GOLDSHIRE_SMITHY_APPROACH
                    elif scene == "goldshire":
                        if inn_inside:
                            rejoin = GOLDSHIRE_INN_DOOR_REJOIN
                            if (low_goal is None or not .3 < math.dist(pose.position_xy, rejoin) <= 2
                                    or len(supports_to(low_goal, tracker.candidates(rejoin))) != 1):
                                self.wait_for_data(ctx, now, event_prefix + "_door_support_unconfirmed")
                                return True
                            dx, dy = (b - a for a, b in zip(pose.position_xy, rejoin))
                            exterior = nav.position[0] + dx / nav.world_size[0], nav.position[1] - dy / nav.world_size[1]
                            radius = .22
                        else:
                            exterior = GOLDSHIRE_INN_EXTERIOR
                    elif scene == "echo_ridge":
                        if nav.flags & TravelFlags.OUTDOORS:
                            exterior = ECHO_RIDGE_EXTERIOR
                        elif .35 < math.dist(pose.position_xy, ECHO_RIDGE_WALL_REJOIN_XY) <= 2:
                            dx, dy = (b - a for a, b in zip(pose.position_xy, ECHO_RIDGE_WALL_REJOIN_XY))
                            exterior = nav.position[0] + dx / nav.world_size[0], nav.position[1] - dy / nav.world_size[1]
                            radius = .25
                        else:
                            self.set_travel_keys(ctx, frozenset())
                            if self.forever_scene_wait_at is None:
                                self.forever_scene_wait_at = now
                            elif now - self.forever_scene_wait_at > 8:
                                self.stop(ctx, event_prefix + "_interior_support_unconfirmed")
                            return True
                    elif scene == "fargodeep":
                        if inside_mine:
                            self.wait_for_data(ctx, now, event_prefix + "_interior_support_unconfirmed")
                            return True
                        exterior = FARGODEEP_SOUTH_ENTRY
                    else:
                        exterior = NORTHSHIRE_NORTH_REJOIN if from_north else NORTHSHIRE_ABBEY_ENTRY
                        if abbey_outside and low_goal:
                            nearby = sorted(NORTHSHIRE_REJOINS, key=lambda point: math.dist(pose.position_xy, point))
                            for point in nearby:
                                if not .35 < math.dist(pose.position_xy, point) <= 4:
                                    continue
                                ground = tracker.candidates(point, edge_tolerance=tracker.edge_tolerance)
                                if len(ground) == 1 and supports_to(low_goal, ground):
                                    dx, dy = (b - a for a, b in zip(pose.position_xy, point))
                                    exterior = nav.position[0] + dx / nav.world_size[0], nav.position[1] - dy / nav.world_size[1]
                                    radius = .25
                                    break
                    self.forever_entry_trip = QuestTrip(nav.world_size, exterior,
                        ctx.cfg.navigation.facing_convention, arrival_radius=radius, walk_only=True,
                        local_travel=True, log_event=ctx.log_event)
                    ctx.log_event(event_prefix + "_mesh_rejoin", {"position": pose.position_xy,
                                                                 "exterior_point": exterior})
                command = self.forever_entry_trip.step(now, nav, heading, heading_age=ctx.heading_age())
                self.apply_travel_command(ctx, command)
                if command.outcome != "arrived":
                    if command.outcome:
                        self.stop(ctx, event_prefix + "_rejoin_" + command.outcome)
                    return True
                self.stop(ctx, event_prefix + "_rejoin_support_unconfirmed")
                return True
            if self.forever_entry_trip is not None:
                self.set_travel_keys(ctx, frozenset())
                self.forever_entry_trip = None
        self.unusable_since = self.unusable_reason = None
        self.forever_scene_wait_at = None
        # A worker can be selected across a tunnel wall. The cave handoff
        # needs actual movement into the chamber, even with a live target.
        target_receipt = (None if scene in {"echo_ridge", "fargodeep"}
                          else building_target_receipt(nav, self.observation, now))
        command = coordinator.step(now, pose, nav, target_receipt=target_receipt, travel_only=True,
                                   retreating=retreating)
        if corpse_pose is not None and command.tap == "w" and command.hold_s is not None:
            command = replace(command, hold_s=min(.055, command.hold_s))
        self.apply_travel_command(ctx, command)
        if command.outcome in {"nav3d_route_arrived", "nav3d_target_confirmed", "nav3d_exit_reached"}:
            self.forever_building_complete.add((nav.quest_id, nav.phase, scene_key))
            if scene_key == "goldshire_smithy_exit":
                self.forever_smithy_return = False
            self.arrived = mode == "enter"
            self.trip = None
            ctx.log_event(event_prefix + "_handoff", {"position": pose.position_xy, "basis": command.outcome,
                                                     "next": "interaction" if self.arrived else "outdoor_travel"})
        elif command.outcome:
            if retreating:
                return decline_retreat(command.outcome)
            if (scene == "abbey" and mode == "exit"
                    and command.outcome in {"nav3d_exit_unreachable", "nav3d_support_missing",
                                            "nav3d_displacement_discontinuity"}):
                point = min(NORTHSHIRE_REJOINS, key=lambda p: math.dist(pose.position_xy, p))
                rejoin = segment, point
                if rejoin not in self.forever_exit_rejoins and .35 < math.dist(pose.position_xy, point) <= 4:
                    dx, dy = (b - a for a, b in zip(pose.position_xy, point))
                    exterior = nav.position[0] + dx / nav.world_size[0], nav.position[1] - dy / nav.world_size[1]
                    self.forever_exit_rejoins.add(rejoin)
                    self.forever_entry_trip = QuestTrip(nav.world_size, exterior,
                        ctx.cfg.navigation.facing_convention, arrival_radius=.25, walk_only=True,
                        local_travel=True, log_event=ctx.log_event)
                    ctx.log_event("forever_abbey_exit_rejoin", {"position": pose.position_xy, "goal": point,
                                                               "reason": command.outcome})
                    return True
            if mode == "enter" and nav.phase == QuestState.PENDING and command.outcome == "nav3d_candidates_exhausted":
                ctx.log_event(event_prefix + "_entry_unconfirmed", {"id": nav.quest_id, "reason": command.outcome})
                self.stop(ctx, "quest_building_access_unconfirmed")
            else:
                self.stop(ctx, command.outcome)
        return True

    def maintain_forever_character(self, ctx, now, nav, heading, dialogue):
        step = nav.forever_step if nav and (nav.addon_build or 0) >= 291 else ForeverStep.NONE
        if step == ForeverStep.NONE:
            if self.forever_progress is not None:
                self.set_travel_keys(ctx, frozenset())
                self.trip = None
                self.arrived = False
                if self.hunt and self.hunt.fight is None:
                    self.hunt.reset_search()
            self.forever_progress = self.forever_progress_trip = self.forever_progress_since = None
            self.forever_npc_scan = self.forever_npc_selects = self.forever_repair_interacts = 0
            return False
        if nav.sequence != self.nav_sequence:
            self.nav_sequence, self.nav_at = nav.sequence, now
        if now - self.nav_at > 1.2:
            self.stop(ctx, "quest_navigation_channel_stale")
            return True
        if nav.flags & (TravelFlags.IN_COMBAT | TravelFlags.FALLING | TravelFlags.FLYING | TravelFlags.CASTING
                        | TravelFlags.SWIMMING):
            self.set_travel_keys(ctx, frozenset())
            return True
        value = nav.forever_value
        repair = step == ForeverStep.REPAIR_TRAVEL
        service = "repair" if repair else "trainer"
        if self.forever_progress != (step, value):
            self.forever_progress = step, value
            if step in FOREVER_SERVICE_TRAVEL:
                self.forever_progress_since = now
                self.forever_progress_trip = None
                self.forever_npc_scan = self.forever_npc_selects = self.forever_repair_interacts = 0
            ctx.log_event("forever_progression", {"step": step.name, "value": value,
                                                  "sequence": nav.sequence})
        if self.forever_progress_since is None:
            self.forever_progress_since = now
        if step not in FOREVER_SERVICE_TRAVEL:
            self.set_travel_keys(ctx, frozenset())
            if (step in {ForeverStep.REPAIR, ForeverStep.REPAIR_CANNOT_PAY, ForeverStep.REPAIR_CLOSE}
                    and nav.destination and nav.position and nav.world_size
                    and ctx.cfg.nav3d.forever_goldshire_mesh_id
                    and GridRouter(*nav.world_size).distance(nav.destination, GOLDSHIRE_SMITHY_APPROACH) <= 25
                    and GridRouter(*nav.world_size).distance(nav.position, nav.destination) <= 8):
                self.forever_smithy_return = True
            if step in {ForeverStep.TRAINER_GOSSIP, ForeverStep.LEARN, ForeverStep.TALENT, ForeverStep.MACRO,
                        ForeverStep.TRAINER_FINISH, ForeverStep.EQUIP, ForeverStep.REPAIR_GOSSIP,
                        ForeverStep.REPAIR, ForeverStep.REPAIR_CANNOT_PAY,
                        ForeverStep.REPAIR_CLOSE} and now - self.forever_progress_at >= 1.5:
                self.forever_progress_at = now
                ctx.input.tap("f4")
            elif (step == ForeverStep.TRAINER_WAIT and now - self.forever_progress_since >= 95
                    and now - self.forever_progress_at >= 1.5):
                self.forever_progress_at = now
                ctx.input.tap("f4")
            return True
        if (dialogue != Dialogue.CLOSED or not nav.interact_verified or not nav.flags & TravelFlags.KEYS_VERIFIED
                or nav.destination_map != nav.map_id or not nav.destination or not nav.position or not nav.world_size):
            self.set_travel_keys(ctx, frozenset())
            return True
        if now - self.forever_progress_since > (180 if repair else 95):
            self.set_travel_keys(ctx, frozenset())
            if now - self.forever_progress_at >= 1.5:
                self.forever_progress_at = now
                ctx.input.tap("f4")
            return True
        if all(nav.task & flag for flag in (TaskFlags.NPC_SELECTED, TaskFlags.NPC_RANGE_KNOWN,
                                           TaskFlags.NPC_WITHIN_5_YARDS)):
            self.set_travel_keys(ctx, frozenset())
            if now - self.forever_progress_at >= 1.7:
                self.forever_progress_at = now
                if repair and self.forever_repair_interacts >= 3:
                    ctx.log_event("forever_repair_interaction_unconfirmed", {"npc": value})
                    ctx.input.tap("f4")
                else:
                    if repair:
                        self.forever_repair_interacts += 1
                    ctx.input.tap("f")
            return True
        distance = GridRouter(*nav.world_size).distance(nav.position, nav.destination)
        if (nav.task & TaskFlags.NPC_CANDIDATE and not nav.task & TaskFlags.NPC_SELECTED
                and distance <= 5 and self.forever_npc_selects < 2):
            self.set_travel_keys(ctx, frozenset())
            if now - self.forever_progress_at >= .8:
                self.forever_progress_at = now
                self.forever_npc_selects += 1
                ctx.log_event(f"forever_{service}_select", {"attempt": self.forever_npc_selects,
                                                           "distance": round(distance, 1)})
                ctx.input.tap("k")
            return True
        if self.approach_forever_building(ctx, now, nav, heading):
            return True
        if self.forever_progress_trip is None:
            road = self.northshire_south_road(nav, nav.destination) if repair else ()
            if repair and value in GOLDSHIRE_REPAIR_NPCS and ctx.cfg.nav3d.forever_goldshire_mesh_id:
                road = (*road, GOLDSHIRE_SMITHY_APPROACH)
            self.forever_progress_trip = QuestTrip(nav.world_size, nav.destination, ctx.cfg.navigation.facing_convention,
                                                  arrival_radius=2.2, walk_only=True, approach_on_foot=True,
                                                  outdoor_detour=repair, ground_checkpoints=road,
                                                  log_event=ctx.log_event)
        trip = self.forever_progress_trip
        command = trip.step(now, nav, heading, heading_age=ctx.heading_age())
        if command.outcome == "arrived":
            self.set_travel_keys(ctx, frozenset())
            if now - self.forever_progress_at >= .8:
                self.forever_progress_at = now
                self.forever_npc_scan += 1
                self.forever_npc_selects = 0
                if self.forever_npc_scan <= 12:
                    ctx.input.tap("left", hold_s=.13)
                else:
                    ctx.input.tap("f4")
        elif command.outcome:
            self.set_travel_keys(ctx, frozenset())
            if now - self.forever_progress_at >= 1.5:
                self.forever_progress_at = now
                ctx.log_event(f"forever_{service}_route_unconfirmed", {"reason": command.outcome})
                ctx.input.tap("f4")
        else:
            self.apply_travel_command(ctx, command)
        return True

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

    @staticmethod
    def northshire_south_road(nav, destination):
        if (nav.map_id == 1429 and nav.flags & TravelFlags.OUTDOORS and nav.position
                and .44 <= nav.position[0] <= .52 and .418 <= nav.position[1] < .496
                and destination[1] >= .52):
            return NORTHSHIRE_SOUTH_ROAD
        return ()

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
        if (ctx.cfg.game_version == "1.60.1" and navigation.quest_id == 62 and self.phase == QuestState.ACTIVE
                and navigation.map_id == 1429 and ctx.cfg.nav3d.forever_fargodeep_mesh_id and not self.travel_only):
            destination = FARGODEEP_SOUTH_ENTRY
        local_search = self.search_waypoint is not None
        live_object = bool(self.object_target or navigation.objective_pin and not local_search)
        if self.accepted_from_offer and self.phase == QuestState.READY and not (local_search or live_object):
            # An immediately ready follow-up can briefly inherit the pickup pin.
            pin = self.accepted_turn_in_pin
            if pin is None or pin[0] != destination:
                self.accepted_turn_in_pin = (destination, now, navigation.sequence)
                self.trip = None
                self.set_travel_keys(ctx, frozenset())
                return
            if now - pin[1] < 1 or navigation.sequence == pin[2]:
                return
        map_key = (navigation.map_id, navigation.world_size)
        map_changed = self.ground_router is not None and self.ground_router[0] != map_key
        if (self.trip is None or map_changed
                or self.trip.router.distance(self.trip.destination, destination) > (4 if live_object else 8)):
            # A kill objective is an area marker. Start checking targets before
            # walking into whatever occupies the exact center of that area.
            if self.phase == QuestState.READY and not self.travel_only:
                # This is only the map-marker search area. Actual NPC range has
                # its own receipt; walking onto the pin cannot prove arrival.
                radius = 4
            elif live_object and not self.travel_only:
                radius = self.object_approach_yards
            elif (navigation.quest_id == 95998 and self.phase == QuestState.ACTIVE
                    and ctx.cfg.game_version == "1.60.1" and (navigation.addon_build or 0) >= 312):
                radius = 6
            else:
                radius = 14 if self.hunt is not None and self.search_waypoint is None else 4
            road = (self.northshire_south_road(navigation, destination)
                    if self.require_prepared and ctx.cfg.game_version == "1.60.1"
                    and not (local_search or live_object) else ())
            self.trip = QuestTrip(navigation.world_size, destination, ctx.cfg.navigation.facing_convention,
                                  arrival_radius=radius, local_travel=live_object or local_search,
                                  walk_only=bool(self.building_access and self.building_access.ground_only),
                                  approach_on_foot=live_object or self.phase in {QuestState.READY, QuestState.PENDING},
                                  outdoor_detour=self.require_prepared and ctx.cfg.game_version == "1.60.1",
                                  ground_checkpoints=road,
                                  log_event=ctx.log_event)
            if self.ground_router is None or self.ground_router[0] != map_key:
                self.ground_router = (map_key, self.trip.router)
                self.terrain_attempts.clear()
            else:
                self.trip.router = self.ground_router[1]
            source = ("northshire_south_road" if road else "object_minimap" if self.object_target else
                      "object_pin" if live_object else "local_search" if local_search else
                      "apr_objective" if navigation.apr_waypoint else "quest_waypoint")
            ctx.log_event("quest_route", {"id": navigation.quest_id, "map": navigation.map_id,
                                          "destination": destination, "arrival_radius": radius,
                                          "source": source})
        if self.road_status is not None:
            identity = self.road_status.identity
            scope = self.trip, identity
            traces = (() if live_object or local_search or self.building_pending or self.trip.ground_checkpoints
                      else self.road_scan.traces(self.road_status))
            previous_trip = self.road_trip_identity
            identity_changed = previous_trip is not None and previous_trip[0] is self.trip and previous_trip != scope
            new_roads = traces and not self.trip.road_traces and not self.trip.road_disabled
            if (identity_changed or new_roads) and self.trip.mode in {"walk", "mount"} and self.trip.phase == "cruise":
                self.trip.enter("choose", now)
                self.trip.road_origin = None
                self.set_travel_keys(ctx, frozenset())
            self.trip.road_traces = traces
            self.road_trip_identity = scope
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
        if (command.outcome == "route_blocked" and local_search and self.hunt
                and ctx.cfg.game_version == "1.60.1" and navigation.flags & TravelFlags.OUTDOORS
                and navigation.position is not None):
            # This is a search sample. The objective does not require this route.
            self.trip.router.block_ahead(navigation.position, self.trip.motion_goal or destination)
            ctx.log_event("quest_hunt_waypoint_blocked", {"id": navigation.quest_id,
                "position": navigation.position, "waypoint": self.search_waypoint})
            return self.route_blocked(ctx, navigation)
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
        # 2026-11-18 split policy: after the mesh handoff the legacy leg is
        # open-field travel. Building scenes belong to Nav3D; the legacy visual
        # probes must not hijack the turn-in route (run 20261118T144009
        # suspended on quest_building_access_unconfirmed instead of flying).
        if getattr(self, "nav3d_released", False):
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
        if (ctx.input.name != "helper" or self.trip.mode not in {"walk", "mount"}
                or nav.position is None or len(self.terrain_attempts) >= 4
                or any(self.trip.router.distance(nav.position, p) < 12 for p in self.terrain_attempts)):
            return False
        segmented = False
        config = getattr(ctx.cfg, "segmented_navigation", None)
        if (config and config.enabled and nav.overhead_camera_available and nav.camera
                and nav.flags & TravelFlags.OUTDOORS and self.trip.mode == "walk"
                and not nav.flags & TravelFlags.MOUNTED):
            from ..control.segmented_detour import preflight
            try:
                preflight(config, nav)
            except (OSError, ValueError, KeyError) as exc:
                ctx.log_event("quest_segmented_preflight_failed", {"reason": str(exc), "mode": "travel"})
            else:
                segmented = True
        if not segmented and not local_vision_available():
            return False
        self.set_forward(ctx, False)
        if self.restore_camera(ctx, now, nav, before_detour=True):
            return True
        self.terrain_attempts.append(nav.position)
        self.road_scan.close()
        self.trip.router.block_ahead(nav.position, self.trip.motion_goal or self.trip.destination)
        self.terrain = TerrainDetour(max(now, self.camera_settle_until), nav, heading, self.trip, client,
                                     ground_trace=self.ground_trace, player_identity=self.terrain_identity(now))
        if segmented:
            self.terrain.begin_model_route(ctx, max(now, self.camera_settle_until), nav, heading,
                                           "outdoor_route_blocked", travel_goal=self.trip.destination)
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
            self.forever_building_segment, self.forever_entry, self.forever_entry_trip = None, None, None
            self.forever_building_complete.clear()
            self.forever_exit_rejoins.clear()
            self.forever_corpse_goal = None
            self.consumables = ConsumableRecovery()
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
            lambda keys: self.set_travel_keys(ctx, keys), frontmost_game_bounds,
            ground_route=lambda nav, corpse, heading: self.forever_corpse_route(ctx, now, nav, corpse, heading))
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
            self.forever_building_segment, self.forever_entry, self.forever_entry_trip = None, None, None
            self.forever_building_complete.clear()
            self.forever_exit_rejoins.clear()
            self.forever_corpse_goal = None
            if self.round_advance_at is not None:
                self.round_advance_at = now
            if self.followup_started is not None:
                self.followup_started = now
            ctx.stats["quest_recoveries"] = ctx.stats.get("quest_recoveries", 0) + 1
            ctx.log_event("quest_recovery_complete", {"id": self.steps[self.index].quest_id})
        elif result:
            self.stop(ctx, "quest_recovery_" + result)
        return True

    def forever_corpse_route(self, ctx, now, nav, corpse, heading):
        if (ctx.cfg.game_version != "1.60.1" or nav.quest_id != 21 or nav.map_id != 1429
                or nav.recovery.corpse_source not in {CorpseSource.CURRENT_MAP, CorpseSource.RECORDED_DEATH}
                or nav.recovery.walk_verified is not True
                or GridRouter(*nav.world_size).distance(corpse, ECHO_RIDGE_HUNT) > 60):
            return False
        nav = replace(nav, destination=corpse, destination_map=nav.map_id, flags=nav.flags | TravelFlags.KEYS_VERIFIED)
        if not self.forever_building_mode(ctx, nav):
            return False
        pose = getattr(self, "nav3d_sample", None)
        if (pose is None or pose.world_id != 0 or pose.sequence != nav.sequence
                or pose.position_xy is None or pose.in_combat
                or nav.flags & (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING
                                | TravelFlags.SWIMMING | TravelFlags.CASTING)):
            self.wait_for_data(ctx, now, "forever_corpse_mesh_pose_unavailable")
            return True
        if self.forever_corpse_goal is None or self.forever_corpse_goal[0] != corpse:
            dx = (corpse[0] - nav.position[0]) * nav.world_size[0]
            dy = (nav.position[1] - corpse[1]) * nav.world_size[1]
            self.forever_corpse_goal = corpse, (pose.position_xy[0] + dx, pose.position_xy[1] + dy)
        pose = replace(pose, goal_xy=self.forever_corpse_goal[1],
                       goal_world_id=0, grounded=True, bindings_verified=True)
        return self.approach_forever_building(ctx, now, nav, heading, corpse_pose=pose)

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
        if (ctx.cfg.game_version == "1.60.1" and nav.map_id == 1429 and nav.quest_id == 21
                and not nav.flags & TravelFlags.OUTDOORS):
            return self.echo_ridge_combat_side_step(ctx, nav, facing)
        return None

    def echo_ridge_combat_side_step(self, ctx, nav, facing):
        pose = getattr(self, "nav3d_sample", None)
        config = ctx.cfg.nav3d
        if (pose is None or not pose.grounded or not pose.bindings_verified or not pose.position_xy
                or (pose.sequence, pose.world_id, pose.ui_map_id, pose.quest_id, pose.phase)
                != (nav.sequence, 0, nav.map_id, nav.quest_id, int(nav.phase))
                or not config.forever_echo_ridge_mesh or not config.forever_echo_ridge_mesh_id):
            return None
        from ..nav3d import NavMesh, NavigationError, Query
        from ..nav3d.runtime import SurfaceTracker

        try:
            mesh = NavMesh.load(config.forever_echo_ridge_mesh)
        except (OSError, ValueError, NavigationError):
            return None
        if mesh.mesh_id != config.forever_echo_ridge_mesh_id or pose.client_build != 70959:
            return None
        tracker = SurfaceTracker(mesh)
        starts = tracker.candidates(pose.position_xy, edge_tolerance=.5)
        for key, direction in (("q", -1), ("e", 1)):
            dx, dy = math.cos(facing) * direction, math.sin(facing) * direction
            end = pose.position_xy[0] + dx, pose.position_xy[1] + dy
            goals = tracker.candidates(end)
            if len(goals) != 1:
                continue
            goal = goals[0]
            for start in starts:
                if abs(start[2] - goal[2]) > .6:
                    continue
                try:
                    route = mesh.plan(Query(mesh.frame, start, goal, horizontal_snap=.5))
                except NavigationError:
                    continue
                if route.length <= 1.25:
                    ctx.log_event("forever_echo_ridge_combat_clearance", {
                        "key": key, "position": pose.position_xy, "destination": end,
                    })
                    return key
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
        encounter = getattr(self, 'nav3d_encounter', None)
        if encounter and encounter.active:
            return 'encounter_' + (encounter.state or 'waiting')
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
        if self.timeout_review is not None:
            return "timeout_model_wait"
        if navigation and navigation.flags & TravelFlags.IN_COMBAT:
            return "combat"
        if (self.required_loot or self.nearby_loot_scan is not None
                or self.nearby_loot_select_until is not None and now < self.nearby_loot_select_until):
            return "required_corpse_loot"
        if self.terrain:
            cloud = getattr(self.terrain, "model_detour", None)
            return "terrain_model_wait" if (getattr(self.terrain, "request", None) is not None
                or cloud is not None and cloud.request is not None) else "terrain_observation_or_move"
        if self.object_scene is not None or getattr(self.planner, "future", None) is not None:
            return "model_wait"
        if self.camp_attempt_at is not None and navigation and navigation.quest_id == 95998:
            return "camp_receipt_wait"
        if self.camera.pending:
            return "camera_receipt_wait"
        if self.npc_step is not None:
            return "npc_movement_receipt_wait"
        if self.npc_alignment is not None:
            return "npc_heading_alignment"
        if self.npc_scan_started is not None:
            return "npc_search"
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
        if self.hunt is not None and self.arrived and self.hunt.fight is None:
            return "combat_target_search"
        if now - self.last_action < 2:
            return "action_receipt_or_cooldown"
        return "unclassified_wait"

    def publish_activity(self, ctx, now, activity, nav, frame):
        send = getattr(ctx.input, 'publish_run_status', None)
        if (not self.require_prepared or self.travel_only or self.status_wire_failed or not send
                or ctx.cfg.nav3d.mode == 'nav3d-shadow' or not nav or not nav.run_status_ready):
            return
        if activity == 'combat' or activity in {'encounter_fighting', 'encounter_defending'}:
            code = 9 if self.required_loot else 4
        elif activity == 'required_corpse_loot' or activity == 'loot_receipt_wait':
            code = 5
        elif activity.startswith('npc_'):
            code = 6
        elif frame and frame.dialogue != Dialogue.CLOSED:
            code = 7
        elif activity == 'combat_target_search' or activity.startswith('encounter_'):
            code = 3
        elif activity == 'travel' or activity.startswith('building_'):
            code = 2
        else:
            code = 8 if 'wait' in activity and activity != 'unclassified_wait' else 1
        if send(code):
            self.status_wire_at = now
            if code != self.status_wire_code:
                self.status_wire_code = code
                ctx.log_event('quest_run_status_sent', {'activity': activity, 'code': code, 'sequence': nav.sequence})
        else:
            self.status_wire_failed = True
            ctx.log_event('quest_run_status_unavailable', {'activity': activity, 'sequence': nav.sequence})

    def select_objective_mode(self, ctx, now, navigation, progress, measurable, targets):
        native = navigation.objective_action, navigation.counted_drop
        identity = navigation.map_id, navigation.quest_id, navigation.objective_index
        text_identity = self.quest_text.identity
        matching = text_identity is not None and text_identity[:3] == identity
        verified = matching and text_identity[-1]
        if verified and navigation.quest_id != 95998:
            return native
        signature = identity + ((text_identity,) if matching else ())
        if signature != self.quest_mode_identity:
            self.quest_mode_identity = signature
            self.quest_mode_started = now
            self.quest_mode_result = None
        if self.quest_mode_result is not None:
            return self.quest_mode_result

        self.set_travel_keys(ctx, frozenset())
        text = self.quest_text.text if matching else None
        elapsed = now - self.quest_mode_started
        reason = None
        if text is None or not text.objective.strip():
            if elapsed < 6:
                return None
            reason = "objective_text_unavailable"
        elif elapsed >= 32:
            reason = "decision_timeout"
        else:
            state = {
                "quest_id": navigation.quest_id, "map_id": navigation.map_id,
                "objective_index": navigation.objective_index, "title": text.title,
                "objective_text": text.objective, "quest_description": text.description,
                "native_type": text.native_type, "native_action": native[0].name,
                "progress": list(progress), "observation_age_ms": round((now - self.nav_at) * 1000),
            }
            try:
                result = self.planner.poll(("objective_mode", *signature), state, QUEST_MODES,
                                           instructions=QUEST_MODE_INSTRUCTIONS, max_age_s=30)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                reason = str(exc)
            else:
                if result is None:
                    return None
                recommendation = result.get("recommendation")
                resolved = executable_objective_mode(recommendation, navigation,
                                                     measurable=measurable, targets=targets)
                accepted = resolved is not None and (not verified or resolved[0] == native[0])
                ctx.log_event("quest_objective_mode_decision", {"id": navigation.quest_id,
                    "objective": navigation.objective_index, "accepted": accepted, **state, **result})
                if accepted:
                    self.quest_mode_result = resolved[0], resolved[1] or native[1]
                    return self.quest_mode_result
                reason = result.get("reason") or "mode_unavailable_or_unverified"
        ctx.log_event("quest_objective_mode_unconfirmed", {"id": navigation.quest_id,
            "objective": navigation.objective_index, "reason": reason})
        if verified or (navigation.quest_id == 95998 and native[0] == ObjectiveAction.ARRIVE):
            self.quest_mode_result = native
            return native
        return self.stop(ctx, "quest_mode_unconfirmed")

    def binding_transition(self, ctx, now, navigation):
        previous = self.camera_bindings
        current = getattr(navigation, "camera_bindings", None)
        self.camera_bindings = current
        if previous is None or previous == current:
            return False
        self.set_travel_keys(ctx, frozenset())
        self.camera.cancel(ctx, now, "binding_changed")
        self.camera_receipt = None
        self.cancel_scene_requests()
        ctx.log_event("quest_bindings_changed", {"previous": previous.generation,
            "current": current.generation if current else None, "id": navigation.quest_id})
        return True

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
        forever_bars = ctx.cfg.game_version == "1.60.1"
        road_image = None
        self.road_status = None
        self.combat_frame = None
        frame_image = nav_image = text_image = None
        self.nav3d_fast_pose = None
        if isinstance(ctx.capture, ScreenCapture):
            quest_rect = quest_region(channel.x, channel.y)
            combat_rect = combat_region(channel.x, channel.y)
            nav_rect = travel_region(channel.x, channel.y, supply_bars=forever_bars)
            if self.auto_route and self.require_prepared:
                monitors = ctx.capture.monitor_regions()
                for run_status, corpse_receipts in ((True, True), (False, True), (False, False)):
                    extended = travel_region(channel.x, channel.y, camera_bindings=True,
                                             corpse_receipts=corpse_receipts, run_status=run_status)
                    if any(x <= extended[0] and extended[0] + extended[2] <= x + w
                           and y <= extended[1] < channel.y <= y + h for x, y, w, h in monitors):
                        nav_rect = extended
                        break
            left, top = channel.x, min(nav_rect[1], combat_rect[1]) if self.auto_route else combat_rect[1]
            right = nav_rect[0] + nav_rect[2] if self.auto_route else left + REGION_W
            read_nav3d = self.auto_route and (self.nav3d_enabled(ctx) or forever_bars)
            pose_rect = None
            if read_nav3d:
                from ..perception.nav3d_channel import fast_pose_region, nav3d_region
                xyz_rect = nav3d_region(channel.x, channel.y)
                top, right = min(top, xyz_rect[1]), max(right, xyz_rect[0] + xyz_rect[2])
                candidate = fast_pose_region(channel.x, channel.y)
                if candidate[1] >= top or any(x <= channel.x < x + w and y <= candidate[1] <= channel.y < y + h
                                               for x,y,w,h in ctx.capture.monitor_regions()):
                    pose_rect = candidate
                    top = min(top, pose_rect[1])
            if self.auto_route and self.require_prepared and forever_bars and ctx.input.name == "helper":
                road_rect = road_region(channel.x, channel.y)
                right = max(right, road_rect[0] + road_rect[2])
            read_text = (self.auto_route and self.require_prepared and not self.travel_only and self.planner is not None
                         and self.sample and self.sample.state == QuestState.ACTIVE)
            if read_text:
                text_rect = quest_text_region(channel.x, channel.y)
                top, right = min(top, text_rect[1]), max(right, text_rect[0] + text_rect[2])
            snapshot = ctx.capture.grab(left, top, right - left, channel.y - top)

            def strip(rect):
                x, y, width, height = rect
                return snapshot[y - top:y - top + height, x - left:x - left + width]

            self.combat_frame, frame_image = strip(combat_rect), strip(quest_rect)
            if self.auto_route:
                nav_image = strip(nav_rect)
                if read_text:
                    text_image = strip(text_rect)
                if self.require_prepared and forever_bars and ctx.input.name == "helper":
                    road_image = strip(road_rect)
                if read_nav3d:
                    from ..perception.nav3d_channel import decode_fast_pose, decode_nav3d
                    raised = nav3d_region(channel.x, channel.y, raised=True)
                    self.nav3d_sample = decode_nav3d(strip(xyz_rect))
                    if self.nav3d_sample is None and forever_bars:
                        self.nav3d_sample = decode_nav3d(strip(raised))
                    if self.nav3d_sample is not None and pose_rect is not None:
                        self.nav3d_fast_pose = decode_fast_pose(strip(pose_rect))
        status = ctx.channel.status(max_age_s=1.5)
        navigation = None
        if self.auto_route:
            if nav_image is None:
                nav_image = ctx.capture.grab(*travel_region(channel.x, channel.y, supply_bars=forever_bars))
            navigation = (decode_travel(nav_image, expect_camera_bindings=True)
                          if self.expect_camera_bindings else decode_travel(nav_image))
            if self.require_prepared and navigation is not None:
                if (navigation.addon_build or 0) >= CAMERA_BINDING_BUILD:
                    self.expect_camera_bindings = True
                if self.binding_transition(ctx, now, navigation):
                    return
            self.camera.guard(ctx, now, navigation)
            if text_image is not None:
                self.quest_text.observe(decode_quest_text(text_image), navigation)
        if frame_image is None:
            frame_image = ctx.capture.grab(*quest_region(channel.x, channel.y))
        frame = decode_quest(frame_image)
        observed_combat = decode_combat(self.combat_frame) if self.combat_frame is not None else None
        self.observation = self.observations.snapshot(now, frame, navigation, observed_combat)
        if road_image is not None:
            road = decode_road_status(road_image)
            if road and navigation and (navigation.addon_build or 0) >= 307:
                if road.sequence == navigation.sequence and road.identity.map_id == navigation.map_id:
                    self.road_status = road
            elif road and self.road_scan.request_at is not None and road.identity == self.road_scan.identity:
                self.road_status = road
            safe = bool(self.road_status and navigation and frame and frame.quest_id == navigation.quest_id
                        and frame.dialogue == Dialogue.CLOSED
                        and navigation.task & TaskFlags.PREPARED and navigation.position and navigation.world_size
                        and navigation.flags & TravelFlags.OUTDOORS and not navigation.flags & (
                            TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
                            | TravelFlags.IN_COMBAT | TravelFlags.CASTING)
                        and (not navigation.recovery or navigation.recovery.life == LifeState.ALIVE)
                        and not self.terrain and not self.npc_detouring and not self.defense
                        and not (navigation.camera and navigation.camera.result == CameraResult.PENDING))
            if self.road_scan.tick(ctx, now, self.road_status, allow=safe and not self.required_loot,
                                   position=navigation.position if navigation else None,
                                   world_size=navigation.world_size if navigation else None,
                                   release=lambda: self.set_travel_keys(ctx, frozenset())):
                if self.road_scan.request_at is not None:
                    self.execution_started = True
                return
        if (ctx.cfg.game_version == "1.60.1" and navigation and frame
                and navigation.quest_id == frame.quest_id and navigation.phase == frame.state
                and (navigation.addon_build or 0) >= 291 and navigation.task & TaskFlags.PREPARED):
            receipt = navigation.forever_step, navigation.forever_value
            if receipt != self.forever_progression_receipt:
                self.forever_progression_receipt = receipt
                gear_receipt = receipt[0] in {ForeverStep.EQUIPPED, ForeverStep.EQUIP_FAILED, ForeverStep.REPAIRED,
                                             ForeverStep.REPAIR_WAITING_FOR_FUNDS, ForeverStep.REPAIR_FAILED}
                if gear_receipt or receipt[0] in {ForeverStep.SPELL_LEARNED, ForeverStep.TALENT_SPENT,
                        ForeverStep.MACRO_UPDATED, ForeverStep.TRAINER_DONE,
                        ForeverStep.TRAINER_WAITING_FOR_FUNDS, ForeverStep.ERROR}:
                    ctx.log_event("forever_progression_receipt", {"step": receipt[0].name,
                        "value": receipt[1], "id": navigation.quest_id, "sequence": navigation.sequence})
        if (ctx.cfg.game_version == "1.60.1" and observed_combat
                and client_edition(observed_combat.build) == "forever" and observed_combat.level > 0):
            level, sequence = observed_combat.level, observed_combat.sequence
            previous = self.forever_level_receipt
            if previous is None or level != previous[0]:
                ctx.log_event("forever_level_observed", {"level": level, "sequence": sequence})
            if (level >= FOREVER_TARGET_LEVEL and not self.forever_target_level_verified
                    and previous and previous[0] == level and previous[1] != sequence):
                self.forever_target_level_verified = True
                ctx.stats["forever_level"] = level
                ctx.log_event("forever_level_reached", {"level": level, "sequence": sequence})
            self.forever_level_receipt = level, sequence
        if navigation and navigation.addon_build != getattr(self, "loaded_addon_build", None):
            self.loaded_addon_build = navigation.addon_build
            ctx.log_event("quest_addon_loaded", {"build": navigation.addon_build,
                "protocol": navigation.protocol_version, "sequence": navigation.sequence,
                "extension_schema": navigation.extension_schema})
        if now - self.observation_logged_at >= 1:
            self.observation_logged_at = now
            activity = self.activity_reason(now, navigation)
            self.publish_activity(ctx, now, activity, navigation, frame)
            ctx.log_event("quest_observation", {
                "activity": activity,
                "id": navigation.quest_id if navigation else step.quest_id,
                "phase": frame.state.name if frame else None,
                "dialogue": frame.dialogue.name if frame else None,
                "dialogue_id": frame.dialogue_id if frame else None,
                "navigation": asdict(navigation) if navigation else None,
                "combat": asdict(observed_combat) if observed_combat else None,
                "health_bucket": status[1] if status and not status[0] & StatusFlags.HP_UNKNOWN else None,
                "channel_ages": {name: round(now - getattr(self.observation, name).changed_at, 3)
                                 if getattr(self.observation, name).value is not None else None
                                 for name in ("quest", "navigation", "combat")},
                "pending": {"terrain": getattr(self.terrain, "phase", None),
                            "building": getattr(self.building_access, "state", None),
                            "npc_step": self.npc_step is not None,
                            "travel": getattr(self.trip, "mode", None)},
            })
        loot_combat = observed_combat
        if self.combat_frame is None and navigation and self.strict_loot(ctx, navigation):
            loot_combat = self.read_combat(ctx)
        if self.review_timeout(ctx, now, frame, navigation, loot_combat, status):
            return
        if self.process_required_loot(ctx, now, frame, navigation, loot_combat, status):
            return
        if self.auto_route and self.wait_for_choice_handoff(ctx, now, frame, navigation):
            return
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
        if (self.require_prepared and not self.travel_only and ctx.cfg.game_version == "1.60.1"
                and navigation and navigation.quest_id == frame.quest_id == step.quest_id
                and navigation.map_id == self.map_id and navigation.phase == frame.state
                and navigation.task & TaskFlags.PREPARED):
            combat_now = bool((status and status[0] & StatusFlags.IN_COMBAT)
                              or navigation.flags & TravelFlags.IN_COMBAT)
            if self.consumables.tick(ctx, now, navigation, in_combat=combat_now,
                    can_start=(frame.dialogue == Dialogue.CLOSED and self.loot_sweep is None
                               and self.object_click_at is None and self.observation.navigation.fresh(now, .75)),
                    release=lambda: self.set_travel_keys(ctx, frozenset())):
                self.execution_started = True
                return
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
        counted_drop = bool(navigation and navigation.phase == sample.state and navigation.counted_drop)
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
            self.accepted_turn_in_pin = None
            ctx.stats["quests_accepted"] = ctx.stats.get("quests_accepted", 0) + 1
            ctx.log_event("quest_accept_confirmed", {"id": step.quest_id, "state": sample.state.name})
        in_combat = bool(status[0] & StatusFlags.IN_COMBAT
                         or navigation and navigation.flags & TravelFlags.IN_COMBAT)
        if not in_combat:
            self.cave_retreat_failed = False
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
                and not self.forever_building_mode(ctx, navigation)
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
        if (self.hunt and (sample.state == QuestState.READY or ctx.cfg.game_version == "1.60.1"
                           and sample.state == QuestState.ACTIVE and sample.progress[0] > self.hunt.baseline)
                and navigation and not self.travel_only
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
            if (not self.cave_retreat_failed and frame.dialogue == Dialogue.CLOSED
                    and self.forever_building_mode(ctx, navigation) == ("echo_ridge", "exit")
                    and self.approach_forever_building(ctx, now, navigation, heading, retreating=True)):
                self.execution_started = True
                return
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
            if not self.strict_loot(ctx, navigation):
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
            if (not self.strict_loot(ctx, navigation) and not retreated and self.loot_sweep is None
                    and self.defense_had_output and navigation
                    and navigation.sweep_ready and navigation.fetch_ready):
                self.defense_had_output = False
                self.loot_sweep = LootSweep(now)
                self.batch_for_fight = self.hunt.fight if self.hunt else None
                self.set_forward(ctx, False)
                self.loot_sweep.tick(ctx, now, navigation)
                return
            self.defense_had_output = False
        if (not self.strict_loot(ctx, navigation) and not in_combat and frame.dialogue == Dialogue.CLOSED
                and self.finish_defense_loot(ctx, now, navigation)):
            return
        if self.suspend_request and not in_combat:
            return self.suspend_task(ctx, now, navigation, in_combat)
        if (prepared and not in_combat and self.require_prepared and ctx.cfg.game_version == "1.60.1"
                and self.maintain_forever_character(ctx, now, navigation, heading, frame.dialogue)):
            return
        if self.forever_target_level_verified and prepared and not in_combat:
            self.set_travel_keys(ctx, frozenset())
            self.OUTCOME = "completed"
            return
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
            if prepared and ctx.cfg.game_version == "1.60.1" and (navigation.addon_build or 0) >= 298:
                return self.wait_for_data(ctx, now, "quest_reward_choice_required")
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
            if (sample.state == QuestState.ACTIVE and navigation.objectives_finished and not self.travel_only
                    and objective_action != ObjectiveAction.CHOICE):
                if self.hunt and self.loot_corpse(ctx, now, progress[0], self.read_combat(ctx), navigation):
                    return
                return self.wait_for_data(ctx, now, "quest_ready_state_unconfirmed")
            if (self.require_prepared and not self.travel_only and not mount_tutorial and self.planner is not None
                    and isinstance(ctx.capture, ScreenCapture) and sample.state == QuestState.ACTIVE
                    and not in_combat and frame.dialogue == Dialogue.CLOSED and objective_index
                    and not navigation.task & TaskFlags.USE_ITEM and objective_action != ObjectiveAction.CHOICE
                    and (navigation.addon_build or 0) >= 313):
                selected = self.select_objective_mode(ctx, now, navigation, progress, measurable, targets)
                if selected is None:
                    return
                objective_action, counted_drop = selected
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
        talk_objective = sample.state == QuestState.ACTIVE and (objective_action == ObjectiveAction.NPC_TALK
            or objective_action == ObjectiveAction.CHOICE and navigation.choice_step == ChoiceStep.APPROACH
            and (navigation.map_id, navigation.quest_id) in CHOICE_HANDOFFS)
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
            and not talk_objective and objective_action not in {ObjectiveAction.ARRIVE, ObjectiveAction.INTERACT}
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
            self.goldshire_npc_reposition_tried = False
            if self.phase != QuestState.ACTIVE or step.quest_id != 95998:
                self.camp_attempt_at, self.camp_attempts = None, 0
            self.follower = RouteFollower(route, ctx.cfg.navigation, loop=False) if route else None
            self.trip, self.search_waypoint, self.searches = None, None, 0
            self.area_search = None
            self.loot_for_fight = None
            self.loot_attempt = None
            self.fetch_for_fight, self.fetch_attempt = None, None
            self.corpse_wait_at, self.corpse_approach = None, None
            self.corpse_restore, self.corpse_loot_wait = None, None
            self.arrived, self.attempts, self.action_signature = False, 0, None
            self.npc_press_at, self.npc_step, self.npc_steps = None, None, 0
            self.npc_marker_approach, self.npc_marker_anchor = None, None
            self.npc_grail_hint = None
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
            self.hunt = QuestHunt(targets, resource=resource, counted_drop=counted_drop) if hunting else None
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
            self.hunt = QuestHunt(targets, resource=resource, counted_drop=counted_drop)
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
                if (prepared and self.phase == QuestState.PENDING and self.attempts
                        and self.action_signature == (QuestState.PENDING, Dialogue.DETAIL)
                        and now - self.last_action < 2.5):
                    return  # The quest log can update after the accepted detail panel closes.
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
                ctx.log_event("quest_dialogue_mismatch", {
                    "id": step.quest_id, "phase": self.phase.name,
                    "dialogue_id": frame.dialogue_id, "dialogue": frame.dialogue.name,
                })
                return self.stop(ctx, "quest_dialogue_unexpected")
            self.npc_dialogue_recovery_at = None
            signature = (self.phase, frame.dialogue)
            if signature != self.action_signature:
                self.action_signature, self.attempts = signature, 0
            return self.act(ctx, step.dialog_key, now)
        if navigation and not in_combat and self.approach_forever_building(ctx, now, navigation, heading):
            return
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
            fallback_radius = 28 if ctx.cfg.game_version == "1.60.1" else 90
            if inside_quest_search_area(navigation, fallback_radius=fallback_radius):
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
            if (prepared and ctx.cfg.game_version == "1.60.1" and navigation.quest_id == 95998
                    and (navigation.addon_build or 0) >= 312 and objective_action == ObjectiveAction.ARRIVE):
                self.set_travel_keys(ctx, frozenset())
                if self.camp_attempt_at is None or now - self.camp_attempt_at >= 75:
                    if self.camp_attempts >= 2:
                        return self.stop(ctx, "quest_camp_progress_unconfirmed")
                    self.camp_attempt_at = now
                    self.camp_attempts += 1
                    ctx.log_event("quest_camp_sit_requested", {"id": step.quest_id, "position": navigation.position,
                                  "attempt": self.camp_attempts})
                    self.execution_started = True
                    ctx.input.tap(step.dialog_key)
                return
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
        if self.goldshire_npc_trip is not None:
            command = self.goldshire_npc_trip.step(now, navigation, heading, heading_age=ctx.heading_age())
            self.apply_travel_command(ctx, command)
            if command.outcome:
                self.goldshire_npc_trip = None
                if command.outcome != "arrived":
                    return self.begin_npc_detour(ctx, now, navigation, "interaction_unconfirmed")
                self.attempts = 0
                self.npc_press_at = None
                self.npc_interaction_attempts.pop(self.npc_interaction_identity(navigation), None)
                ctx.log_event("forever_goldshire_npc_repositioned", {"id": navigation.quest_id,
                              "position": navigation.position, "sequence": navigation.sequence})
            return
        candidate = bool(navigation.task & TaskFlags.NPC_CANDIDATE) and self.attempts < 3
        if candidate or navigation.task & TaskFlags.NPC_SELECTED:
            self.npc_marker_approach = None
        if (self.npc_detouring or self.npc_obstacle_reason or self.npc_step is not None
                or navigation.task & TaskFlags.NPC_SELECTED) and not navigation.task & TaskFlags.NPC_CONFIGURED:
            hint = self.npc_location_hint(navigation)
            if hint is not None:
                navigation = replace(navigation, destination=hint[1])
        range_receipt = TaskFlags.NPC_SELECTED | TaskFlags.NPC_RANGE_KNOWN | TaskFlags.NPC_WITHIN_5_YARDS
        if self.npc_detouring and self.npc_obstacle_reason == "interaction_unconfirmed":
            return self.tick_npc_detour(ctx, now, navigation, heading)
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
        if candidate:
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
        done, required = navigation.objective_progress
        if navigation.phase == QuestState.ACTIVE and required > 0:
            previous = defense.quest_progress
            defense.quest_progress = (navigation.quest_id, navigation.objective_index, done)
            if previous and previous[:2] == defense.quest_progress[:2] and done > previous[2]:
                defense.grounded_at = None
                ctx.log_event("quest_defense_credit", {"id": navigation.quest_id, "progress": done})
        airborne = flags & (TravelFlags.FLYING | TravelFlags.FALLING)
        if airborne:
            self.set_forward(ctx, False)
            if defense.airborne_since is None:
                defense.airborne_since = now
            elif now - defense.airborne_since > 18:
                return self.stop(ctx, "quest_defense_landing_unconfirmed")
            return
        defense.airborne_since = None
        if flags & TravelFlags.MOUNTED:
            self.set_forward(ctx, False)
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
        if not self.strict_loot(ctx, navigation):
            self.remember_defense_corpse(defense.fight, combat)
        status, key = defense.step(now, combat, navigation)
        if key:
            if status == 'action' and self.strict_loot(ctx, navigation) and not loot_prepared(navigation):
                return self.stop(ctx, 'quest_loot_addon_required')
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
            if self.continuous and status.endswith('timeout'):
                return self.stop(ctx, "quest_defense_" + status)
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
            router = GridRouter(*navigation.world_size)
            point = next((p for p in reversed(self.safe_ground) if 35 <= router.distance(p, navigation.position) <= 62), None)
            if point is None:
                return self.stop(ctx, "quest_defense_" + status)
            self.start_defense_retreat(ctx, now, navigation, point, combat, status)
            return self.defense_retreat(ctx, now, navigation)
        elif status != "waiting":
            return self.stop(ctx, "quest_defense_" + status)

    def start_defense_retreat(self, ctx, now, nav, point, combat, reason):
        defense = self.defense
        defense.retreat = QuestTrip(nav.world_size, point, ctx.cfg.navigation.facing_convention, walk_only=True)
        if self.ground_router:
            defense.retreat.router = self.ground_router[1]
        defense.retreat_at, defense.retreat_origin = now, nav.position
        self.danger_zones.append((nav.position, now + 45))
        if nav.recovery and nav.recovery.follow and nav.recovery.pet == PetState.ALIVE:
            ctx.input.tap("f9")
        ctx.log_event("quest_defense_retreat", {"reason": reason, "destination": point,
                      "target": combat.target_id if combat else None})

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
                        defense.handoff_probe = None
                        try:
                            ctx.input.click_ui()
                        except HelperActionRejected as exc:
                            ctx.log_event("quest_attacker_handoff_rejected", {"id": nav.quest_id,
                                          "point": point, "reason": str(exc)})
                            return True
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
        try:
            ctx.input.move_to(*point, jitter_px=0)
        except HelperActionRejected as exc:
            defense.handoff_probe = None
            ctx.log_event("quest_attacker_handoff_rejected", {"id": nav.quest_id,
                          "point": point, "reason": str(exc)})
            return True
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

    def start_goldshire_npc_reposition(self, ctx, now, nav):
        if (ctx.cfg.game_version != "1.60.1" or nav.quest_id != 54 or nav.phase != QuestState.READY
                or nav.map_id != 1429 or not nav.flags & TravelFlags.OUTDOORS):
            return False
        pose = self.nav3d_sample
        goal = GOLDSHIRE_DUGHAN_APPROACH_XY
        if (self.goldshire_npc_reposition_tried
                or not pose or not pose.position_xy or not pose.grounded or not pose.bindings_verified
                or (pose.sequence, pose.quest_id, pose.ui_map_id, pose.world_id, pose.client_build)
                != (nav.sequence, nav.quest_id, nav.map_id, 0, 70959)):
            return False
        distance = math.dist(pose.position_xy, goal)
        if not .9 < distance <= 10:
            return False
        self.goldshire_npc_reposition_tried = True
        from ..nav3d import NavMesh, NavigationError, Query
        from ..nav3d.follower import WorldRouteFollower
        from ..nav3d.runtime import SurfaceTracker

        try:
            mesh = NavMesh.load(ctx.cfg.nav3d.forever_goldshire_mesh)
            if mesh.mesh_id != ctx.cfg.nav3d.forever_goldshire_mesh_id:
                return False
            tracker = SurfaceTracker(mesh)
            start, = tracker.candidates(pose.position_xy)
            end, = tracker.candidates(goal)
            route = mesh.plan(Query(mesh.frame, start, end, horizontal_snap=.14))
            WorldRouteFollower.validate_route(route)
            if route.length > distance * 1.25 or any(
                    math.dist(point[:2], projection(point[:2], pose.position_xy, goal)[0]) > .6
                    for point in route.points):
                return False
        except (OSError, ValueError, NavigationError):
            return False
        x, y = nav.position
        width, height = nav.world_size
        dx, dy = (b - a for a, b in zip(pose.position_xy, goal))
        self.goldshire_npc_trip = QuestTrip(nav.world_size, (x + dx / width, y - dy / height),
            ctx.cfg.navigation.facing_convention, arrival_radius=.9, walk_only=True, local_travel=True,
            log_event=ctx.log_event)
        self.npc_obstacle_reason = None
        ctx.log_event("forever_goldshire_npc_reposition", {"id": nav.quest_id, "start": pose.position_xy,
                      "goal": goal, "route_yards": round(route.length, 2)})
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
        if reason == "interaction_unconfirmed" and self.start_goldshire_npc_reposition(ctx, now, nav):
            return
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
                if ctx.cfg.game_version != "1.60.1":
                    return self.stop(ctx,'quest_segmented_setup_unavailable')
                segmented_available = False
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
        self.npc_detouring = True
        self.npc_obstacle_reason = reason if reason == "interaction_unconfirmed" else None
        self.npc_obstacle_feedback = None
        self.terrain = TerrainDetour(now, nav, heading, self.trip, getattr(self.planner, "client", None),
                                     ground_trace=self.ground_trace, player_identity=self.terrain_identity(now),
                                     contact_release=contact_confirmed, npc_bypass=contact_confirmed,
                                     allow_segmented=segmented_available)
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
        self.npc_obstacle_reason = None
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
        self.npc_interaction_attempts.pop(self.npc_interaction_identity(nav), None)
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

    @staticmethod
    def npc_interaction_identity(navigation):
        return (navigation.quest_id, navigation.map_id, int(navigation.phase), "npc",
                bool(navigation.task & TaskFlags.NPC_SELECTED), navigation.destination)

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
        identity = self.npc_interaction_identity(navigation)
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
                if not selected and navigation.task & TaskFlags.NPC_CANDIDATE:
                    # A new candidate can replace the selected soft target.
                    # Release the old step and let K verify selection again.
                    self.npc_alignment = self.npc_range_wait_since = None
                    return
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

    def npc_location_hint(self, nav):
        if (self.phase not in {QuestState.PENDING, QuestState.READY} or nav.destination_map != nav.map_id
                or nav.destination is None or nav.world_size is None):
            return None
        identity = (nav.quest_id, nav.phase, nav.map_id)
        if self.npc_grail_hint is None or self.npc_grail_hint[0] != identity:
            role = "pickup" if self.phase == QuestState.PENDING else "turnIn"
            self.npc_grail_hint = identity, cached_npc_location(nav.quest_id, nav.map_id, role)
        hint = self.npc_grail_hint[1]
        if hint is not None and GridRouter(*nav.world_size).distance(hint[1], nav.destination) <= 6:
            return hint
        return None

    def approach_npc_search_marker(self, ctx, now, nav, heading):
        if (self.phase not in {QuestState.PENDING, QuestState.READY} or nav.destination_map != nav.map_id
                or not nav.npc_range_supported or not nav.task & TaskFlags.PREPARED
                or (self.phase == QuestState.PENDING and self.npc_select_attempted and self.attempts < 3)):
            return False
        identity = (nav.quest_id, nav.phase, nav.map_id)
        router = GridRouter(*nav.world_size)
        if router.distance(nav.position, nav.destination) > NPC_MARKER_APPROACH_YARDS:
            self.npc_marker_approach = None
            return False
        if self.npc_marker_anchor != identity:
            self.npc_marker_anchor = identity
            self.npc_marker_approach = None
            hint = self.npc_location_hint(nav)
            goal, source, npc = nav.destination, "quest_marker", None
            if hint is not None:
                npc_id, point = hint
                if router.distance(nav.position, point) <= NPC_MARKER_APPROACH_YARDS:
                    goal, source, npc = point, "grail_cache", npc_id
            if router.distance(nav.position, goal) > NPC_MARKER_APPROACH_RADIUS:
                trip = QuestTrip(nav.world_size, goal, ctx.cfg.navigation.facing_convention,
                                 walk_only=True, arrival_radius=NPC_MARKER_APPROACH_RADIUS,
                                 log_event=ctx.log_event)
                self.npc_marker_approach = trip, now + 12, nav.destination
                ctx.log_event("quest_npc_marker_approach", {"id": nav.quest_id, "source": source,
                    "npc_hint": npc, "start": nav.position, "destination": goal})
        if self.npc_marker_approach is None:
            return False
        trip, deadline, marker = self.npc_marker_approach
        if router.distance(marker, nav.destination) > 1.5:
            self.npc_marker_approach = None
            self.set_forward(ctx, False)
            return False
        command = trip.step(now, nav, heading, heading_age=ctx.heading_age()) if now < deadline else None
        if command is not None and not command.outcome and not command.keys and command.tap in {None, "w", "left", "right"}:
            self.apply_travel_command(ctx, command)
            return True
        self.npc_marker_approach = None
        self.set_forward(ctx, False)
        ctx.log_event("quest_npc_marker_approach_result", {"id": nav.quest_id, "position": nav.position,
            "result": "deadline" if command is None else command.outcome or "unverified_local_movement"})
        return False

    def search_quest_npc(self, ctx, now, navigation, heading):
        if not self.npc_ground_ready(ctx, now, navigation):
            return
        if not (navigation.flags & TravelFlags.KEYS_VERIFIED and navigation.position
                and navigation.destination and navigation.world_size):
            return self.stop(ctx, "quest_interact_position_unavailable")
        if self.approach_npc_search_marker(ctx, now, navigation, heading):
            return
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
        if self.strict_loot(ctx, navigation):
            return False  # The required corpse queue owns loot in this session.
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
        forever = ctx.cfg.game_version == "1.60.1" and sample and client_edition(sample.build) == "forever"
        if not same_corpse and (forever or progress <= self.hunt.baseline):
            if restored and now - self.corpse_restore[1] < 1.5:
                return True
            if (not restored and navigation.last_target_verified and not navigation.flags & blocked
                    and sample and not sample.unsafe and (sample.key_verified or forever) and sample.target_id == 0):
                self.set_forward(ctx, False)
                self.corpse_restore = fight, now
                self.hunt.credit_deadline = now + 7
                ctx.log_event("quest_corpse_restore_target", {"target": fight.target_id})
                ctx.input.tap("p")
                return True
        if (forever and same_corpse and navigation.corpse_lootable is None
                and not navigation.flags & blocked):
            if self.corpse_loot_wait is None or self.corpse_loot_wait[0] is not fight:
                self.corpse_loot_wait = fight, now
            if now - self.corpse_loot_wait[1] < 1.5:
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
            if self.hunt.requires_loot:
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
            if status == 'action' and self.strict_loot(ctx, navigation) and not loot_prepared(navigation):
                return self.stop(ctx, 'quest_loot_addon_required')
            if status == "approach" and (not navigation or not navigation.flags & TravelFlags.KEYS_VERIFIED):
                return self.stop(ctx, "quest_navigation_bindings_unverified")
            self.set_forward(ctx, False)
            event = "quest_combat_" + (status if status in {"turn", "search_turn", "approach"} else "action")
            ctx.log_event(event, {"id": self.steps[self.index].quest_id, "target": sample.target_id, "key": key})
            tap_combat_key(ctx.input, status, key)
        elif status == "credited":
            ctx.log_event("quest_combat_credit", {"id": self.steps[self.index].quest_id, "progress": progress})
        elif status == "target_tagged_by_other_player":
            ctx.log_event("quest_combat_target_tagged", {"id": self.steps[self.index].quest_id,
                "target": sample.target_id, "progress": progress})
        elif status == "resource_try_next":
            ctx.log_event("quest_resource_try_next", {"id": self.steps[self.index].quest_id,
                "percent": progress / 10, "uncredited_kills": self.hunt.uncredited_kills})
        elif status == "drop_try_next":
            ctx.log_event("quest_drop_try_next", {"id": self.steps[self.index].quest_id,
                "count": progress, "uncredited_kills": self.hunt.uncredited_kills})
        elif status == "uncredited_target":
            ctx.log_event("quest_combat_uncredited_target", {"id": self.steps[self.index].quest_id,
                "target": sample.target_id, "progress": progress, "consecutive": self.hunt.uncredited_kills})
        elif status == "target_not_found":
            if self.search_quest_area(ctx, navigation):
                self.hunt.reset_search()
            else:
                self.stop(ctx, "quest_combat_target_not_found")
        elif (status in {"no_cast_or_damage_receipt", "approach_timeout", "approach_blocked"}
                and not in_combat and not sample.target_engaged
                and ctx.cfg.game_version == "1.60.1" and navigation
                and navigation.flags & TravelFlags.OUTDOORS and self.search_quest_area(ctx, navigation)):
            ctx.log_event("quest_combat_reselect_no_receipt", {"id": navigation.quest_id,
                "target": sample.target_id, "target_epoch": sample.state_epoch, "progress": progress, "reason": status,
                "waypoint": self.search_waypoint})
            self.hunt.fight = None
            self.hunt.rejected_selection = (sample.target_id, sample.state_epoch)
            self.hunt.reset_search()
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
        self.startup_wait = None
        self.prepare_heartbeat = None
        self.prepare_requested = False
        self.online_wait = None
        self.online_block = None
        self.online_checked = None
        self.online_poll_at = 0.0
        self.online_sample = None
        self.resume_loot = None
        self.prepare_retry_at = 0.

    def on_enter(self, ctx):
        self.started = time.monotonic()

    def wait_for_startup(self, ctx, reason, message):
        self.sequence = None
        if self.startup_wait != reason:
            self.startup_wait = reason
            ctx.log_event("quest_start_waiting", {"reason": reason})
            log.info("任务主控已启动。%s", message)

    def request_preparation(self, ctx):
        request = getattr(ctx.input, "request_quest_preparation", None)
        if request is None or self.prepare_requested:
            return
        now = time.monotonic()
        if now < self.prepare_retry_at:
            return
        status = ctx.channel.status(max_age_s=.7)
        if status is None or not status[0] & StatusFlags.QUEST_PREPARE_KEY or status[0] & StatusFlags.IN_COMBAT:
            self.prepare_heartbeat = None
            return
        if ctx.cfg.game_version == "1.60.1" and not status[0] & StatusFlags.QUEST_PREPARE_FOREVER:
            self.prepare_heartbeat = None
            return
        heartbeat = bool(status[0] & StatusFlags.HEARTBEAT)
        if self.prepare_heartbeat is not None and self.prepare_heartbeat != heartbeat:
            self.prepare_requested = request()
            self.prepare_retry_at = now + 2
            self.prepare_heartbeat = None
            ctx.log_event("quest_start_prepare_requested", {"accepted": self.prepare_requested})
        else:
            self.prepare_heartbeat = heartbeat

    def pause_online(self, ctx, now, reason):
        if self.online_wait is not None:
            return
        ctx.input.release_all()
        if self.planner is not None:
            self.planner.discard_pending()
        if self.runner is not None:
            self.runner.pause_online()
        self.online_wait = now, reason
        self.online_checked = self.online_sample = self.sequence = self.prepare_heartbeat = None
        self.online_poll_at = now
        if reason in QUEST_CONNECTION_LOSSES:
            self.prepare_requested = False
        ctx.log_event("quest_controller_waiting", {"reason": reason})

    @staticmethod
    def resume_identity(nav, frame):
        return (nav.quest_id, nav.map_id, nav.phase, nav.round_stage, nav.objective_index,
                nav.objective_action, nav.objective_progress, nav.forever_step, nav.suspend_epoch,
                nav.addon_build, bool(nav.flags & TravelFlags.IN_COMBAT),
                nav.recovery.life if nav.recovery else None, nav.camera_bindings,
                frame.dialogue, frame.dialogue_id)

    def check_online_context(self, ctx, now):
        from ..perception.calibration import Location, startup_location

        _, reason = self.online_wait
        epoch = ctx.input.focus_epoch
        checked = reason, epoch
        channel = ctx.cfg.channel
        if self.online_checked != checked:
            if now < self.online_poll_at:
                return False
            self.online_poll_at = now + 2
            if isinstance(ctx.capture, ScreenCapture):
                try:
                    ctx.capture.bind_game_window()
                    location = startup_location(ctx.capture,
                        Location(ctx.cfg.capture.monitor, channel.x, channel.y), scan_duration=1.25)
                except (CaptureError, OSError, ValueError):
                    self.online_sample = None
                    return False
                ctx.cfg.capture.monitor = location.monitor
                channel.x, channel.y = location.x, location.y
            ctx.invalidate_game_observation()
            self.online_checked = checked
            self.online_sample = None
        if epoch is not None:
            ctx.input.resume_foreground(epoch)
            self.online_checked = reason, None
            return False  # The next tick reads the newly bound window again.
        if self.runner is None:
            return True
        nav = decode_travel(ctx.capture.grab(*travel_region(channel.x, channel.y)))
        frame = decode_quest(ctx.capture.grab(*quest_region(channel.x, channel.y)))
        if (not nav or not frame or not frame.tracking or not nav.quest_id
                or (nav.quest_id, nav.map_id, nav.phase) != (frame.quest_id, frame.map_id, frame.state)
                or not nav.task & TaskFlags.PREPARED or not nav.flags & TravelFlags.KEYS_VERIFIED):
            self.online_sample = None
            if self.online_block is None and (not nav or not nav.flags & TravelFlags.IN_COMBAT):
                self.request_preparation(ctx)
            return False
        if ctx.cfg.game_version == "1.60.1":
            sample = decode_combat(ctx.capture.grab(*combat_region(channel.x, channel.y)))
            if sample is None or client_edition(sample.build) != "forever":
                self.online_sample = None
                return False
        identity = self.resume_identity(nav, frame)
        before = self.online_sample
        if before is None or before[0] != identity or now - before[3] > 1:
            self.online_sample = identity, nav.sequence, frame.sequence, now
            return False
        if before[1] == nav.sequence or before[2] == frame.sequence:
            return False
        if self.online_block is not None:
            blocked_identity, blocked_nav = self.online_block
            if blocked_identity is None:
                self.online_block = identity, nav
                return False
            moved = (nav.position and blocked_nav.position and nav.map_id == blocked_nav.map_id
                     and nav.world_size and GridRouter(*nav.world_size).distance(nav.position, blocked_nav.position) >= 3)
            if nav.round_stage == RoundStage.DONE or (identity == blocked_identity and not moved):
                return False
        runner = self.runner
        task_changed = nav.quest_id != runner.steps[runner.index].quest_id or nav.map_id != runner.map_id
        round_changed = (runner.round_stage != RoundStage.NONE and runner.round_stage != nav.round_stage)
        if task_changed or round_changed:
            if nav.map_id == runner.map_id and (runner.round_advance_at is not None
                    or runner.followup_id == nav.quest_id):
                return True  # Let the existing task transaction consume the new game receipt.
            if nav.map_id == runner.map_id:
                self.resume_loot = runner.map_id, runner.required_loot
            runner.road_scan.close()
            self.runner = None
        return True

    def tick(self, ctx):
        if not (self.require_prepared and not self.travel_only
                and getattr(ctx.input, "supports_focus_pause", False)):
            return self.tick_task(ctx)
        now = time.monotonic()
        try:
            focused = game_is_frontmost()
            if ctx.input.focus_epoch is not None or not focused:
                self.pause_online(ctx, now, "foreground")
                if not focused:
                    self.online_checked = self.online_sample = None
                    return
            if self.online_wait is not None:
                if not self.check_online_context(ctx, now):
                    return
                started, reason = self.online_wait
                self.online_wait = self.online_block = self.online_sample = None
                if self.runner is not None:
                    self.runner.resume_online(now, now - started)
                ctx.log_event("quest_controller_resumed", {"reason": reason})
                return
            self.tick_task(ctx)
        except HelperForegroundPaused:
            self.pause_online(ctx, now, "foreground")
            self.online_checked = self.online_sample = None
            return
        except CaptureError:
            self.pause_online(ctx, now, "capture_unavailable")
            self.online_checked = self.online_sample = None
            return
        except HelperActionRejected as exc:
            ctx.log_event("quest_controller_action_rejected", {"reason": str(exc)})
            self.OUTCOME = "stopped:action_rejected"
        if self.OUTCOME is None:
            return
        reason = self.OUTCOME.removeprefix("stopped:")
        if reason not in QUEST_CONNECTION_LOSSES and reason != "game_not_frontmost":
            observation = self.runner and self.runner.observation
            nav = observation and observation.navigation.value
            frame = observation and observation.quest.value
            self.online_block = (self.resume_identity(nav, frame), nav) if nav and frame else (None, None)
        self.OUTCOME = None
        self.pause_online(ctx, now, reason)

    def tick_task(self, ctx):
        if self.runner is not None:
            self.runner.tick(ctx)
            self.OUTCOME = self.runner.OUTCOME
            return
        if self.require_prepared and ctx.input.name != "mock" and not game_is_frontmost():
            self.prepare_heartbeat = None
            return self.wait_for_startup(ctx, "foreground", "等待魔兽回到前台。")
        channel = ctx.cfg.channel
        try:
            navigation = decode_travel(ctx.capture.grab(*travel_region(channel.x, channel.y)))
        except CaptureError:
            if not self.require_prepared or getattr(ctx.input, "supports_focus_pause", False):
                raise
            self.prepare_heartbeat = None
            return self.wait_for_startup(ctx, "capture", "等待可读的游戏画面。")
        has_task = navigation and navigation.quest_id > 0 and navigation.map_id > 0
        if self.require_prepared:
            if getattr(ctx.input, "supports_focus_pause", False) and has_task:
                if navigation.round_stage == RoundStage.DONE:
                    return self.wait_for_startup(ctx, "round_done", "本轮已结束，等待新的任务。")
                frame = decode_quest(ctx.capture.grab(*quest_region(channel.x, channel.y)))
                if (not frame or not frame.tracking or (frame.quest_id, frame.map_id, frame.state)
                        != (navigation.quest_id, navigation.map_id, navigation.phase)):
                    return self.wait_for_startup(ctx, "task_receipt", "等待一致的任务和导航色条。")
            if not (has_task and navigation.task & TaskFlags.PREPARED and navigation.flags & TravelFlags.KEYS_VERIFIED):
                if not navigation or not navigation.flags & TravelFlags.IN_COMBAT:
                    self.request_preparation(ctx)
                return self.wait_for_startup(ctx, "preparation", "等待插件发布任务和核对后的键位。")
            if ctx.cfg.game_version == "1.60.1":
                try:
                    sample = decode_combat(ctx.capture.grab(*combat_region(channel.x, channel.y)))
                except CaptureError:
                    sample = None
                if sample is None or client_edition(sample.build) != "forever":
                    return self.wait_for_startup(ctx, "forever_client", "等待无限服角色色条。")
            if not self.travel_only and not navigation.npc_range_supported:
                return self.wait_for_startup(ctx, "npc_range", "等待支持 NPC 距离回执的插件。")
            previous = self.sequence
            self.sequence = navigation.quest_id, navigation.sequence
            if previous is None or previous[0] != navigation.quest_id or previous == self.sequence:
                return
        if has_task:
            targets = QUEST_TARGETS.get(navigation.quest_id, frozenset())
            step = QuestStep(navigation.quest_id, "9", None, None, None, None, targets)
            self.runner = RunQuests(navigation.map_id, [step], self.kinds, self.planner,
                                    auto_route=True, travel_only=self.travel_only,
                                    require_prepared=self.require_prepared)
            self.runner.continuous = self.require_prepared and not self.travel_only
            if self.resume_loot is not None:
                map_id, corpses = self.resume_loot
                if map_id == navigation.map_id:
                    self.runner.required_loot = corpses
                self.resume_loot = None
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

    def pump_channel(self):
        state = self.machine.current
        if isinstance(state, NavigateCurrentQuest) and getattr(self.ctx.input, "supports_focus_pause", False):
            if self.ctx.input.focus_epoch is not None or (state.online_wait and state.online_checked is None):
                return
        super().pump_channel()

    def _finish(self, status, t0):
        state = self.machine.current
        if isinstance(state, NavigateCurrentQuest) and state.runner is not None:
            if (state.runner.status_wire_at is not None and not state.runner.status_wire_failed
                    and time.monotonic() - state.runner.status_wire_at < 2 and game_is_frontmost()):
                self.ctx.input.publish_run_status(0)
            state.runner.cancel_scene_requests()
            state.runner.road_scan.close()
        return super()._finish(status, t0)
