"""Execute a validated same-map quest plan with live dialogue confirmation."""
from __future__ import annotations

import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..control.navigation import Route, RouteFollower
from ..core.statemachine import State
from ..perception.channel_spec import StatusFlags
from ..perception.combat_channel import combat_region, decode_combat
from ..perception.quest_channel import Dialogue, Objective, QuestKind, QuestState, decode_quest, quest_region
from .base import Module
from .combat import QuestHunt

STORY_KINDS = frozenset({QuestKind.CAMPAIGN, QuestKind.MAINLINE, QuestKind.SIDE})
UNSAFE_FLAGS = StatusFlags.DEAD | StatusFlags.GHOST | StatusFlags.FLYING
PICKUP_DIALOGUE = {Dialogue.OFFER, Dialogue.DETAIL}
TURN_IN_DIALOGUE = {Dialogue.TURN_IN, Dialogue.PROGRESS, Dialogue.REWARD}


@dataclass(frozen=True)
class QuestStep:
    quest_id: int
    dialog_key: str
    pickup: Route
    turn_in: Route
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
                 planner=None, *, accept_only=False):
        self.map_id, self.steps, self.kinds = map_id, steps, kinds
        self.planner, self.accept_only = planner, accept_only
        self.planned_phase = None
        self.index = 0
        self.forward = False
        self.had_focus = False
        self.last_sequence = None
        self.last_frame = self.last_target = time.monotonic()
        self.sample = None
        self.phase = None
        self.follower = None
        self.arrived = False
        self.last_action = self.last_progress = 0.0
        self.unusable_since = None
        self.phase_started = 0.0
        self.progress = (0, 0)
        self.action_signature = None
        self.attempts = 0
        self.hunt: QuestHunt | None = None
        self.quest_keys = {key for step in steps for key in (step.dialog_key, step.objective_key) if key}
        self.OUTCOME = None

    def on_enter(self, ctx):
        self.last_frame = self.last_target = time.monotonic()

    def stop(self, ctx, reason):
        ctx.input.release_all()
        self.OUTCOME = f"stopped:{reason}"

    def set_forward(self, ctx, enabled):
        if self.forward != enabled:
            (ctx.input.down if enabled else ctx.input.up)("w")
            self.forward = enabled

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
        cfg = ctx.cfg.input
        if decision.turn_err is not None and abs(decision.turn_err) > math.radians(cfg.turn_deadband_deg):
            turn = decision.turn_err * cfg.mouse_turn_gain
            pixels = int(max(-cfg.mouse_turn_max_px, min(turn, cfg.mouse_turn_max_px)))
            ctx.input.rmb_turn(-pixels if cfg.invert_turn else pixels)
        self.set_forward(ctx, decision.move_forward)

    def tick(self, ctx):
        now = time.monotonic()
        if ctx.input.name != "mock":
            if not game_is_frontmost():
                if self.had_focus or now - self.last_frame > 12:
                    return self.stop(ctx, "game_not_frontmost")
                self.last_target = now
                return
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
        if frame.sequence != self.last_sequence:
            self.last_sequence, self.last_frame = frame.sequence, now
            if frame.quest_id == step.quest_id:
                self.sample, self.last_target = frame, now
        if now - self.last_frame > 2 or now - self.last_target > 3:
            return self.stop(ctx, "quest_channel_stale")
        if not frame.tracking:
            return self.stop(ctx, "quest_plan_not_tracked")
        if frame.map_id != self.map_id:
            return self.stop(ctx, "quest_wrong_map")
        status, pos, heading = ctx.channel.status(max_age_s=1.5), ctx.pos(), ctx.heading()
        if status and status[0] & UNSAFE_FLAGS:
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
        if sample.state == QuestState.TURNED_IN:
            self.set_forward(ctx, False)
            ctx.stats["quests_done"] = ctx.stats.get("quests_done", 0) + 1
            ctx.log_event("quest_done", {"id": step.quest_id, "kind": sample.kind.name})
            self.index += 1
            self.sample, self.phase, self.last_target, self.hunt = None, None, now, None
            self.unusable_since = None
            if self.index == len(self.steps):
                self.OUTCOME = "completed"
            return
        if sample.kind == QuestKind.UNKNOWN:
            return self.wait_for_data(ctx, now, "quest_classification_unknown")
        if sample.kind not in self.kinds:
            return self.stop(ctx, f"quest_kind_{sample.kind.name.lower()}")
        route = {QuestState.PENDING: step.pickup, QuestState.ACTIVE: step.objective,
                 QuestState.READY: step.turn_in}.get(sample.state)
        hunting = sample.state == QuestState.ACTIVE and bool(step.combat_targets)
        in_combat = bool(status[0] & StatusFlags.IN_COMBAT)
        if in_combat:
            if self.hunt is not None and sample.state == QuestState.READY:
                return self.wait_for_data(ctx, now, "quest_combat_did_not_end")
            if not (hunting and self.phase == QuestState.ACTIVE and self.arrived and frame.dialogue == Dialogue.CLOSED):
                return self.stop(ctx, "quest_unsafe_character_state")
        if route is None or (sample.state == QuestState.ACTIVE and sample.objective == Objective.UNKNOWN):
            return self.wait_for_data(ctx, now, "quest_objective_unsupported")
        if hunting and sample.objective != Objective.COUNTED:
            return self.wait_for_data(ctx, now, "quest_combat_needs_counted_objective")
        if hunting and sample.progress[0] >= sample.progress[1] > 0:
            return self.wait_for_data(ctx, now, "quest_ready_state_unconfirmed")
        self.unusable_since = None
        if sample.state != self.phase:
            self.phase = sample.state
            self.follower = RouteFollower(route, ctx.cfg.navigation, loop=False)
            self.arrived, self.attempts, self.action_signature = False, 0, None
            self.phase_started = self.last_progress = now
            self.planned_phase = None
            self.progress = sample.progress
            self.hunt = QuestHunt(step.combat_targets) if hunting else None
            ctx.log_event("quest_phase", {"id": step.quest_id, "kind": sample.kind.name, "state": self.phase.name})
        if now - self.phase_started > 150:
            return self.stop(ctx, "quest_phase_timeout")

        if self.planner is not None and self.planned_phase != self.phase:
            self.set_forward(ctx, False)
            labels = {
                QuestState.PENDING: ("pick_up", "按计划路线接取当前任务"),
                QuestState.ACTIVE: ("do_objective", "按计划路线完成当前任务目标"),
                QuestState.READY: ("turn_in", "按计划路线交付当前任务"),
            }
            action, label = labels[self.phase]
            signature = (self.map_id, step.quest_id, self.phase)
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
            try:
                result = self.planner.poll(signature, state, {action: label, "wait": "等待并停止本次执行"})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                ctx.log_event("quest_decision_error", {"error": str(exc)})
                return self.stop(ctx, "quest_startlux_unavailable")
            if result is None:
                if now - self.phase_started > 5:
                    return self.stop(ctx, "quest_startlux_timeout")
                return
            ctx.log_event("quest_decision", {"id": step.quest_id, "state": self.phase.name, **result})
            if result["recommendation"] != action:
                return self.stop(ctx, "quest_startlux_" + (result.get("reason") or "wait"))
            self.planned_phase = self.phase

        if frame.dialogue != Dialogue.CLOSED:
            self.set_forward(ctx, False)
            if frame.quest_id != step.quest_id:
                return
            allowed = PICKUP_DIALOGUE if self.phase == QuestState.PENDING else TURN_IN_DIALOGUE
            if self.phase == QuestState.ACTIVE or frame.dialogue_id != step.quest_id or frame.dialogue not in allowed:
                return self.stop(ctx, "quest_dialogue_unexpected")
            signature = (self.phase, frame.dialogue)
            if signature != self.action_signature:
                self.action_signature, self.attempts = signature, 0
            return self.act(ctx, step.dialog_key, now)
        if not self.arrived:
            return self.walk(ctx, now, pos, heading)
        if frame.quest_id != step.quest_id:
            return
        if self.phase == QuestState.ACTIVE:
            if self.hunt is not None:
                return self.hunt_tick(ctx, now, sample.progress[0], in_combat)
            if sample.progress[0] > self.progress[0]:
                self.progress, self.last_progress, self.attempts = sample.progress, now, 0
            if step.objective_key is None:
                if now - self.last_progress > 10:
                    return self.stop(ctx, "quest_objective_no_progress")
                return
            return self.act(ctx, step.objective_key, now)
        if self.action_signature != (self.phase, Dialogue.CLOSED):
            self.action_signature, self.attempts = (self.phase, Dialogue.CLOSED), 0
        return self.act(ctx, "f", now)

    def hunt_tick(self, ctx, now, progress, in_combat):
        channel = ctx.cfg.channel
        sample = decode_combat(ctx.capture.grab(*combat_region(channel.x, channel.y)))
        if sample and sample.key_verified and str(sample.key) in self.quest_keys:
            return self.stop(ctx, "quest_combat_action_key_conflicts")
        status, key = self.hunt.step(now, sample, progress, in_combat)
        if key:
            ctx.log_event("quest_combat_action", {"id": self.steps[self.index].quest_id,
                                                 "target": sample.target_id, "key": key})
            ctx.input.tap(key)
        elif status == "credited":
            ctx.log_event("quest_combat_credit", {"id": self.steps[self.index].quest_id, "progress": progress})
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


def wait_for_game() -> None:
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

    if not game_is_frontmost():
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.blizzard.worldofwarcraft")
        if apps:
            apps[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    print("正在等待游戏前台，最长 12 秒。", flush=True)
    deadline = time.monotonic() + 12
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
