"""Offline and bounded live terrain probes. No quest or combat inputs."""

from __future__ import annotations

import json
import logging
import math
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .control.navigation import compass_from_delta, heading_error
from .control.pathfinding import GridRouter
from .core.config import load_config
from .core.logging import setup_logging
from .core.paths import DATA_DIR, ROOT
from .decision.startlux import LocalStartLux
from .decision.terrain import STUCK_AFTER, decide_multiview, decide_terrain
from .perception import channel_spec as spec
from .perception.capture import ScreenCapture
from .perception.channel import ChannelReader
from .perception.travel_channel import TravelFlags, decode_travel, travel_region

log = logging.getLogger(__name__)
CROP = (.42, .10, .68, .635)
UNSAFE = (TravelFlags.MOUNTED | TravelFlags.FLYING | TravelFlags.FALLING
          | TravelFlags.SWIMMING | TravelFlags.CASTING)


class TerrainCombat(ValueError):
    pass


def inspect_images(images: list[Path], *, cropped: bool = False,
                   guard: Callable[[], None] | None = None) -> list[dict]:
    command = [str(DATA_DIR / "startlux/.venv/bin/python"),
               str(ROOT / "experiments/qwen3_vl/run.py")]
    if not cropped:
        command.extend(("--crop", *map(str, CROP)))
    timeout = 20 + 8 * (len(images) - 1)
    with subprocess.Popen([*command, *map(str, images)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as process:
        deadline = time.monotonic() + timeout
        try:
            while True:
                if guard:
                    guard()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"Qwen 视觉推理超过 {timeout} 秒")
                try:
                    stdout, stderr = process.communicate(timeout=min(.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    if process.returncode:
        raise ValueError("Qwen 视觉推理失败：" + (stderr.strip() or stdout.strip())[-500:])
    cases = json.loads(stdout)["cases"]
    if [case["image"] for case in cases] != [image.name for image in images]:
        raise ValueError("Qwen 返回的视角与输入不一致")
    return cases


def inspect_image(image: Path, *, cropped: bool = False, guard: Callable[[], None] | None = None) -> dict:
    return inspect_images([image], cropped=cropped, guard=guard)[0]


def game_scene(capture: ScreenCapture):
    import Quartz as q
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if not app or app.bundleIdentifier() != "com.blizzard.worldofwarcraft":
        raise ValueError("魔兽未在前台")
    pid = app.processIdentifier()
    windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID)
    for window in windows:
        bounds = window.get("kCGWindowBounds", {})
        if (window.get("kCGWindowOwnerPID") == pid and window.get("kCGWindowLayer") == 0
                and bounds.get("Width", 0) >= 800 and bounds.get("Height", 0) >= 500):
            left, top, right, bottom = CROP
            w, h = bounds["Width"], bounds["Height"]
            return capture.grab(bounds["X"] + round(w * left), bounds["Y"] + round(h * top),
                                round(w * right) - round(w * left), round(h * bottom) - round(h * top))
    raise ValueError("未找到可用的魔兽窗口")


def ground_pose(cfg, capture, guard):
    channel = cfg.channel
    reader = ChannelReader(channel)
    region = travel_region(channel.x, channel.y)
    previous = None
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        guard()
        reader.update(capture.grab(channel.x, channel.y, spec.REGION_W, spec.REGION_H))
        nav = decode_travel(capture.grab(*region))
        if nav and nav.position is not None and nav.world_size and nav.flags & TravelFlags.KEYS_VERIFIED:
            if nav.flags & TravelFlags.IN_COMBAT:
                raise TerrainCombat("角色仍在战斗中")
            if nav.flags & UNSAFE:
                raise ValueError("角色需下马、停止施法并站在地面")
            if previous is not None and nav.sequence != previous and reader.heading() is not None:
                status = reader.status()
                if status and status[0] & spec.StatusFlags.IN_COMBAT:
                    raise TerrainCombat("角色仍在战斗中")
                if status and status[0] & (spec.StatusFlags.DEAD | spec.StatusFlags.GHOST):
                    raise ValueError("角色状态不适合移动")
                if status:
                    return nav, reader.heading()
            previous = nav.sequence
        time.sleep(.035)
    raise ValueError("未读到实时寻路、状态和朝向色条")


def wait_ground_pose(cfg, capture, guard):
    deadline = time.monotonic() + 12
    while True:
        try:
            return ground_pose(cfg, capture, guard)
        except TerrainCombat:
            if time.monotonic() >= deadline:
                raise TerrainCombat("等待 12 秒后仍在战斗中")
            time.sleep(.25)


def save_scene(capture, path):
    import cv2

    scene = game_scene(capture)
    captured_at = time.monotonic()
    if not cv2.imwrite(str(path), cv2.cvtColor(scene, cv2.COLOR_RGB2BGR)):
        raise ValueError("无法保存探查画面")
    return captured_at


def same_ground(anchor, sample, router):
    return (anchor.map_id == sample.map_id and anchor.quest_id == sample.quest_id
            and router.distance(anchor.position, sample.position) <= 1)


def turn_to(target, anchor, cfg, capture, backend, guard, router):
    ccw = cfg.navigation.facing_convention == "ccw_west"
    for _ in range(8):
        pose, heading = ground_pose(cfg, capture, guard)
        if not same_ground(anchor, pose, router):
            raise ValueError("多视角采集时角色位置已变化")
        error = math.degrees(heading_error(heading, target))
        if abs(error) <= 12:
            return heading
        side = "left" if (error > 0) == ccw else "right"
        guard()
        backend.press_for(side, min(.28, max(.05, abs(error) / 180)))
        time.sleep(.45)
        turned, new_heading = ground_pose(cfg, capture, guard)
        yaw = math.degrees(heading_error(heading, new_heading))
        if not same_ground(anchor, turned, router) or not 3 <= yaw * (1 if error > 0 else -1) <= 85:
            raise ValueError("多视角转向与实机朝向不一致")
    raise ValueError("未能转到指定观察方向")


def short_step(before, heading, cfg, capture, backend, guard, router):
    guard()
    backend.press_for("w", .35)
    time.sleep(.25)
    moved, _ = ground_pose(cfg, capture, guard)
    distance = router.distance(before.position, moved.position)
    if moved.map_id != before.map_id or moved.quest_id != before.quest_id or distance > 5:
        raise ValueError("短步后地图、任务或位移超出预期")
    if distance < .8:
        log.info("terrain short step distance=%.2f verified=False reason=no_progress", distance)
        return False
    x, y = router.metric(before.position)
    tx, ty = router.metric(moved.position)
    bearing = compass_from_delta(tx - x, ty - y, cfg.navigation.facing_convention)
    if abs(heading_error(heading, bearing)) > math.radians(35):
        raise ValueError("短步的实际位移方向与计划不一致")
    log.info("terrain short step distance=%.2f verified=True position=%s", distance, moved.position)
    return True


def probe_multiview(cfg, capture, backend, guard, client, failed_headings):
    anchor, origin = ground_pose(cfg, capture, guard)
    router = GridRouter(*anchor.world_size)
    left = math.pi / 2 if cfg.navigation.facing_convention == "ccw_west" else -math.pi / 2
    offsets = {"front": 0, "left": left, "back": math.pi, "right": -left}
    headings = {}
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for direction, offset in offsets.items():
            headings[direction] = turn_to(origin + offset, anchor, cfg, capture, backend, guard, router)
            path = Path(tmp) / f"{direction}.png"
            save_scene(capture, path)
            paths.append(path)
        observations = inspect_images(paths, cropped=True, guard=guard)
        views = {direction: record["observation"] for direction, record in zip(headings, observations)}
        failed = frozenset(direction for direction, heading in headings.items()
                           if any(abs(heading_error(heading, prior)) <= math.radians(25)
                                  for prior in failed_headings))
        decision = decide_multiview(client, views, failed)
        log.info("terrain multiview decision %s", json.dumps({"views": views, "decision": decision},
                                                              ensure_ascii=False))
        chosen = decision.get("recommendation")
        if chosen not in headings:
            return
        heading = turn_to(headings[chosen], anchor, cfg, capture, backend, guard, router)
        confirm = Path(tmp) / "confirm.png"
        captured_at = save_scene(capture, confirm)
        observation = inspect_image(confirm, cropped=True, guard=guard)["observation"]
        if observation["front"] != "open":
            log.info("terrain multiview stopped: selected direction no longer looks open")
            return
        confirmation = decide_terrain(client, observation)
        pose, current_heading = ground_pose(cfg, capture, guard)
        if (not same_ground(anchor, pose, router) or time.monotonic() - captured_at > 15
                or abs(heading_error(heading, current_heading)) > math.radians(6)):
            raise ValueError("多视角复核时画面对应的位置或朝向已变化")
        if confirmation.get("recommendation") == "advance":
            short_step(pose, current_heading, cfg, capture, backend, guard, router)
        else:
            log.info("terrain multiview stopped: fresh view did not authorize a short step")


def run_live(args, client):
    from .control.estop import EStop
    from .control.input import create_input
    from .control.input_helper import has_helper_socket, in_controller_app
    from .modules.quest import game_is_frontmost, wait_for_game

    cfg = load_config()
    backend = stop = None
    capture = ScreenCapture(cfg.capture)
    if args.execute and (cfg.dry_run or not in_controller_app() or not has_helper_socket()):
        raise ValueError("实机探查需要从 WowAgent 主控启动并启用一次键鼠会话")
    wait_for_game()
    deadline = time.monotonic() + 120

    def guard():
        if time.monotonic() >= deadline:
            raise ValueError("已到本轮探查的时间上限")
        if not game_is_frontmost():
            raise ValueError("魔兽已离开前台")
        if stop and stop.check():
            raise ValueError("键鼠会话已停止")

    try:
        if args.execute:
            backend = create_input(cfg.input)
            if backend.name != "helper":
                raise ValueError("实机探查仅使用已启用的 WowAgent 键鼠会话")
            stop = EStop(cfg.input.estop_hotkey)
            stop.bind(backend)
            stop.start_hotkey()
        scans = ()
        initial_heading = None
        unresolved = 0
        failed_headings = []
        while True:
            before, heading = wait_ground_pose(cfg, capture, guard) if backend else ground_pose(cfg, capture, guard)
            if initial_heading is None:
                initial_heading = heading
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "scene.png"
                captured_at = save_scene(capture, path)
                vision = inspect_image(path, cropped=True, guard=guard)
            observation = vision["observation"]
            already_blocked = any(abs(heading_error(heading, prior)) <= math.radians(25)
                                  for prior in failed_headings)
            if already_blocked:
                observation = {**observation, "front": "blocked"}
            elif observation["front"] != "open":
                unresolved += 1
            if backend and unresolved >= STUCK_AFTER:
                log.info("terrain escalating to multiview after %d unresolved attempts", unresolved)
                probe_multiview(cfg, capture, backend, guard, client, failed_headings)
                return
            decision = decide_terrain(client, observation, scans)
            log.info("terrain decision %s", json.dumps({"map_id": before.map_id, "position": before.position,
                     "scans": scans, "vision": vision, "decision": decision}, ensure_ascii=False))
            action = decision.get("recommendation")
            if not backend or action in (None, "stop"):
                return
            router = GridRouter(*before.world_size)
            try:
                after, current_heading = ground_pose(cfg, capture, guard)
            except TerrainCombat:
                log.info("terrain decision discarded: combat began before input")
                fresh, fresh_heading = wait_ground_pose(cfg, capture, guard)
                if (fresh.map_id != before.map_id or fresh.quest_id != before.quest_id
                        or router.distance(before.position, fresh.position) > 1
                        or abs(heading_error(heading, fresh_heading)) > math.radians(6)):
                    raise ValueError("战斗后角色位置或朝向已变化，本轮探查结束")
                continue
            if (time.monotonic() - captured_at > 15 or before.map_id != after.map_id
                    or before.quest_id != after.quest_id
                    or router.distance(before.position, after.position) > 1
                    or abs(heading_error(heading, current_heading)) > math.radians(6)):
                raise ValueError("画面对应的位置或朝向已变化，未执行探查动作")
            guard()
            if action.startswith("scan_"):
                side = action.removeprefix("scan_")
                continued = bool(scans and side == scans[-1])
                duration = .22 if continued else (.56 if scans else .28)
                backend.press_for(side, duration)
                time.sleep(.55)
                turned, new_heading = ground_pose(cfg, capture, guard)
                yaw = math.degrees(heading_error(current_heading, new_heading))
                from_origin = math.degrees(heading_error(initial_heading, new_heading))
                positive = (side == "left") == (cfg.navigation.facing_convention == "ccw_west")
                if (turned.map_id != after.map_id or router.distance(after.position, turned.position) > 1
                        or not 8 <= (yaw if positive else -yaw) <= (125 if scans else 85)
                        or not 8 <= (from_origin if positive else -from_origin) <= (155 if continued else 85)):
                    raise ValueError(f"实际转向与计划不一致（{yaw:.1f}°），探查停止")
                scans += (side,)
                log.info("terrain turn verified side=%s yaw=%.1f", side, yaw)
            elif action == "advance":
                if short_step(after, current_heading, cfg, capture, backend, guard, router):
                    return
                failed_headings.append(current_heading)
                unresolved += 1
                if unresolved >= STUCK_AFTER:
                    log.info("terrain escalating to multiview after %d unresolved attempts", unresolved)
                    probe_multiview(cfg, capture, backend, guard, client, failed_headings)
                    return
    finally:
        if backend:
            backend.release_all()
        if stop:
            stop.stop()


def cmd_terrain(args):
    setup_logging("INFO", DATA_DIR / "logs/bot.log")
    client = LocalStartLux(args.startlux_port, timeout=10)
    try:
        if args.image:
            if args.execute:
                raise ValueError("历史图片仅用于离线决策")
            vision = inspect_image(args.image)
            print(json.dumps({"vision": vision, "decision": decide_terrain(client, vision["observation"])},
                             ensure_ascii=False, indent=2))
        else:
            run_live(args, client)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        log.error("terrain stopped: %s", exc)
        return 2
    return 0


def add_parser(sub):
    parser = sub.add_parser("terrain-probe", help="本地视觉和 StartLux 地形探查")
    parser.add_argument("--image", type=Path, help="用保存的截图离线测试")
    parser.add_argument("--startlux-port", type=int, default=8090)
    parser.add_argument("--execute", action="store_true", help="在主控中运行单视角探查和卡住后的多视角恢复")
    parser.set_defaults(func=cmd_terrain)
