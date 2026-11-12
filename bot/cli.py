"""WowAgent controller CLI.

Main commands:
  init-config          write default config/items/route files
  selftest             synthetic end-to-end run (no game required)
  probe-channel        live-decode the pixel channel, print stats/values
  probe-quests         classify quests from the optional quest strip
  probe-audio          list audio devices / live band-energy probe
  calibrate            interactive screen point/rect picker -> config
  calibrate-channel    find and verify the main pixel strip -> config
  learn-spell          watch CAST events, print spell ids (fill addon cfg)
  record-route         record POS stream into a route yaml
  run                  run a module (fisher|gatherer|seller|loop|quest)
  report               print/save a markdown run report
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from .core.config import BotConfig, load_config, load_items, save_config
from .core.events import EventBus
from .core.logging import setup_logging
from .core.paths import CONFIG_DIR, DATA_DIR, ROOT, ROUTES_DIR, ensure_dirs
from .data.db import Database
from .data.reports import build_report, save_report
from .ops.notify import Notifier
from .ops.watchdog import Watchdog
from .perception import channel_spec as spec
from .perception.audio import AudioMonitor
from .perception.capture import CaptureError, ScreenCapture
from .perception.channel import ChannelReader

log = logging.getLogger("cli")


# --------------------------------------------------------------- helpers ----
def _build_runtime(cfg: BotConfig, dry_run: bool, want_audio: bool = True):
    from .control.estop import EStop
    from .control.input import create_input
    from .modules.context import ModuleContext

    capture = ScreenCapture(cfg.capture)
    channel = ChannelReader(cfg.channel)
    input_backend = create_input(cfg.input, force_mock=dry_run)
    audio = AudioMonitor(cfg.audio)
    if want_audio and not dry_run:
        audio.start()
    db = Database(DATA_DIR / "wowagnet.db")
    bus = EventBus()
    estop = EStop(cfg.input.estop_hotkey, bus)
    estop.bind(input_backend)
    notifier = Notifier(cfg.notify)
    ctx = ModuleContext(cfg, capture, channel, input_backend, audio, db, bus, estop, notifier)
    return ctx, estop


def _maybe_caffeinate(cfg: BotConfig) -> Optional[subprocess.Popen]:
    if not cfg.input.prevent_sleep or sys.platform != "darwin" or cfg.dry_run:
        return None
    try:
        p = subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("caffeinate started (pid %s)", p.pid)
        return p
    except Exception as exc:  # noqa: BLE001
        log.warning("caffeinate failed: %s", exc)
        return None


# ------------------------------------------------------------ init-config ----
DEFAULT_BOT_YAML = """\
# WowAgent 主配置 —— 由 `python -m bot.cli init-config` 生成
# 所有坐标都是“截图像素坐标”，用 `python -m bot.cli calibrate` 采集。
game_version: "12.x"
dry_run: false          # true = 只感知+决策，不注入任何输入
log_level: INFO
game_process_names: ["World of Warcraft"]

channel:
  x: 40                # 像素通道色条左上角（游戏内 /wagent unlock 拖动后校准）
  y: 900
  calibrated: false

capture:
  monitor: 1
  control_fps: 15

audio:
  enabled: true
  device: null          # macOS 需要装 BlackHole 并填设备号；probe-audio 可查
  bite_threshold: 6.0

input:
  backend: auto         # auto/mac/win/mock
  mouse_turn_gain: 900
  invert_turn: false    # 游戏内转向方向反了就改 true
  estop_hotkey: "<ctrl>+<alt>+<f12>"

navigation:
  facing_convention: ccw_west   # 转向镜像时改 cw_east

fishing:
  key_cast: "1"
  key_interact: "f"
  bite_window_min_s: 3.0
  bite_window_max_s: 30.0
  bobber_roi: [0, 0, 0, 0]     # 校准：鱼漂落水区域

gathering:
  minimap_roi: [0, 0, 0, 0]    # 校准：整个小地图
  yards_per_px: 0.55
  node_click_offset: [0, 40]
  key_descend: "x"

selling:
  key_hearthstone: "2"
  key_interact: "f"
  route_to_ah: hub_to_ah
  route_to_mail: ah_to_mail
  route_return: hub_to_spot

rhythm:
  work_min_s: 5400
  work_max_s: 9000
  rest_min_s: 600
  rest_max_s: 1800
  maintenance_windows: [["Thu 05:00", "Thu 09:00"]]

notify:
  provider: none      # none | bark | serverchan
  key: ""
"""

DEFAULT_ITEMS_YAML = """\
# 白名单：插件计数（BAG 消息）与售卖关注。itemID -> {name, reserve(保留数量)}
# 示例（按你实际钓获/采集的物品替换，游戏内把鼠标悬停在物品上可看 itemID，
# 或用 Auctionator 悬停提示）：
# 173032:
#   name: 示例鱼
#   reserve: 0
"""

DEFAULT_ROUTE_YAML = """\
# 采集/回城路线：record-route 录制生成，也可手写。points 为地图坐标 [x, y]。
name: gather_route
ui_map_id: 0
mode: fly            # fly | ground
arrival_radius: null # null = 按相邻点中位距离自动
points: []
"""


def cmd_init_config(args: argparse.Namespace) -> int:
    ensure_dirs()
    (CONFIG_DIR / "bot.yaml").write_text(DEFAULT_BOT_YAML, encoding="utf-8")
    (CONFIG_DIR / "items.yaml").write_text(DEFAULT_ITEMS_YAML, encoding="utf-8")
    for name, body in (
        ("gather_route", DEFAULT_ROUTE_YAML),
        ("hub_to_ah", DEFAULT_ROUTE_YAML.replace("gather_route", "hub_to_ah")),
        ("ah_to_mail", DEFAULT_ROUTE_YAML.replace("gather_route", "ah_to_mail")),
        ("hub_to_spot", DEFAULT_ROUTE_YAML.replace("gather_route", "hub_to_spot")),
    ):
        p = ROUTES_DIR / f"{name}.yaml"
        if not p.exists():
            p.write_text(body, encoding="utf-8")
    print(f"默认配置已写入 {CONFIG_DIR}")
    return 0


# ---------------------------------------------------------------- selftest ----
def cmd_selftest(args: argparse.Namespace) -> int:
    from . import selftest as st

    return st.main()


# ------------------------------------------------------------ probe-channel ----
def cmd_probe_channel(args: argparse.Namespace) -> int:
    cfg = load_config()
    capture = ScreenCapture(cfg.capture)
    reader = ChannelReader(cfg.channel)
    print(f"通道区域: ({cfg.channel.x}, {cfg.channel.y}) 监视 {args.duration}s …（游戏内确认 WowAgent 已加载）")
    t0 = time.time()
    last_print = 0.0
    try:
        while time.time() - t0 < args.duration:
            img = capture.grab(cfg.channel.x, cfg.channel.y, spec.REGION_W, spec.REGION_H)
            reader.update(img)
            if time.time() - last_print >= 1.0:
                last_print = time.time()
                pos = reader.pos()
                hdg = reader.heading()
                st = reader.status()
                bag = reader.bagsum()
                print(
                    f"ok={reader.frames_ok} bad={reader.frames_bad} "
                    f"pos={pos and (round(pos[0],4), round(pos[1],4))} "
                    f"hdg={hdg and round(hdg, 3)} st={st} bag={bag} alive={reader.is_alive()}"
                )
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    total = reader.frames_ok + reader.frames_bad
    print(f"\n合计: ok={reader.frames_ok} bad={reader.frames_bad} 误码率={reader.error_rate():.4f}")
    if total == 0:
        print("没有任何有效帧：检查 WowAgent 是否加载、色条位置是否与 config.channel 一致。")
        return 1
    return 0 if reader.error_rate() < 0.05 else 2


# -------------------------------------------------------------- probe-audio ----
def cmd_probe_audio(args: argparse.Namespace) -> int:
    import sounddevice as sd

    print("音频设备列表:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0 or d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}  in={d['max_input_channels']} out={d['max_output_channels']}")
    cfg = load_config()
    if args.device is not None:
        cfg.audio.device = args.device
    mon = AudioMonitor(cfg.audio)
    if not mon.start():
        print("无法打开设备——macOS 需要 BlackHole 等回环设备并在 config 指定 device 序号")
        return 1
    print(f"\n监听 {args.duration}s（期间在游戏里抛竿制造上钩音效）…")
    t0 = time.time()
    try:
        while time.time() - t0 < args.duration:
            e, b, r = mon.probe_tick()
            bar = "#" * min(60, int(r * 4))
            print(f"\renergy={e:9.1f} base={b:9.1f} ratio={r:7.2f} {bar:<60}", end="")
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    print("\n结束。把 bite_threshold 设为观察到峰值的 ~60%。")
    mon.stop()
    return 0


# --------------------------------------------------------------- calibrate ----
def cmd_calibrate_impl(args: argparse.Namespace) -> int:  # pragma: no cover - interactive
    """Interactive picker: move mouse, hotkeys: c=point, r=rect(两次), q=quit."""
    from pynput import keyboard, mouse

    cfg = load_config()
    cap = ScreenCapture(cfg.capture)
    ms = mouse.Controller()
    kb = keyboard.Controller()
    points: dict[str, tuple[int, int]] = {}
    rect_a: Optional[tuple[int, int]] = None
    print("校准工具：移动鼠标到目标位置后按 c 记录点；按 r 两次记录矩形；按 q 退出")
    print("先测量: 1) channel(通道色条左上角) 2) bobber(鱼漂水域左上+右下) 3) minimap(小地图左上+右下)")

    def on_press(key):
        nonlocal rect_a
        try:
            k = key.char if hasattr(key, "char") else key.name
        except Exception:
            return
        pos = ms.position()
        if k == "c":
            print(f"  点: {pos}")
            points[f"point{len(points)+1}"] = pos
        elif k == "r":
            if rect_a is None:
                rect_a = pos
                print(f"  矩形左上: {pos} — 移到右下再按 r")
            else:
                x0, y0 = rect_a
                rect = (min(x0, pos[0]), min(y0, pos[1]), abs(pos[0] - x0), abs(pos[1] - y0))
                rect_a = None
                print(f"  矩形: {rect}")
                points[f"rect{sum(1 for k2 in points if k2.startswith('rect'))+1}"] = rect
        elif k == "q":
            return False
        return None

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()
    print("\n已记录:")
    for k, v in points.items():
        print(f"  {k} = {v}")
    print("把上面的值手工填入 config/bot.yaml（channel: x/y；fishing.bobber_roi；gathering.minimap_roi）")
    print("（自动写回将在真实环境联调时完善，当前保守手工填写避免误写）")
    return 0


# ------------------------------------------------------------- learn-spell ----
def cmd_learn_spell(args: argparse.Namespace) -> int:
    cfg = load_config()
    capture = ScreenCapture(cfg.capture)
    reader = ChannelReader(cfg.channel)
    seen: set[int] = set()
    print(f"监听通道 {args.duration}s —— 在游戏里手动钓鱼/采集/施法，CAST 事件将打印 spellIdx")
    t0 = time.time()
    while time.time() - t0 < args.duration:
        img = capture.grab(cfg.channel.x, cfg.channel.y, spec.REGION_W, spec.REGION_H)
        reader.update(img)
        for ev in reader.events:
            key = (ev.kind, ev.seq, ev.payload)
            if key not in seen:
                seen.add(key)
                kind_name = spec.FastEvKind(ev.kind).name
                print(f"  {time.strftime('%H:%M:%S')} {kind_name} seq={ev.seq} payload={ev.payload}")
        time.sleep(0.05)
    return 0


# ------------------------------------------------------------ record-route ----
def cmd_record_route(args: argparse.Namespace) -> int:
    from pynput import keyboard

    cfg = load_config()
    capture = ScreenCapture(cfg.capture)
    reader = ChannelReader(cfg.channel)
    pts: list[tuple[float, float]] = []
    recording = {"on": False}
    stop = {"flag": False}
    print("路线录制：'s' 开始/暂停记录，'q' 保存并退出（人物沿路线走/飞）")

    def on_press(key):
        try:
            k = key.char if hasattr(key, "char") else key.name
        except Exception:
            return
        if k == "s":
            recording["on"] = not recording["on"]
            print("  > 记录中" if recording["on"] else "  > 暂停")
        elif k == "q":
            stop["flag"] = True
            return False
        return None

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    zone_id = None
    while not stop["flag"]:
        img = capture.grab(cfg.channel.x, cfg.channel.y, spec.REGION_W, spec.REGION_H)
        reader.update(img)
        z = reader.zone()
        if z is not None:
            zone_id = z
        if recording["on"]:
            p = reader.pos()
            if p is not None:
                if not pts or (abs(p[0] - pts[-1][0]) + abs(p[1] - pts[-1][1])) > 0.002:
                    pts.append(p)
        time.sleep(0.1)
    listener.stop()
    if not pts:
        print("未录到任何点（通道无 POS？先跑 probe-channel）")
        return 1
    out = ROUTES_DIR / f"{args.name}.yaml"
    body = [
        f"name: {args.name}",
        f"ui_map_id: {zone_id or 0}",
        f"mode: {args.mode}",
        "arrival_radius: null",
        "points:",
    ]
    body += [f"  - [{p[0]:.4f}, {p[1]:.4f}]" for p in pts]
    out.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"已保存 {len(pts)} 个点到 {out}")
    return 0


# -------------------------------------------------------------------- run ----
def _prepared_channel_location(capture, previous, timeout):
    from .modules.quest import game_is_frontmost, wait_for_game
    from .perception.calibration import ChannelNotFound, startup_location

    deadline = time.monotonic() + timeout
    reported_retry = False
    while True:
        if not game_is_frontmost():
            wait_for_game(activate=False, timeout=max(0, deadline - time.monotonic()))
        remaining = deadline - time.monotonic()
        try:
            location = startup_location(capture, previous, scan_duration=min(5, max(0, remaining)))
        except ChannelNotFound:
            if time.monotonic() >= deadline:
                raise
            if not reported_retry:
                log.info("主色条暂未通过核对，将继续扫描。请保持魔兽窗口可见。")
                reported_retry = True
            continue
        if game_is_frontmost():
            return location
        if time.monotonic() >= deadline:
            raise ValueError("魔兽未保持在前台，无法确认任务色条。")
        previous = location


def cmd_run(args: argparse.Namespace) -> int:
    from .control.input_helper import has_helper_socket, in_controller_app

    cfg = load_config()
    if args.dry_run:
        cfg.dry_run = True
    quest_module_args = None
    quest_mode = args.module in {"quest", "quest_accept", "quest_navigate"}
    if quest_mode:
        from .modules.quest import QuestAcceptModule, QuestModule, QuestNavigateModule, load_quest_plan
        from .perception.quest_channel import QuestKind

        if args.module == "quest" and args.quest_plan is None:
            print("quest 模块需要 --quest-plan PATH", file=sys.stderr)
            return 2
        try:
            quest_module_args = (load_quest_plan(args.quest_plan) if args.quest_plan else None,
                                 {QuestKind[k.upper()] for k in args.quest_kinds})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(f"任务计划无效：{exc}", file=sys.stderr)
            return 2
        if not (cfg.dry_run or args.mock_input or in_controller_app()):
            if sys.platform != "darwin":
                print("实机任务目前支持 macOS。", file=sys.stderr)
                return 2
            from ApplicationServices import AXIsProcessTrusted

            if not AXIsProcessTrusted():
                print("实机任务需要运行环境的 macOS 辅助功能权限。", file=sys.stderr)
                return 2
    setup_logging(cfg.log_level, DATA_DIR / "logs" / "bot.log")
    try:
        if in_controller_app() and not (cfg.dry_run or args.mock_input or cfg.input.backend == "mock"):
            if not has_helper_socket():
                raise ValueError("请先打开 WowAgent Keyboard Mouse.app")
            from .modules.quest import PREPARED_FOCUS_WAIT_S, wait_for_game

            if getattr(args, "require_prepared", False):
                from .perception.calibration import Location

                previous = Location(cfg.capture.monitor, cfg.channel.x, cfg.channel.y)
                log.info("核对当前游戏窗口的色条位置，最多等待 %s 秒。", PREPARED_FOCUS_WAIT_S)
                capture = ScreenCapture(cfg.capture)
                location = _prepared_channel_location(capture, previous, PREPARED_FOCUS_WAIT_S)
                cfg.capture.monitor = location.monitor
                cfg.channel.x, cfg.channel.y = location.x, location.y
                cfg.channel.calibrated = True
                if location != previous:
                    log.info("色条位置已自动校正：显示器 %s，坐标 (%s, %s)。",
                             location.monitor, location.x, location.y)
            else:
                wait_for_game()
        ctx, estop = _build_runtime(cfg, dry_run=cfg.dry_run or args.mock_input, want_audio=not quest_mode)
    except (CaptureError, ValueError, OSError) as exc:
        log.error("无法启动：%s", exc)
        return 2
    caf = _maybe_caffeinate(cfg)

    if not in_controller_app() or ctx.input.name == "helper":
        estop.start_hotkey()
    wd = Watchdog(cfg.game_process_names, on_lost=lambda reason: estop.fire(reason))
    if not cfg.dry_run:
        wd.start()

    from .modules.fisher import FisherModule
    from .modules.gatherer import GathererModule
    from .modules.loop import LoopRunner
    from .modules.seller import SellerModule

    session_id = ctx.db.start_session(args.module, {"dry_run": cfg.dry_run})
    try:
        if args.module == "loop":
            if args.route:
                ctx.route_name = args.route
            runner = LoopRunner(ctx, primary=args.primary)
            outcome = runner.run(args.max_duration)
        elif quest_mode:
            from .decision.startlux import LocalStartLux, QuestPlanner

            planner = QuestPlanner(LocalStartLux(args.startlux_port))
            if args.module == "quest_accept":
                runner = QuestAcceptModule(ctx, args.key, planner, quest_module_args[1])
            elif args.module == "quest_navigate":
                runner = QuestNavigateModule(ctx, planner, quest_module_args[1], args.travel_only,
                                             args.require_prepared)
            else:
                runner = QuestModule(ctx, *quest_module_args, planner=planner)
            max_duration = args.max_duration
            if max_duration is None:
                max_duration = 3600.0 if args.require_prepared and not args.travel_only else 600.0
            outcome = runner.run(max_duration)
        else:
            mod = {"fisher": FisherModule, "gatherer": GathererModule, "seller": SellerModule}[args.module]
            if args.module == "gatherer" and args.route:
                from .control.navigation import Route, RouteFollower
                from .core.config import load_route

                ctx.gather_follower = RouteFollower(Route.from_dict(load_route(args.route)), cfg.navigation, loop=True)
                ctx.route_mode = ctx.gather_follower.route.mode
            outcome = mod(ctx).run(args.max_duration)
        print(f"结果: {outcome.status}  统计: {outcome.stats}")
        ctx.db.end_session(session_id, outcome.status, outcome.stats)
        if getattr(args, "require_prepared", False):
            explanations = {
                "stopped:quest_automatic_not_prepared": "请在插件里点「一键准备自动任务」，等核对成功后再启动。",
                "stopped:quest_mount_button_unavailable": "未核对到原生驭空坐骑图标。请关闭插件面板和地图，并确认动作栏上有原生驭空坐骑图标。",
                "stopped:quest_mount_not_confirmed": "已尝试使用原生驭空坐骑，游戏尚未确认骑乘任务进度。请查看游戏内提示及任务运行日志。",
                "stopped:quest_object_interaction_unconfirmed": "已点击确认的任务物品，但任务进度尚未增加。角色已停在当前物品附近，请查看游戏内距离或互动提示。",
                "stopped:game_not_frontmost": "魔兽未保持前台，脚本已停止。点击游戏窗口后让游戏保持在前台。",
                "stopped:quest_navigation_not_prepared": "已找到主色条，但未读取到寻路色条。请在插件里点「一键准备自动任务」，等准备完成后再启动。",
                "stopped:quest_navigation_channel_stale": "寻路色条停止更新。请保持游戏和色条可见，再准备并启动任务。",
                "stopped:quest_automatic_preparation_lost": "运行中失去了已经核对的任务键位或游戏设置。",
                "stopped:quest_turn_in_npc_not_in_interact_range": "游戏没有识别到范围内的交付 NPC，请核对名字和位置。",
                "stopped:quest_pickup_npc_not_found": "已搜索任务起点附近，仍未确认可交互的接取 NPC。",
                "stopped:quest_pickup_npc_selection_unconfirmed": "已发现接取 NPC 并尝试选中，但游戏未确认目标，未发出互动键。",
                "stopped:quest_turn_in_npc_lost": "靠近时失去了已确认的交付 NPC，已停止移动。",
                "stopped:quest_turn_in_approach_blocked": "尝试靠近 NPC 时，角色位置没有变化。请检查脚下或前方是否有障碍。",
                "stopped:quest_turn_in_approach_exhausted": "已向交付 NPC 靠近三次，仍未打开任务对话。",
                "stopped:quest_turn_in_range_addon_required": "请重载新版 WowAgent 插件并重新准备任务，以启用 NPC 距离检查。",
                "stopped:quest_turn_in_range_unknown": "游戏没有返回可确认的 NPC 距离，未尝试互动。",
                "stopped:quest_turn_in_outside_five_yards": "已尝试靠近，仍在交付 NPC 的 5 码范围外。",
                "stopped:quest_reward_choice_required": "游戏要求选择奖励，请在任务对话中选择。",
                "stopped:quest_objective_profile_required": "任务已在日志中，当前目标无法匹配已实现的动作。请查看游戏内目标后再继续。",
                "stopped:quest_object_not_found": "已搜索任务标记附近，游戏没有返回可确认的任务物件。",
                "stopped:quest_defense_preparation_unavailable": "遇袭后无法核对任务键位和色条，未发送战斗按键。",
                "stopped:quest_defense_binding_or_telemetry_unavailable": "遇袭后未能确认落地状态、战斗色条或输出键，请查看任务日志。",
                "stopped:quest_defense_timeout": "自卫超时，仍未脱战。请查看任务运行日志。",
                "stopped:quest_round_state_changed": "本轮任务的接取或可交付状态发生变化，请在游戏中核对任务日志。",
                "stopped:quest_round_advance_timeout": "游戏未确认切换到本轮的下一项，请查看任务日志。",
                "stopped:quest_round_unexpected_switch": "游戏中的追踪任务或阶段已改变，请重新准备附近任务。",
                "stopped:quest_destination_unavailable": "游戏没有返回当前目标或交付点的可用坐标。",
                "stopped:quest_offer_not_open": "需要从当前任务的接取对话开始。",
                "stopped:quest_accept_not_confirmed": "已经尝试接取，但任务日志未确认该任务。",
                "stopped:quest_followup_not_confirmed": "前一任务已交付，但插件没有确认切换到后续任务。",
                "stopped:quest_startlux_timeout": "本地 StartLux 推理超时，任务在发送按键前停止。",
                "stopped:quest_startlux_unavailable": "本地 StartLux 请求失败，请查看本次任务日志中的错误。",
            }
            for prefix in ("quest_combat_", "quest_defense_"):
                explanations.update({
                    f"stopped:{prefix}facing_feedback_unavailable": "游戏报告朝向错误，但未能核对转向键或角色朝向。",
                    f"stopped:{prefix}facing_turn_timeout": "转向后未在时限内确认角色朝向变化。",
                    f"stopped:{prefix}facing_unconfirmed": "尝试多个朝向后，游戏仍报告朝向错误。",
                })
            if outcome.status in explanations:
                print(explanations[outcome.status])
            return 0 if outcome.status == "completed" else 3
        return 0 if not outcome.status.startswith("stopped") else 3
    finally:
        ctx.input.release_all()
        estop.stop()
        wd.stop()
        ctx.audio.stop()
        ctx.db.close()
        if caf:
            caf.terminate()


# ----------------------------------------------------------------- report ----
def cmd_report(args: argparse.Namespace) -> int:
    db = Database(DATA_DIR / "wowagnet.db")
    text = build_report(db, since_hours=args.hours)
    print(text)
    if args.save:
        p = save_report(db, since_hours=args.hours)
        print(f"已保存: {p}")
    db.close()
    return 0


def cmd_probe_quests(args: argparse.Namespace) -> int:
    from .perception.quest_channel import decode_quest, quest_region

    cfg = load_config()
    cap = ScreenCapture(cfg.capture)
    region = quest_region(cfg.channel.x, cfg.channel.y)
    reader = ChannelReader(cfg.channel)
    x, y, width, height = region
    capture_region = (x, y, width, height + spec.REGION_H) if args.waypoints else region
    seen = {}
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        image = cap.grab(*capture_region)
        if args.waypoints:
            reader.update(image[height:])
        sample = decode_quest(image[:height])
        if sample:
            pos = reader.pos(max_age_s=.6) if args.waypoints else None
            waypoint = [round(value, 4) for value in pos] if pos else None
            state = (sample.kind.name, sample.state.name, sample.objective.name, sample.progress,
                     sample.map_id, sample.dialogue_id, sample.dialogue.name, waypoint)
            if seen.get(sample.quest_id) != state:
                seen[sample.quest_id] = state
                print(f"id={sample.quest_id} kind={state[0]} state={state[1]} objective={state[2]} "
                      f"progress={state[3]} map={state[4]} dialogue={state[6]}:{state[5]}"
                      + (f" waypoint={waypoint}" if waypoint is not None else ""), flush=True)
        time.sleep(.1)
    if not seen:
        print("未读取到任务色条。请先在游戏内执行 /wagent quest scan。", file=sys.stderr)
        return 2
    return 0


def cmd_probe_navigation(args: argparse.Namespace) -> int:
    from .perception.travel_channel import TravelFlags, decode_travel, travel_region

    cfg = load_config()
    capture = ScreenCapture(cfg.capture)
    region = travel_region(cfg.channel.x, cfg.channel.y)
    sequence = None
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        sample = decode_travel(capture.grab(*region))
        if sample:
            if sequence is not None and sample.sequence != sequence:
                result = dataclasses.asdict(sample)
                result["phase"] = sample.phase.name
                result["flags"] = [flag.name for flag in TravelFlags if sample.flags & flag]
                print(json.dumps(result, ensure_ascii=False))
                return 0
            sequence = sample.sequence
        time.sleep(.1)
    print("未读到持续更新的寻路色条。请先在插件中点「准备任务寻路」。", file=sys.stderr)
    return 2


# ------------------------------------------------------------------ main ----
def _run_controller_once(argv: list[str]) -> int:
    from .control.input_helper import in_controller_app

    if sys.platform != "darwin" or not in_controller_app():
        print("此选项需要由 WowAgent 主控 App 启动。", file=sys.stderr)
        return 2
    from AppKit import NSRunningApplication

    parent = NSRunningApplication.runningApplicationWithProcessIdentifier_(os.getppid())
    expected = (DATA_DIR / "apps" / "WowAgent Controller.app").resolve()
    if (not parent or parent.bundleIdentifier() != "io.wowagent.controller"
            or not parent.bundleURL() or Path(parent.bundleURL().path()).resolve() != expected):
        print("无法核对本次启动的主控 App。", file=sys.stderr)
        return 2
    try:
        return main(argv)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        parent.terminate()


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--controller-once"]:
        return _run_controller_once(argv[1:])

    from .calibrate_cli import add_parser as add_calibration_parser
    from .combat_cli import add_parser as add_combat_parser
    from .terrain_cli import add_parser as add_terrain_parser

    parser = argparse.ArgumentParser(prog="wowagnet", description="WoW retail automation controller")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_combat_parser(sub)
    add_calibration_parser(sub)
    add_terrain_parser(sub)

    sub.add_parser("init-config", help="生成默认配置文件").set_defaults(func=cmd_init_config)

    sub.add_parser("selftest", help="合成环境端到端自测（无需游戏）").set_defaults(func=cmd_selftest)

    p = sub.add_parser("probe-channel", help="像素通道解码探针")
    p.add_argument("--duration", type=float, default=20.0)
    p.set_defaults(func=cmd_probe_channel)

    p = sub.add_parser("probe-quests", help="只读查看任务分类和目标进度")
    p.add_argument("--duration", type=float, default=15.0)
    p.add_argument("--waypoints", action="store_true", help="附带当前地图上的归一化坐标")
    p.set_defaults(func=cmd_probe_quests)

    p = sub.add_parser("probe-navigation", help="只读核对任务目标和移动能力")
    p.add_argument("--duration", type=float, default=3.0)
    p.set_defaults(func=cmd_probe_navigation)

    p = sub.add_parser("accept-quest", help="本地 StartLux 决策，脚本接受已准备的当前 NPC 任务")
    p.add_argument("--key", choices=[str(key) for key in range(1, 10)], default="9")
    p.add_argument("--startlux-port", type=int, default=8090)
    p.add_argument("--max-duration", type=float, default=30.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--mock-input", action="store_true")
    p.set_defaults(func=cmd_run, module="quest_accept", quest_plan=None,
                   quest_kinds=["campaign", "mainline", "side", "important"])

    p = sub.add_parser("navigate-quest", help="按原生任务目标坐标寻路并执行已准备任务")
    p.add_argument("--startlux-port", type=int, default=8090)
    p.add_argument("--max-duration", type=float, help="最长运行秒数，自动任务默认 3600，其余默认 600")
    p.add_argument("--travel-only", action="store_true", help="只验证寻路，抵达落地后结束")
    p.add_argument("--require-prepared", action="store_true", help="仅运行插件内一键准备并核对的任务")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--mock-input", action="store_true")
    p.set_defaults(func=cmd_run, module="quest_navigate", quest_plan=None,
                   quest_kinds=["campaign", "mainline", "side", "important"])

    p = sub.add_parser("probe-audio", help="音频设备与能量探针")
    p.add_argument("--duration", type=float, default=15.0)
    p.add_argument("--device", type=int, default=None)
    p.set_defaults(func=cmd_probe_audio)

    p = sub.add_parser("calibrate", help="屏幕坐标/矩形校准工具")
    p.set_defaults(func=cmd_calibrate_impl)

    p = sub.add_parser("learn-spell", help="监听 CAST 事件打印法术编号")
    p.add_argument("--duration", type=float, default=60.0)
    p.set_defaults(func=cmd_learn_spell)

    p = sub.add_parser("record-route", help="录制采集/回城路线")
    p.add_argument("--name", default="gather_route")
    p.add_argument("--mode", default="fly", choices=["fly", "ground"])
    p.set_defaults(func=cmd_record_route)

    p = sub.add_parser("run", help="运行模块")
    p.add_argument("--module", required=True, choices=["fisher", "gatherer", "seller", "loop", "quest"])
    p.add_argument("--route", default=None, help="gatherer/loop 使用的路线名")
    p.add_argument("--quest-plan", type=Path, help="quest 使用的同地图计划文件")
    p.add_argument("--quest-kinds", nargs="+", choices=["campaign", "mainline", "side", "important"],
                   default=["campaign", "mainline", "side"], help="quest 允许执行的分类")
    p.add_argument("--startlux-port", type=int, default=8090, help="任务模块使用的本地 StartLux 端口")
    p.add_argument("--primary", default="fisher", help="loop 主模块")
    p.add_argument("--max-duration", type=float, default=3600.0, help="秒")
    p.add_argument("--dry-run", action="store_true", help="只感知+决策，不注入输入")
    p.add_argument("--mock-input", action="store_true", help="强制 mock 输入（连真实截图也不点击）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="输出运行/收益报告")
    p.add_argument("--hours", type=float, default=48.0)
    p.add_argument("--save", action="store_true")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
