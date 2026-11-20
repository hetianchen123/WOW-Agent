"""Recheck a local detour without interrupting the quest control loop."""

import math
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4
from threading import Event

import cv2

from ..core.paths import DATA_DIR
from ..decision.terrain import decide_multiview
from ..perception.capture import CaptureError, frontmost_game_bounds
from ..perception.travel_channel import TravelFlags, CameraScene, CameraResult, LifeState, camera_binding_key
from ..terrain_cli import inspect_images
from .navigation import compass_from_delta, heading_error
from .quest_travel import TravelCommand
from .ground_trace import GroundBacktrack, ContactRelease
from .local_bypass import LocalBypass
from .model_detour import ModelDetour
from ..perception.player_identity import PlayerIdentityGate
from ..perception.scene_scale import SCENE_BOUNDS, anchor_profile
from ..perception.screenshot_retention import mark_captures_complete

DETOUR_PROBE_YARDS = 7
RETREAT_YARDS = 4
RETREAT_MAX_PULSES = 4
PROBE_BUDGET_S = 60
COMPOSITION_PHASES = {"view_check", "view_recheck", "clearance_check"}
SCENE_ERRORS = (CaptureError, OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError)
BUSY = (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING | TravelFlags.SWIMMING
        | TravelFlags.CASTING | TravelFlags.IN_COMBAT)


def local_vision_available():
    return (DATA_DIR / "qwen3-vl/model/config.json").is_file() and (DATA_DIR / "startlux/.venv/bin/python").is_file()


def capture_scene(capture, bounds=None):
    x, y, width, height = bounds if bounds is not None else frontmost_game_bounds()
    left, top, right, bottom = SCENE_BOUNDS
    return capture.grab(x + round(width * left), y + round(height * top),
                        round(width * right) - round(width * left), round(height * bottom) - round(height * top))


def capture_building_overview(capture, bounds=None):
    """One wider recovery observation; the worker separately masks known HUD."""
    x,y,width,height=bounds if bounds is not None else frontmost_game_bounds()
    return capture.grab(x,y,width,height)


class SceneRequest:
    def __init__(self, frames, *, target=None, decide=None, metadata=None, composition=False,
                 validate=None, timeout=20):
        self.cancelled = Event()
        self.evidence_dir = DATA_DIR / "terrain" / (datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8])
        self.evidence_dir.mkdir(parents=True)

        def guard():
            if self.cancelled.is_set():
                raise ValueError("visual request cancelled")

        def infer():
            report = {"metadata": metadata or {}, "target": target, "composition": composition,
                      "cases": [], "views": {}}
            deadline = time.monotonic() + timeout

            def save():
                temporary = self.evidence_dir / "report.tmp"
                temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                temporary.replace(self.evidence_dir / "report.json")

            try:
                views = report["views"]
                for name, rgb in frames.items():
                    guard()
                    path = self.evidence_dir / (name + ".png")
                    if not cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)):
                        raise ValueError("could not encode game scene")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("scene request deadline")
                    cases = inspect_images([path], cropped=True, guard=guard, target=target,
                                       allow_unreadable=target is None, nearfield=target is None and not composition,
                                       composition=composition, timeout=remaining,
                                       player_identity=(metadata or {}).get("player_identity"))
                    case, = cases
                    report["cases"].append(case)
                    if "parse_error" in case and target is None:
                        views[name] = {"schema": 2, "feet": None, "front": "unknown", "evidence": case["parse_error"]}
                    else:
                        views[name] = case["observation"]
                    if validate and target is None and not composition:
                        views[name] = validate(name, views[name])
                    save()  # Completed frames survive cancellation of later work.
                guard()
                decision = decide(views) if decide else None
                report.update(views=views, decision=decision)
                return views, decision
            except Exception as exc:
                report["error"] = str(exc)
                raise
            finally:
                try:
                    save()
                finally:
                    mark_captures_complete(self.evidence_dir)

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quest-scene")
        self.future = pool.submit(infer)
        pool.shutdown(wait=False)

    def close(self):
        self.cancelled.set()
        self.future.cancel()


class TerrainDetour:
    """Try distinct views around an obstacle, confirming each before walking."""

    def __init__(self, now, nav, heading, trip, client, *, retreat_first=True, ground_trace=None,
                 player_identity=None, contact_release=False, npc_bypass=False, allow_segmented=True):
        self.started, self.anchor = now, nav
        self.router, self.client = trip.router, client
        self.convention = trip.convention
        self.allow_segmented = allow_segmented
        left = math.radians(55) * (1 if trip.convention == "ccw_west" else -1)
        self.left_positive = left > 0
        self.angles = {"left": heading + left, "right": heading - left, "back": heading + math.pi,
                       "wide_left": heading + 2 * left, "wide_right": heading - 2 * left}
        self.destination = trip.destination
        self.update_goal_estimates(nav.position)
        self.retreat_first = retreat_first
        self.retreat_heading = heading
        self.retreat_origin = None
        self.retreat_at = self.retreat_pulse_at = None
        self.retreat_pulses = 0
        self.retreat_pulse_limit = RETREAT_MAX_PULSES
        self.retreat_trace = []
        self.direction = "back" if retreat_first else "left"
        self.initial_phase = "retreat_observe" if retreat_first else "scan"
        self.phase = "dismount" if nav.flags & TravelFlags.MOUNTED else (
            "view_prepare" if nav.probe_camera_available else self.initial_phase)
        if (self.phase == "view_prepare" and nav.camera and not nav.camera.paused
                and nav.camera.zoom is not None and nav.camera.zoom >= 28
                and nav.camera.scene == nav.camera.applied
                and nav.camera.result == CameraResult.DISTANCE_VERIFIED):
            self.phase = "view_check"  # Keep a useful wide view instead of zooming into the crowd.
        self.view_request = None
        self.composition_heading = heading
        self.view_recovery_used = False
        self.probe_view_used = False
        self.view_availability_logged = False
        self.short_probe_steps = 0
        self.short_probe_origin = None
        self.hint_directions = set()
        self.views, self.observations = {}, {}
        self.request = None
        self.request_camera = None
        self.last_turn_at, self.last_turn = now - 1, None
        self.settled_at = now
        self.dismounted_at = None
        self.captured_at = None
        self.walk_at = None
        self.walk_origin = None
        self.walk_trace = []
        self.confirmed_scene = None
        self.failed = set()
        self.scan_queue = ["left", "right"]
        self.capture_poses = {}
        self.result_logged = False
        self.player_identity = player_identity
        self.identity_gate = PlayerIdentityGate()
        self.request_started = self.request_deadline = None
        self.observation_cost = 10.0
        self.escape_checked = not retreat_first
        self.retreat_nearfield = False
        self.retreat_released_at = None
        path = ground_trace.backtrack(nav, now, self.router) if ground_trace and retreat_first else []
        self.backtrack = GroundBacktrack(now, nav, path, self.router, self.convention) if path else None
        if self.backtrack:
            self.phase = "backtrack"
        self.contact_release = None
        self.allow_clearance = contact_release and retreat_first
        self.clearance_origin, self.clearance_heading = nav.position, heading
        self.npc_bypass, self.bypass, self.bypass_attempted = npc_bypass, None, False
        self.model_detour = None
        self.clearance_total, self.clearance_pulses = 0.0, 0
        self.clearance_trace = [nav.position]
        self.clearance_retry_used = False
        self.clearance_align_at = None
        self.composition_ok = False
        self.composition_camera = None
        if contact_release and retreat_first and not self.backtrack and not nav.flags & BUSY:
            self.contact_release = ContactRelease(now, nav, heading, self.router, self.convention)
            self.phase = "contact_release"
        self.escape_basis_logged = False
        # Retain the initial framing timestamp for diagnostics. Visual requests
        # share the 60-second deadline; backtracking has its own movement limit.
        self.escape_started = None if self.phase in {"view_prepare", "view_check", "dismount"} else now

    def update_goal_estimates(self, position):
        distance = self.router.distance(position, self.destination)
        x, y = self.router.metric(position)
        tx, ty = self.router.metric(self.destination)
        goal_heading = compass_from_delta(tx - x, ty - y, self.convention)
        errors = {name: heading_error(angle, goal_heading) for name, angle in self.angles.items()}
        self.goal_errors = {name: round(abs(math.degrees(error))) for name, error in errors.items()}
        self.remaining_after_probe = {name: round(math.hypot(distance - DETOUR_PROBE_YARDS * math.cos(error),
                                                             DETOUR_PROBE_YARDS * math.sin(error)), 1)
                                      for name, error in errors.items()}

    def begin_model_route(self, ctx, now, nav, heading, reason, *, travel_goal=None):
        local = getattr(getattr(ctx,"cfg",None),"segmented_navigation",None)
        if self.allow_segmented and local and local.enabled:
            from .segmented_detour import SegmentedDetour
            goal_mode = 'travel' if travel_goal is not None else 'npc'
            self.model_detour=SegmentedDetour(now,nav,self.router,self.convention,local,
                capture_scene,deadline=self.started+PROBE_BUDGET_S,
                goal_mode=goal_mode,travel_goal=travel_goal)
            self.anchor,self.phase=nav,"model_route"
            ctx.log_event('quest_segmented_detour_started',{'id':nav.quest_id,'reason':reason,
                'goal_mode':goal_mode})
            return True
        config = getattr(getattr(ctx, "cfg", None), "route_planner", None)
        if not config or not config.enabled:
            return False
        self.model_detour = ModelDetour(now, nav, heading, self.router, self.convention,
            config, capture_scene, deadline=self.started + PROBE_BUDGET_S)
        self.model_detour.history.append({"reason": reason,
            "direct_approach_no_displacement_confirmed": self.allow_clearance,
            "contact_position": self.clearance_origin if self.allow_clearance else None,
            "position": nav.position, "actual_retreat_trace": self.clearance_trace[-12:],
            "retreat_yards": self.clearance_total})
        self.anchor, self.phase = nav, "model_route"
        ctx.log_event("quest_model_detour_started", {"id": nav.quest_id,
            "reason": reason, "model": config.model})
        return True

    def begin_clearance(self, ctx, now, nav, heading, target, max_pulses, reason):
        self.contact_release = ContactRelease(now, nav, heading, self.router, self.convention,
            target_yards=max(.15, target - self.clearance_total), max_pulses=max_pulses,
            budget_s=min(12, self.started + PROBE_BUDGET_S - now))
        self.clearance_heading = self.retreat_heading = heading
        self.phase, self.escape_basis_logged = "contact_release", False
        ctx.log_event("quest_clearance_continue", {"id": nav.quest_id, "reason": reason,
            "already_moved_yards": self.clearance_total, "target_total_yards": target,
            "remaining_pulses": max_pulses, "position": nav.position})
        return TravelCommand()

    def widen_clearance(self, ctx, now, nav):
        # One additional relocation, before spending another full model request
        # at the same occluded point. Never back along the last scan's heading.
        if (not self.allow_clearance or self.clearance_retry_used or self.escape_checked
                or self.clearance_total < .15 or self.clearance_total >= 6 or "back" in self.failed
                or self.clearance_pulses >= 12
                or self.started + PROBE_BUDGET_S - now < self.observation_cost + 18):
            return False
        if any(v.get("route_hint") or any(v.get(side) == "open" for side in ("front", "left", "right"))
               for v in self.observations.values()):
            return False
        self.clearance_retry_used = True
        self.clearance_align_at = now
        self.phase, self.settled_at = "clearance_align", now + .3
        self.observations.clear()
        self.views.clear()
        self.capture_poses.clear()
        self.identity_gate.clear()
        ctx.log_event("quest_clearance_relocate", {"id": nav.quest_id,
            "reason": "no_usable_route_after_first_observation", "target_total_yards": 6,
            "already_moved_yards": self.clearance_total, "retreat_heading": self.clearance_heading,
            "remaining_s": round(self.started + PROBE_BUDGET_S - now, 2)})
        return True

    def finish_retreat(self, ctx, now, nav, reason):
        moved = self.router.distance(self.retreat_origin, nav.position) if self.retreat_origin else 0
        if reason in {"blocked", "escape_no_displacement", "escape_trace_blocked"}:
            self.failed.add("back")
            self.angles["back"] = self.retreat_heading + math.pi
            if reason in {"blocked", "escape_no_displacement"}:
                sign = 1 if self.convention == "ccw_west" else -1
                angle = self.retreat_heading + math.pi
                x, y = nav.position
                self.router.block_ahead(nav.position, (x - sign * math.sin(angle) / self.router.width,
                                                       y - math.cos(angle) / self.router.height))
        if moved >= 2:
            self.router.remember_passage(self.retreat_trace)
        ctx.log_event("quest_terrain_retreat_result", {"id": nav.quest_id, "reason": reason,
            "yards": round(moved, 2), "pulses": self.retreat_pulses, "position": nav.position,
            "clearance_verified": False, "displacement_verified": moved >= .15,
            "observation_improved": None})
        # A retreat is only clearance, not a completed detour. Discard all views
        # from the fence and re-observe from this new origin before seeking the NPC.
        self.anchor = nav
        self.update_goal_estimates(nav.position)
        self.views.clear()
        self.observations.clear()
        self.capture_poses.clear()
        self.identity_gate.clear()
        self.scan_queue = ["left", "right"]
        self.phase = "scan"
        self.direction = min(self.scan_queue, key=lambda name: (name in self.failed, self.goal_errors[name]))
        self.initial_phase = "scan"
        if moved >= .15:
            self.phase = "clearance_check"
            self.composition_heading = self.retreat_heading
            if ((self.allow_clearance and anchor_profile(nav.camera)) or
                    (self.clearance_retry_used and self.composition_ok and nav.camera == self.composition_camera)):
                # Same camera, new position: obtain a NEW dual-scale route image
                # directly instead of another generic framing-only inference.
                self.phase = "scan"
        self.settled_at = now + .5
        planner_config = getattr(getattr(ctx, "cfg", None), "route_planner", None)
        local_config=getattr(getattr(ctx,"cfg",None),"segmented_navigation",None)
        if self.npc_bypass and ((planner_config and planner_config.enabled)
                                or (self.allow_segmented and local_config and local_config.enabled)):
            # Real screenshot planning replaces fixed doglegs in this mode,
            # including a retreat that encountered another obstacle.
            self.begin_model_route(ctx, now, nav, self.retreat_heading, reason)
        elif self.npc_bypass and not self.bypass_attempted and moved >= .15:
            self.bypass_attempted = True
            candidate = LocalBypass(now, nav, self.clearance_origin, self.destination, self.router, self.convention)
            if candidate.candidates:
                self.bypass, self.phase = candidate, "bypass"
                ctx.log_event("quest_bypass_started", {"id": nav.quest_id, "position": nav.position,
                    "contact": self.clearance_origin, "destination": self.destination,
                    "candidate_count": len(candidate.candidates), "route_verified": False})
        return TravelCommand()

    def retreat_step(self, ctx, now, nav, heading):
        moved = self.router.distance(self.retreat_origin, nav.position)
        if nav.height is not None and self.anchor.height is not None and self.anchor.height - nav.height > 1.5:
            return TravelCommand(outcome="probe_height_drop")
        x, y = self.router.metric(self.retreat_origin)
        tx, ty = self.router.metric(nav.position)
        if moved > 6 or moved > .5 and abs(heading_error(
                compass_from_delta(tx - x, ty - y, self.convention), self.retreat_heading + math.pi
        )) > math.radians(30):
            return TravelCommand(outcome="probe_pose_changed")
        if nav.position != self.retreat_trace[-1]:
            self.retreat_trace.append(nav.position)
        if abs(heading_error(heading, self.retreat_heading)) > math.radians(13):
            return self.finish_retreat(ctx, now, nav, "heading_changed")
        if now - self.retreat_at >= 4 and not self.retreat_nearfield:
            return self.finish_retreat(ctx, now, nav, "time_budget")
        elapsed = now - self.retreat_pulse_at
        if elapsed < .25:
            return TravelCommand(frozenset({"s"}))
        # Always release between pulses; a fresh position receipt is mandatory.
        if not self.retreat_released:
            self.retreat_released = True
            self.retreat_released_at = now
            return TravelCommand()
        if now - self.retreat_released_at < .5:
            return TravelCommand()
        if nav.sequence == self.retreat_sequence:
            return (TravelCommand(outcome="probe_telemetry_stale") if elapsed >= 1
                    else TravelCommand())
        progress = self.router.distance(self.retreat_pulse_origin, nav.position)
        ctx.log_event("quest_terrain_retreat_step", {"id": nav.quest_id, "pulse": self.retreat_pulses,
            "yards": round(progress, 2), "total_yards": round(moved, 2), "sequence": nav.sequence})
        if moved >= (2 if self.retreat_nearfield else RETREAT_YARDS):
            return self.finish_retreat(ctx, now, nav, "distance_reached")
        if progress >= .15 and self.retreat_nearfield and self.retreat_pulses < 8:
            # A new image leases another single step, never a blind series of S presses.
            self.anchor = nav
            self.views.clear()
            self.observations.clear()
            self.capture_poses.clear()
            self.phase, self.direction, self.settled_at = "retreat_observe", "back", now + .5
            return TravelCommand()
        if progress < .15 or self.retreat_pulses >= self.retreat_pulse_limit:
            return self.finish_retreat(ctx, now, nav, "blocked" if progress < .15 else "pulse_budget")
        self.retreat_pulses += 1
        self.retreat_released = False
        self.retreat_released_at = None
        self.retreat_pulse_at, self.retreat_pulse_origin, self.retreat_sequence = now, nav.position, nav.sequence
        return TravelCommand(frozenset({"s"}))

    def close(self):
        if self.request:
            self.request.close()
        if self.model_detour:
            self.model_detour.close()

    def step(self, ctx, now, nav, heading):
        if not self.view_availability_logged:
            self.view_availability_logged = True
            ctx.log_event("quest_terrain_view_capability", {"id": nav.quest_id, "key": camera_binding_key(nav, probe=True),
                "available": nav.probe_camera_available, "build": nav.addon_build,
                "reason": "verified" if nav.probe_camera_available else "binding_not_verified_or_older_protocol"})
        try:
            command = self.observe(ctx, now, nav, heading)
        except SCENE_ERRORS as exc:
            ctx.log_event("quest_terrain_error", {"id": nav.quest_id, "error": str(exc)})
            command = TravelCommand(outcome="visual_unavailable")
        if command.outcome and not self.result_logged:
            self.close()
            self.result_logged = True
            ctx.log_event("quest_terrain_result", {"id": nav.quest_id, "outcome": command.outcome,
                "phase": self.phase, "direction": self.direction, "elapsed_s": round(now - self.started, 2),
                "position": nav.position, "failed": sorted(self.failed)})
        return command

    def choose_or_scan(self, ctx, now, nav, heading, decision):
        chosen = decision.get("recommendation")
        if chosen is None and decision.get("reason") in {"low_confidence", "no_option_fits"}:
            # An uncertain ranking may authorize another stationary observation,
            # never walking. The selected corridor still needs a fresh image.
            recheck = decide_multiview(None, self.observations, frozenset(self.failed),
                                      goal_errors=self.goal_errors, remaining_after_probe=self.remaining_after_probe)
            if recheck.get("recommendation") != "stop":
                decision = {**recheck, "selection_context": "uncertain_ranking_recheck_only",
                            "startlux_decision": decision}
                chosen = decision["recommendation"]
        ctx.log_event("quest_terrain_decision", {"id": nav.quest_id, "views": self.observations,
            "choice": chosen, "reason": decision.get("reason"), "decision": decision,
            "failed": sorted(self.failed)})
        if (chosen in self.observations and chosen not in self.failed
                and self.observations[chosen]["front"] == "open"):
            if not self.escape_checked:
                self.escape_checked = True
                ctx.log_event("quest_escape_observation", {"id": nav.quest_id,
                    "position": nav.position, "direction": chosen, "observation_improved": True,
                    "route_verified": False, "movement_authorized": False,
                    "observation": self.observations[chosen]})
            self.phase, self.direction = "confirm", chosen
        else:
            if self.widen_clearance(ctx, now, nav):
                return TravelCommand()
            # A fence end is a camera-relative observation goal, not a world waypoint.
            for source, view in self.observations.items():
                hint = view.get("route_hint")
                if not hint or source not in self.capture_poses:
                    continue
                side = hint.get("side")
                if side not in {"left", "right"}:
                    continue
                name = "gap_" + side
                if name in self.hint_directions:
                    continue
                self.hint_directions.add(name)
                sign = (1 if side == "left" else -1) * (1 if self.left_positive else -1)
                self.angles[name] = self.capture_poses[source]["heading"] + math.radians(25) * sign
                self.update_goal_estimates(nav.position)
                self.phase, self.direction, self.settled_at = "scan", name, now + .5
                ctx.log_event("quest_terrain_endpoint_look", {"id": nav.quest_id, "source": source,
                    "direction": name, "screen_endpoint": hint.get("point"), "movement_authorized": False})
                return TravelCommand()
            # A plausible central avatar must be seen from a second nearby angle.
            # This is an observation action only, even when its ground looked open.
            for source, view in self.observations.items():
                if (view.get("identity_candidate") is True and not view.get("identity_verified")
                        and "identity_confirm" not in self.hint_directions and source in self.capture_poses):
                    self.hint_directions.add("identity_confirm")
                    self.angles["gap_left"] = self.capture_poses[source]["heading"] + math.radians(35) * (
                        1 if self.left_positive else -1)
                    self.update_goal_estimates(nav.position)
                    self.phase, self.direction, self.settled_at = "scan", "gap_left", now + .5
                    ctx.log_event("quest_terrain_identity_look", {"id": nav.quest_id, "source": source,
                        "direction": "gap_left", "movement_authorized": False})
                    return TravelCommand()
            # An identified side opening authorizes ONLY a smaller stationary look.
            # Its pixel coordinate is never converted into a world waypoint.
            for source, view in self.observations.items():
                if view.get("schema", 0) < 2 or not (view.get("feet") or view.get("origin_verified")):
                    continue
                for side, sign in (("left", 1), ("right", -1)):
                    name = "gap_" + side
                    pose = self.capture_poses.get(source)
                    if view.get(side) == "open" and name not in self.hint_directions and pose:
                        self.hint_directions.add(name)
                        self.angles[name] = pose["heading"] + math.radians(25) * sign * (1 if self.left_positive else -1)
                        self.update_goal_estimates(nav.position)
                        self.scan_queue = [name]
                        self.phase, self.direction, self.settled_at = "scan", name, now + .5
                        ctx.log_event("quest_terrain_gap_look", {"id": nav.quest_id, "source": source,
                            "direction": name, "feet": view["feet"], "movement_authorized": False})
                        return TravelCommand()
            self.scan_queue = sorted((name for name in self.angles if name not in self.observations
                                      and name not in self.failed),
                                     key=lambda name: (name not in {"left", "right"},
                                                       self.goal_errors[name], abs(heading_error(heading, self.angles[name]))))
            if (not self.escape_checked and self.observations
                    and all(v.get("schema", 0) >= 2 and v.get("front") != "open"
                            for v in self.observations.values())):
                self.scan_queue = [name for name in self.scan_queue if name in {"left", "right"}]
                if not self.scan_queue:
                    if any("identity_verified" in v for v in self.observations.values()) and not any(
                            v.get("identity_verified") is True for v in self.observations.values()):
                        return TravelCommand(outcome="escape_player_identity_unresolved")
                    return TravelCommand(outcome="escape_evidence_insufficient")
            if not self.scan_queue:
                origin_missing = self.observations and all(
                    value.get("schema", 0) >= 2 and not (value.get("feet") or value.get("origin_verified")) for value in self.observations.values())
                return TravelCommand(outcome="player_origin_unresolved" if origin_missing else "no_passage")
            self.phase, self.direction = "scan", self.scan_queue[0]
        self.settled_at = now + .5
        return TravelCommand()

    def reject_direction(self, ctx, now, nav, heading, reason, observation=None):
        self.failed.add(self.direction)
        ctx.log_event("quest_terrain_candidate_rejected", {"id": nav.quest_id, "direction": self.direction,
            "reason": reason, "observation": observation, "position": nav.position})
        decision = decide_multiview(None, self.observations, frozenset(self.failed),
                                   goal_errors=self.goal_errors, remaining_after_probe=self.remaining_after_probe)
        decision["selection_context"] = "remaining_candidates_after_rejection"
        return self.choose_or_scan(ctx, now, nav, heading, decision)

    def request_scene(self, ctx, now, nav, frames, decide=None):
        remaining = self.started + PROBE_BUDGET_S - now
        if remaining < self.observation_cost + 2:
            ctx.log_event("quest_terrain_observation_skipped", {"reason": "insufficient_result_budget",
                "remaining_s": round(remaining, 2), "estimated_s": round(self.observation_cost, 2),
                "phase": self.phase, "direction": self.direction})
            return TravelCommand(outcome="escape_time_budget")
        self.request_started = now
        self.request_camera = nav.camera
        self.request_deadline = min(self.started + PROBE_BUDGET_S - 1,
                                    now + max(22, self.observation_cost * 1.5))
        poses = self.capture_poses.copy()
        screen_anchor = anchor_profile(nav.camera)
        identity = dict(self.player_identity or {})
        if screen_anchor:
            identity["screen_anchor"] = screen_anchor
            for pose in poses.values():
                pose["anchor_profile"] = screen_anchor["profile"]
        def validate(name, view):
            return self.identity_gate.review(view, poses[name])
        self.request = SceneRequest(frames, decide=decide, composition=self.phase in COMPOSITION_PHASES,
            validate=validate, timeout=self.request_deadline - now, metadata={
            "id": nav.quest_id, "map": nav.map_id, "phase": self.phase, "direction": self.direction,
            "pose": poses, "failed": sorted(self.failed), "player_identity": identity,
            "run_id": getattr(getattr(ctx, "evidence", None), "run_id", None)})
        ctx.log_event("quest_terrain_request", {"id": nav.quest_id, "phase": self.phase,
            "views": list(frames), "direction": self.direction,
            "evidence": str(getattr(self.request, "evidence_dir", ""))})
        return TravelCommand()

    def recover_composition(self, ctx, now, nav, heading, views):
        ctx.log_event("quest_terrain_composition", {"id": nav.quest_id, "usable": False,
            "phase": self.phase, "views": views, "recovery_used": self.view_recovery_used})
        if self.view_recovery_used or not nav.camera_restore_available or not nav.camera or nav.camera.paused:
            return TravelCommand(outcome="camera_composition_unresolved")
        self.view_recovery_used = True
        self.phase, self.view_request = "view_restore", None
        self.composition_heading = heading
        self.anchor = nav
        # No observation or screen point survives the camera change.
        self.observations.clear()
        self.views.clear()
        self.capture_poses.clear()
        self.identity_gate.clear()
        self.scan_queue = ["left", "right"]
        return TravelCommand()

    def observe(self, ctx, now, nav, heading):
        busy = BUSY if self.phase != "dismount" else BUSY & ~TravelFlags.MOUNTED
        if now - self.started >= PROBE_BUDGET_S:
            if self.phase=='model_route' and hasattr(self.model_detour,'deadline_result'):
                return self.model_detour.deadline_result(ctx,now,nav)
            return TravelCommand(outcome="probe_budget_exhausted")
        if (nav.map_id != self.anchor.map_id or nav.quest_id != self.anchor.quest_id
                or nav.phase != self.anchor.phase or nav.objective_index != self.anchor.objective_index
                or nav.objective_target != self.anchor.objective_target
                or nav.destination != self.anchor.destination or nav.destination_map != self.anchor.destination_map
                or getattr(nav, "camera_bindings", None) != getattr(self.anchor, "camera_bindings", None)
                or nav.recovery and nav.recovery.life != LifeState.ALIVE
                or nav.camera and nav.camera.paused
                or nav.position is None or nav.flags & busy
                or not nav.flags & TravelFlags.KEYS_VERIFIED):
            return TravelCommand(outcome="probe_interrupted")
        moved = self.router.distance(self.anchor.position, nav.position)
        if self.phase not in {"walk", "retreat_walk", "backtrack", "contact_release", "bypass", "model_route"} and moved > .15:
            return TravelCommand(outcome="probe_pose_changed")
        if self.phase == "dismount":
            if not nav.flags & TravelFlags.MOUNTED:
                self.phase, self.settled_at = ("view_prepare" if nav.probe_camera_available else self.initial_phase), now + .5
                if self.phase == self.initial_phase and self.escape_started is None:
                    self.escape_started = now
            elif self.dismounted_at is None:
                self.dismounted_at = now
                return TravelCommand(tap=nav.sky_dismount_key or "8")
            elif now - self.dismounted_at >= 3:
                return TravelCommand(outcome="probe_interrupted")
            return TravelCommand()
        if self.phase in {"view_prepare", "view_restore"}:
            restoring = self.phase == "view_restore"
            key = camera_binding_key(nav, probe=not restoring)
            next_phase = "view_recheck" if restoring else "view_check"
            camera = nav.camera
            available = nav.camera_restore_available if restoring else nav.probe_camera_available
            if not available or key is None or camera is None or camera.paused:
                if restoring:
                    return TravelCommand(outcome="camera_restore_unconfirmed")
                self.phase, self.settled_at = next_phase, now + .5
                ctx.log_event("quest_terrain_view_result", {"result": "binding_or_camera_unavailable"})
                return TravelCommand()
            if self.view_request is None:
                expected_scene = camera.scene if restoring else (
                    CameraScene.OVERHEAD if nav.overhead_camera_available else CameraScene.PROBE)
                self.view_request = camera.request, nav.sequence, now, expected_scene
                self.probe_view_used = True
                ctx.log_event("quest_terrain_view_requested", {"id": nav.quest_id, "key": key,
                    "restoring": restoring,
                    "zoom_before": camera.zoom, "request": (camera.request + 1) % 8})
                return TravelCommand(frozenset({key}))
            baseline, sequence, started, expected_scene = self.view_request
            if (nav.sequence != sequence and camera.request == (baseline + 1) % 8
                    and camera.applied == expected_scene
                    and camera.result not in {CameraResult.PENDING, CameraResult.IDLE}):
                if restoring and camera.result not in {CameraResult.DISTANCE_VERIFIED, CameraResult.LIMITED}:
                    return TravelCommand(outcome="camera_restore_unconfirmed")
                self.phase, self.settled_at = next_phase, now + .5
                if restoring:
                    self.probe_view_used = False
                ctx.log_event("quest_terrain_view_result", {"result": camera.result.name,
                    "zoom": camera.zoom, "restoring": restoring, "feet_verified": False})
                return TravelCommand()
            if now - started >= 8.6 or camera.request not in {baseline, (baseline + 1) % 8}:
                if restoring:
                    return TravelCommand(outcome="camera_restore_unconfirmed")
                self.phase, self.settled_at = next_phase, now + .5
                ctx.log_event("quest_terrain_view_result", {"result": "unconfirmed", "feet_verified": False})
                return TravelCommand()
            return TravelCommand(frozenset({key}))
        if ctx.heading_age() > .55:
            return TravelCommand()
        if self.phase == "model_route":
            command = self.model_detour.step(ctx, now, nav, heading)
            if getattr(self.model_detour, 'overhead', None) and self.model_detour.overhead.used:
                self.probe_view_used = True  # Parent cleanup restores F8 on completion/failure.
            return command
        if self.phase == "bypass":
            command = self.bypass.step(ctx, now, nav, heading)
            if not command.outcome or command.outcome == "detoured" or command.outcome.startswith("probe_"):
                return command
            ctx.log_event("quest_bypass_result", {"id": nav.quest_id, "outcome": command.outcome,
                "position": nav.position, "yards": self.bypass.total})
            self.anchor = nav
            self.update_goal_estimates(nav.position)
            self.views.clear()
            self.observations.clear()
            self.capture_poses.clear()
            self.identity_gate.clear()
            self.allow_clearance = False  # Do not retreat along a now unrelated old heading.
            self.phase, self.settled_at = "scan", now + .5
            return TravelCommand()
        if self.phase == "clearance_align":
            if now - self.clearance_align_at >= 7:
                return TravelCommand(outcome="escape_time_budget")
            error = heading_error(heading, self.clearance_heading)
            if abs(error) > math.radians(9):
                if now - self.last_turn_at < .9:
                    return TravelCommand()
                self.last_turn_at, self.settled_at = now, now + .55
                side = "left" if (error > 0) == self.left_positive else "right"
                return TravelCommand(tap=side, hold_s=min(.16, max(.035, abs(error) / math.radians(240))))
            if now < self.settled_at or now - ctx.heading_age() < self.last_turn_at + .2:
                return TravelCommand()
            return self.begin_clearance(ctx, now, nav, heading, 6,
                min(4, 12 - self.clearance_pulses), "observation_still_occluded")
        if self.phase == "contact_release":
            if not self.escape_basis_logged:
                self.escape_basis_logged = True
                ctx.log_event("quest_escape_basis", {"basis": "contact_release_probe", "id": nav.quest_id,
                    "map": nav.map_id, "position": nav.position, "heading": heading,
                    "path_verified": False, "max_pulses": self.contact_release.max_pulses,
                    "target_yards": self.contact_release.target_yards, "hold_s": .18})
            command = self.contact_release.step(ctx, now, nav, heading)
            if command.outcome:
                if command.outcome.startswith("probe_"):
                    return command
                self.clearance_total += self.contact_release.total
                self.clearance_pulses += self.contact_release.pulses
                self.clearance_trace.extend(self.contact_release.trace[1:])
                self.retreat_origin = self.clearance_origin
                self.retreat_pulses = self.clearance_pulses
                self.retreat_trace = self.clearance_trace
                return self.finish_retreat(ctx, now, nav, command.outcome)
            return command
        if self.phase == "backtrack":
            if not self.escape_basis_logged:
                self.escape_basis_logged = True
                ctx.log_event("quest_escape_basis", {"basis": "recent_ground_trace", "id": nav.quest_id,
                    "map": nav.map_id, "position": nav.position, "heading": heading,
                    "points": [dict(position=p.position, at=p.at, sequence=p.sequence, height=p.height)
                               for p in self.backtrack.path]})
            command = self.backtrack.step(ctx, now, nav, heading)
            if command.outcome:
                if command.outcome in {"probe_height_drop", "probe_pose_changed", "probe_telemetry_stale"}:
                    return command
                self.retreat_origin = self.backtrack.origin
                self.retreat_pulses = self.backtrack.pulses
                self.retreat_heading = heading
                self.retreat_trace = self.backtrack.trace
                if self.allow_clearance:
                    self.clearance_total, self.clearance_pulses = self.backtrack.total, self.backtrack.pulses
                    self.clearance_trace = list(self.backtrack.trace)
                    self.clearance_heading = heading
                    if (command.outcome == "escape_moved" and self.clearance_total < 4
                            and self.clearance_pulses < 8 and now - self.started < 12):
                        return self.begin_clearance(ctx, now, nav, heading, 4,
                            8 - self.clearance_pulses, "recent_trace_ended_before_clearance")
                return self.finish_retreat(ctx, now, nav, command.outcome)
            return command
        if self.phase == "retreat_walk":
            return self.retreat_step(ctx, now, nav, heading)
        target_heading = (self.composition_heading if self.phase in COMPOSITION_PHASES else
                          self.retreat_heading if self.phase == "retreat_align" else self.angles[self.direction])
        error = heading_error(heading, target_heading)
        if self.phase == "walk":
            if self.confirmed_scene.get("schema", 0) >= 2:
                return self.short_step(ctx, now, nav, heading, error)
            if nav.height is not None and self.anchor.height is not None and self.anchor.height - nav.height > 3:
                return TravelCommand(outcome="probe_height_drop")
            x, y = self.router.metric(self.walk_origin)
            tx, ty = self.router.metric(nav.position)
            moved = math.hypot(tx - x, ty - y)
            if moved > 1.5 and (moved > 12 or abs(heading_error(
                self.angles[self.direction], compass_from_delta(tx - x, ty - y, self.convention)
            )) > math.radians(38)):
                return TravelCommand(outcome="probe_pose_changed")
            if nav.position != self.walk_trace[-1]:
                self.walk_trace.append(nav.position)
            elapsed = now - self.walk_at
            if (moved >= DETOUR_PROBE_YARDS or abs(error) > math.radians(13) or elapsed >= 1.45
                    or elapsed >= .7 and moved < .65):
                ctx.log_event("quest_terrain_step_result", {"id": nav.quest_id, "direction": self.direction,
                    "yards": round(moved, 2), "elapsed_s": round(elapsed, 2),
                    "heading_error_deg": round(math.degrees(error), 1), "sequence": nav.sequence})
                if moved < 3:
                    # Changed position invalidates old views, even after a partial step.
                    if moved > .65:
                        self.observations.clear()
                        self.anchor = nav
                    return self.reject_direction(ctx, now, nav, heading, "short_step_blocked")
                self.router.remember_passage(self.walk_trace)
                ctx.log_event("quest_terrain_waypoint", {"id": nav.quest_id, "map": nav.map_id,
                    "from": self.walk_origin, "position": nav.position, "yards": round(moved, 1),
                    "feature": self.confirmed_scene.get("feature"),
                    "landmark_on_screen": self.confirmed_scene.get("landmark")})
                return TravelCommand(outcome="detoured")
            return TravelCommand(frozenset({"w"}))

        if self.request:
            if self.request_camera is not None and nav.camera != self.request_camera:
                return TravelCommand(outcome="probe_pose_changed")
            if abs(error) > math.radians(12):
                return TravelCommand(outcome="probe_pose_changed")
            if not self.request.future.done():
                if self.request_deadline is not None and now >= self.request_deadline:
                    return TravelCommand(outcome="escape_observation_timeout")
                return TravelCommand()
            views, decision = self.request.future.result()
            self.request = None
            if self.request_started is not None:
                self.observation_cost = max(4, min(18, now - self.request_started))
            ctx.log_event("quest_terrain_observation_received", {"id": nav.quest_id, "phase": self.phase,
                "views": views, "remaining_s": round(self.started + PROBE_BUDGET_S - now, 2)})
            if self.phase in COMPOSITION_PHASES:
                observation = views["composition"]
                if now - self.captured_at > 20 or observation.get("usable") is not True:
                    return self.recover_composition(ctx, now, nav, heading, views)
                ctx.log_event("quest_terrain_composition", {"id": nav.quest_id, "usable": True,
                    "observation": observation, "movement_authorized": False, "phase": self.phase})
                self.composition_ok, self.composition_camera = True, nav.camera
                if self.escape_started is None:
                    self.escape_started = now
                if (self.initial_phase == "retreat_observe" and not self.backtrack
                        and observation.get("player_visible") is False):
                    # No reverse trajectory and no visible avatar: establish identity
                    # from a nearby side before spending a half-turn on a blind rear view.
                    self.initial_phase = "scan"
                self.phase, self.settled_at = self.initial_phase, now + .5
                self.direction = ("back" if self.initial_phase == "retreat_observe" else
                                  min(("left", "right"), key=lambda name: self.goal_errors[name]))
                return TravelCommand()
            if self.phase == "retreat_observe":
                observation = views["retreat"]
                ctx.log_event("quest_terrain_retreat_observation", {"id": nav.quest_id,
                    "observation": observation, "age_s": round(now - self.captured_at, 2)})
                if now - self.captured_at > 20 or observation["front"] != "open":
                    return self.finish_retreat(ctx, now, nav, "rear_unconfirmed")
                # Near-field evidence supports one pulse, not a blind four-yard retreat.
                self.retreat_nearfield = observation.get("schema", 0) >= 2
                self.retreat_pulse_limit = 8 if self.retreat_nearfield else RETREAT_MAX_PULSES
                self.phase, self.settled_at = "retreat_align", now + .5
                return TravelCommand()
            if self.phase == "confirm":
                observation = views["confirm"]
                ctx.log_event("quest_terrain_confirmation", {"id": nav.quest_id, "direction": self.direction,
                    "observation": observation, "age_s": round(now - self.captured_at, 2)})
                if now - self.captured_at > 20 or observation["front"] != "open":
                    return self.reject_direction(ctx, now, nav, heading,
                        "confirmation_stale" if now - self.captured_at > 20 else "confirmation_not_open", observation)
                self.confirmed_scene = views["confirm"]
                if not self.escape_checked:
                    self.escape_checked = True
                    ctx.log_event("quest_escape_observation", {"id": nav.quest_id,
                        "position": nav.position, "direction": self.direction, "observation_improved": True,
                        "route_verified": False, "observation": observation})
                self.walk_trace = [nav.position]
                self.walk_sequence = nav.sequence
                if self.confirmed_scene.get("schema", 0) >= 2:
                    self.short_probe_steps += 1
                    if self.short_probe_steps > 4:
                        return TravelCommand(outcome="short_probe_budget_exhausted")
                    if self.short_probe_origin is None:
                        self.short_probe_origin = nav.position
                self.phase, self.walk_at, self.walk_origin = "walk", now, nav.position
                ctx.log_event("quest_terrain_step_started", {"id": nav.quest_id, "direction": self.direction,
                    "position": nav.position, "heading": heading})
                return TravelCommand(frozenset({"w"}))
            self.observations.update(views)
            return self.choose_or_scan(ctx, now, nav, heading, decision)

        if self.phase == "retreat_align" and now - self.captured_at > 20:
            return self.finish_retreat(ctx, now, nav, "rear_observation_stale")
        if abs(error) > math.radians(9):
            side = "left" if (error > 0) == self.left_positive else "right"
            interval = .9 if self.last_turn and side != self.last_turn else .45
            if now - self.last_turn_at < interval or now - ctx.heading_age() < self.last_turn_at + .2:
                return TravelCommand()
            hold = min(.16, max(.035, abs(error) / math.radians(240)))
            self.last_turn_at, self.last_turn = now, side
            self.settled_at = now + .55
            return TravelCommand(tap=side, hold_s=hold)
        if now < self.settled_at:
            return TravelCommand()

        if self.phase == "retreat_align":
            self.phase = "retreat_walk"
            if self.retreat_at is None:
                self.retreat_at, self.retreat_origin = now, nav.position
                self.retreat_trace = [nav.position]
            self.retreat_pulse_at, self.retreat_pulse_origin = now, nav.position
            self.retreat_sequence, self.retreat_pulses = nav.sequence, self.retreat_pulses + 1
            self.retreat_released = False
            self.retreat_released_at = None
            ctx.log_event("quest_terrain_retreat_started", {"id": nav.quest_id, "position": nav.position,
                "heading": heading, "target_yards": RETREAT_YARDS, "key": "s"})
            return TravelCommand(frozenset({"s"}))

        frame = capture_scene(ctx.capture)
        frame_name = "composition" if self.phase in COMPOSITION_PHASES else "retreat" if self.phase == "retreat_observe" else (
            "confirm" if self.phase == "confirm" else self.direction)
        self.capture_poses[frame_name] = {
            "position": nav.position, "map_id": nav.map_id, "world_size": nav.world_size,
            "heading": heading, "sequence": nav.sequence, "at": now}
        self.captured_at = now
        if self.phase in {"confirm", "retreat_observe"} | COMPOSITION_PHASES:
            return self.request_scene(ctx, now, nav, {frame_name: frame})
        else:
            previous = self.observations.copy()
            failed = frozenset(self.failed)
            return self.request_scene(ctx, now, nav, {frame_name: frame}, decide=lambda views: decide_multiview(
                self.client, previous | views, failed, goal_errors=self.goal_errors,
                remaining_after_probe=self.remaining_after_probe))

    def short_step(self, ctx, now, nav, heading, error):
        """One near-field observation leases at most .22 s of forward movement."""
        elapsed = now - self.walk_at
        moved = self.router.distance(self.walk_origin, nav.position)
        if nav.height is not None and self.anchor.height is not None and self.anchor.height - nav.height > 1.5:
            return TravelCommand(outcome="probe_height_drop")
        if moved > .4:
            x, y = self.router.metric(self.walk_origin)
            tx, ty = self.router.metric(nav.position)
            actual = compass_from_delta(tx - x, ty - y, self.convention)
            if moved > 3 or abs(heading_error(heading, actual)) > math.radians(30):
                return TravelCommand(outcome="probe_pose_changed")
        if abs(error) > math.radians(13):
            return TravelCommand(outcome="probe_pose_changed")
        if elapsed < .22 and moved < 1.5:
            return TravelCommand(frozenset({"w"}))
        if elapsed < .55:
            return TravelCommand()
        if nav.sequence == self.walk_sequence:
            return TravelCommand(outcome="probe_telemetry_stale") if elapsed >= 1.5 else TravelCommand()
        ctx.log_event("quest_terrain_short_step", {"id": nav.quest_id, "yards": round(moved, 2),
            "sequence": nav.sequence, "step": self.short_probe_steps})
        if moved < .15:
            return self.reject_direction(ctx, now, nav, heading, "short_step_blocked")
        self.router.remember_passage([self.walk_origin, nav.position])
        self.anchor = nav
        self.observations.clear()
        self.views.clear()
        self.capture_poses.clear()
        self.update_goal_estimates(nav.position)
        if self.router.distance(self.short_probe_origin, nav.position) >= 3:
            return TravelCommand(outcome="detoured")
        # Actual movement invalidates the previous image. Re-observe before every pulse.
        self.phase, self.settled_at = "confirm", now + .5
        return TravelCommand()
