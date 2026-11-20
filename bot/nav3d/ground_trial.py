"""Supervised ground trial through the prepared task controller."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
from argparse import Namespace

from ..control.input_helper import HelperInput, has_helper_socket
from ..core.config import Nav3DConfig, load_config
from ..core.paths import DATA_DIR, ROOT
from ..perception.calibration import Location
from ..perception.capture import CaptureError, ScreenCapture
from ..perception.nav3d_channel import decode_fast_pose, decode_nav3d, fast_pose_region, nav3d_region, with_fast_pose
from ..perception.quest_channel import Dialogue, decode_quest, quest_region
from ..perception.travel_channel import LifeState, TaskFlags, decode_travel, travel_region
from .follower import WorldPose, WorldRouteFollower
from .planner import NavigationError, Query
from .runtime import Nav3DCoordinator

REQUEST = DATA_DIR / "ground-trial-request.json"
DISPATCH_CHOICES = ("auto", "controller", "native")
PROFILE_CHOICES = ("short", "continuous", "pulsed")


def queue_trial(args):
    """Use the installed signed controller's prepared launch for one local trial."""
    payload = {key: getattr(args, key) for key in ("mesh_id", "start", "goal", "capture", "pose_source", "dispatch")}
    payload.update(mesh=str(Path(args.mesh).resolve()), output=str(args.output.resolve()),
                   issued=time.time(), profile=args.profile)
    if args.entry_scene is not None:
        payload["entry_scene"] = str(args.entry_scene.resolve())
    try:
        with tempfile.TemporaryDirectory(dir=REQUEST.parent) as folder:
            staged = Path(folder) / REQUEST.name
            staged.write_text(json.dumps(payload))
            os.link(staged, REQUEST)
    except OSError as exc:
        print(f"无法提交短程测试：{exc}")
        return 2
    try:
        subprocess.run([str(ROOT / "macos/quest-navigate.command"), "--prepared"], check=True)
    except OSError as exc:
        REQUEST.unlink(missing_ok=True)
        print(f"无法启动短程测试：{exc}")
        return 2
    except subprocess.CalledProcessError:
        # The helper may receive the request before macOS delays its reply.
        # Keep the one-shot trial so a late launch cannot become a regular task.
        print("未确认启动回执。短程请求仍有效，超过两分钟后会拒绝执行。")
        return 2
    return 0


def take_prepared_trial():
    """Consume the explicit request before the regular prepared task can start."""
    try:
        fd = os.open(REQUEST, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as request:
        REQUEST.unlink()
        body = request.read(8193)
    data = json.loads(body)
    required = {"issued", "mesh", "mesh_id", "start", "goal", "capture", "output", "pose_source"}
    if (len(body) > 8192 or not isinstance(data, dict)
            or not required <= data.keys() <= required | {"dispatch", "profile", "entry_scene"}
            or not 0 <= time.time() - float(data["issued"]) <= 120
            or data["capture"] not in {"stream", "quartz"}
            or data["pose_source"] not in {"fast", "legacy"}
            or data.get("dispatch", "auto") not in DISPATCH_CHOICES
            or data.get("profile", "short") not in PROFILE_CHOICES
            or ("entry_scene" in data and (data.get("profile", "short") != "short"
                                           or not isinstance(data["entry_scene"], str)))
            or any(not isinstance(data[key], list) or len(data[key]) != 2 for key in ("start", "goal"))):
        raise NavigationError("trial_request_invalid_or_expired")
    data.pop("issued")
    data.setdefault("dispatch", "auto")
    data.setdefault("profile", "short")
    data.setdefault("entry_scene", None)
    data["output"] = Path(data["output"])
    return Namespace(**data, queue=False)


def observe(capture, x, y, *, forever):
    quest = quest_region(x, y)
    travel = travel_region(x, y, supply_bars=forever)
    world = nav3d_region(x, y, raised=forever)
    fast = fast_pose_region(x, y)
    strips = (quest, travel, world, fast)
    left, top = min(r[0] for r in strips), min(r[1] for r in strips)
    right = max(r[0] + r[2] for r in strips)
    started = time.monotonic()
    picture = capture.grab(left, top, right - left, y - top)
    captured = time.monotonic()

    def extract(rect):
        px, py, w, h = rect
        return picture[py-top:py-top+h, px-left:px-left+w]

    return (decode_quest(extract(quest)), decode_travel(extract(travel)), decode_nav3d(extract(world)),
            decode_fast_pose(extract(fast)),
            captured, (captured - started) * 1000)


def verified_pose(quest, navigation, pose, mesh):
    if not (quest and navigation and pose and navigation.recovery
            and (quest.quest_id, quest.state, quest.map_id)
            == (navigation.quest_id, navigation.phase, navigation.map_id)
            and (pose.quest_id, pose.phase, pose.ui_map_id, pose.sequence)
            == (navigation.quest_id, int(navigation.phase), navigation.map_id, navigation.sequence)):
        raise NavigationError("trial_incoherent_channels")
    if (pose.world_id != mesh.frame.world_id or str(pose.client_build) != mesh.frame.client_build.rsplit('.', 1)[-1]
            or f'quest:{pose.quest_id}:{pose.phase}' != mesh.frame.phase_id):
        raise NavigationError("trial_world_frame_changed")
    if (not navigation.task & TaskFlags.PREPARED or navigation.recovery.life != LifeState.ALIVE
            or navigation.recovery.dialog or quest.dialogue != Dialogue.CLOSED
            or not pose.grounded or not pose.bindings_verified or pose.in_combat
            or pose.position_xy is None or pose.heading is None):
        raise NavigationError("trial_unsafe_state")
    return pose


def confirm_stop(capture, where, mesh, sequence, *, forever):
    released = time.monotonic()
    last_xy = None
    stationary = []
    while time.monotonic() - released < 1.1:
        quest, navigation, pose, _, observed, _ = observe(capture, where.x, where.y, forever=forever)
        verified_pose(quest, navigation, pose, mesh)
        if pose.sequence != sequence:
            sequence = pose.sequence
            xy = pose.position_xy
            if last_xy is None or math.dist(xy, last_xy) > .05:
                stationary.clear()
            last_xy = xy
            stationary.append(dict(xy=xy, sequence=sequence, observed=observed))
            if len(stationary) >= 2 and observed - released >= .3:
                return stationary
        time.sleep(.016)
    return []


def pulse(backend, command, dispatch):
    if dispatch != "controller" and backend.start_navigation_pulse(command.tap, command.hold_s):
        return "native_timer"
    if dispatch == "native":
        raise NavigationError("trial_native_timer_unavailable")
    backend.tap_precise(command.tap, command.hold_s)
    return "controller"


def cmd_ground_trial(args):
    from ..cli import _prepared_channel_location

    start, goal = tuple(args.start), tuple(args.goal)
    continuous = args.profile == "continuous"
    extended = args.profile != "short"
    minimum, maximum, budget = (6, 32, 25) if extended else (1, 6, 15)
    if args.entry_scene is not None:
        if extended:
            print("网格入口验证需要使用短步。")
            return 2
        minimum, maximum = .50, 1.20
    distance = math.dist(start, goal)
    if not minimum <= distance <= maximum:
        print(f"实机验证目标距离需在 {minimum} 至 {maximum} 码之间。")
        return 2
    if args.queue:
        return queue_trial(args)
    if not has_helper_socket():
        print("短程实机验证需要已启用的 WowAgent 主控和键鼠会话。")
        return 2
    cfg = load_config()
    forever = cfg.game_version.startswith("1.60.")
    capture = ScreenCapture(cfg.capture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = {"capture_requested": args.capture, "mesh_id": args.mesh_id, "start_xy": start,
               "goal_xy": goal, "pose_requested": args.pose_source, "dispatch_requested": args.dispatch,
               "profile": args.profile, "max_planar_deviation_yards": 0.,
               "events": [], "route": None, "outcome": "startup_failed"}

    def log(event, data):
        records["events"].append({"event": event, "at": time.monotonic(), **data})

    def within_trial(path, ratio):
        return (path.length <= min(maximum + 3, ratio * distance)
                and all(math.dist(point[:2], start) <= maximum + .5 for point in path.points))

    backend = None
    last_sequence, previous_observed = None, None
    receipts = {}
    established = None
    planned_xy = ()
    pulse_until = 0.
    forward_held = False
    with args.output.open("x") as output:
        try:
            where = _prepared_channel_location(capture, Location(cfg.capture.monitor, cfg.channel.x, cfg.channel.y),
                                               12, prefer_stream=args.capture == "stream")
            navigator = Nav3DCoordinator(Nav3DConfig(mode="nav3d", mesh=args.mesh,
                supervised_trial_mesh_id=args.mesh_id, budget_s=budget, max_replans=int(extended)),
                now=time.monotonic(), short_steps=not continuous, arrival_radius=.28, log_event=log,
                clearance_route_allowed=lambda path: within_trial(path, 1.6))
            mesh = navigator.mesh
            backend = HelperInput(key_jitter=False)
            if args.dispatch == "native" and not backend.supports_navigation_pulse:
                raise NavigationError("trial_native_timer_unavailable")
            deadline = time.monotonic() + budget
            entry = entry_follower = None
            while time.monotonic() < deadline:
                if backend.interrupted():
                    raise NavigationError("trial_input_session_stopped")
                quest, navigation, base, fast, observed, capture_ms = observe(capture, where.x, where.y,
                                                                             forever=forever)
                verified_pose(quest, navigation, base, mesh)
                recent = with_fast_pose(base, fast)
                for source, sample in (("legacy", base), ("fast", recent)):
                    if sample is not None and sample.sequence != receipts.get(source):
                        receipts[source] = sample.sequence
                        log(source + "_pose", dict(sequence=sample.sequence, base_sequence=base.sequence,
                            xy=sample.position_xy, heading=sample.heading, observed=observed))
                pose = recent if args.pose_source == "fast" else base
                if pose is None or not (pose.grounded and pose.bindings_verified and pose.position_xy
                                         and pose.heading is not None) or pose.in_combat:
                    raise NavigationError("trial_fast_pose_unavailable_or_unsafe")
                if observed < pulse_until:
                    time.sleep(.008)
                    continue
                if pose.sequence == last_sequence:
                    if observed - previous_observed > .45:
                        raise NavigationError("trial_telemetry_stale")
                    time.sleep(.012)
                    continue
                last_sequence, previous_observed = pose.sequence, observed
                xy = pose.position_xy
                log("pose", dict(sequence=pose.sequence, xy=xy, heading=pose.heading, observed=observed,
                    capture_ms=capture_ms, source="stream" if capture._window_stream
                    and capture._window_stream.sequence is not None else "quartz"))
                if established is None:
                    if math.dist(xy, start) > .45:
                        raise NavigationError("trial_start_mismatch")
                    established = xy
                    continue
                if not planned_xy:
                    if math.dist(xy, established) > .12:
                        raise NavigationError("trial_start_moving")
                    if args.entry_scene is not None:
                        from .edge_entry import plan_edge_entry

                        entry = plan_edge_entry(mesh, navigator.tracker, args.entry_scene, xy, goal)
                        path = entry.route
                        entry_follower = WorldRouteFollower(path, now=observed, mesh_id=mesh.mesh_id,
                            obstacle_revision=path.obstacle_revision, supervised_trial=True,
                            short_steps=True, arrival_radius=.18, feedback_paced=True)
                        records["source_entry"] = dict(receding_slope_degrees=entry.receding_slopes,
                            swept_terrain_triangles=len(entry.triangles))
                    else:
                        starts, goals = navigator.tracker.candidates(xy), navigator.tracker.candidates(goal)
                        if (len(starts) != 1 or len(goals) != 1
                                or abs(starts[0][2] - goals[0][2]) > (2.5 if extended else .6)):
                            raise NavigationError("trial_support_ambiguous")
                        path = mesh.plan(Query(mesh.frame, starts[0], goals[0]))
                    planned_xy = tuple(p[:2] for p in path.points)
                    # Near-right-angle paths can exceed the short trial's
                    # directness bound without exceeding the absolute budget.
                    ratio = (1 / math.cos(math.radians(mesh.agent.max_slope)) + .02) if entry else (
                        1.6 if extended else 1.25)
                    if not within_trial(path, ratio):
                        raise NavigationError("trial_path_out_of_bounds")
                    records["route"] = path.to_dict()
                    if entry is None:
                        navigator.begin_exit(goal, now=observed, handoff=goal)
                deviation = WorldRouteFollower.distance_to_route(xy, planned_xy)
                records["max_planar_deviation_yards"] = max(records["max_planar_deviation_yards"], deviation)
                # The follower brakes and can repair at 2 yards. Keep an
                # independent envelope for the original supervised route.
                if math.dist(xy, start) > maximum + .5 or (extended and deviation > 2.6):
                    raise NavigationError("trial_position_out_of_bounds")
                if entry_follower is not None:
                    sample = WorldPose(mesh.frame, entry.position(xy), pose.heading, pose.sequence,
                                       observed, pose.grounded, pose.bindings_verified, pose.in_combat)
                    command = entry_follower.step(observed, sample, mesh_id=mesh.mesh_id,
                                                  obstacle_revision=entry.route.obstacle_revision)
                else:
                    command = navigator.step(observed, base, navigation, travel_only=True,
                                             fast_pose=fast if args.pose_source == "fast" else None)
                if command.outcome:
                    records["outcome"] = command.outcome
                    records["final_xy"] = xy
                    records["goal_error_yards"] = math.dist(xy, goal)
                    if command.outcome in {"nav3d_exit_reached", "nav3d_route_arrived"}:
                        backend.release_all()
                        stopped = confirm_stop(capture, where, mesh, base.sequence, forever=forever)
                        records["stopped"] = stopped
                        if not stopped:
                            records["outcome"] = "trial_stop_not_confirmed"
                        elif math.dist(stopped[-1]["xy"], goal) > .35:
                            records["outcome"] = "trial_settled_outside_goal"
                        elif entry is not None:
                            landed = stopped[-1]["xy"]
                            entry.position(landed)
                            confirmed = navigator.tracker.candidates(landed)
                            if len(confirmed) != 1:
                                records["outcome"] = "trial_entry_mesh_support_unconfirmed"
                            else:
                                records["outcome"] = "trial_mesh_entry_reached"
                                records["entry_confirmed_support"] = confirmed[0]
                    break
                if ((command.tap and (command.tap not in {"w", "left", "right"} or not command.precise))
                        or command.keys - ({"w"} if continuous else set()) or command.camera_yaw_px
                        or (command.tap and command.tap in command.keys)):
                    raise NavigationError("trial_unexpected_control")
                hold_forward = "w" in command.keys
                if hold_forward != forward_held:
                    issued = time.monotonic()
                    (backend.down if hold_forward else backend.up)("w")
                    forward_held = hold_forward
                    log("forward_hold", dict(held=hold_forward, issued=issued, returned=time.monotonic(), xy=xy))
                if command.tap:
                    sent = time.monotonic()
                    dispatch = pulse(backend, command, args.dispatch)
                    returned = time.monotonic()
                    if dispatch == "native_timer":
                        pulse_until = returned + command.hold_s
                    log("input", dict(sequence=pose.sequence, key=command.tap, issued=sent, hold_s=command.hold_s,
                        returned=returned, dispatch=dispatch))
            else:
                records["outcome"] = "trial_timeout"
        except (CaptureError, NavigationError, ValueError, OSError) as exc:
            records["outcome"] = str(exc)
        except KeyboardInterrupt:
            records["outcome"] = "trial_interrupt"
        finally:
            if backend:
                backend.release_all()
            capture.close()
            json.dump(records, output, ensure_ascii=False, indent=2)
    print(records["outcome"], args.output)
    return 0 if records["outcome"] in {"nav3d_exit_reached", "trial_mesh_entry_reached"} else 3
