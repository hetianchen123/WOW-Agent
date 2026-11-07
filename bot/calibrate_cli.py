"""Unattended screen calibration with a machine-readable result."""

from __future__ import annotations

import json
import stat
import sys
import time
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from yaml.nodes import MappingNode, ScalarNode

from .core.config import load_config
from .core.paths import CONFIG_DIR
from .perception.calibration import Location, locate_channel
from .perception.capture import CaptureError, ScreenCapture


def write_location(path: Path, original: str, location: Location) -> None:
    """Replace only the four calibration values and preserve existing YAML comments."""
    raw = yaml.safe_load(original)
    root = yaml.compose(original, Loader=yaml.SafeLoader)
    if not isinstance(raw, dict):
        raise ValueError("配置文件需要是 YAML 映射")
    changes = {
        "channel": {"x": location.x, "y": location.y, "calibrated": True},
        "capture": {"monitor": location.monitor},
    }
    expected = dict(raw)
    edits = []
    for section, values in changes.items():
        section_node = _field(root, section)
        for key, value in values.items():
            node = _field(section_node, key)
            if not isinstance(node, ScalarNode):
                raise ValueError(f"配置中的 {section}.{key} 需要是标量")
            literal = "true" if value is True else str(value)
            edits.append((node.start_mark.index, node.end_mark.index, literal))
        expected[section] = {**raw[section], **values}

    updated = original
    for start, end, literal in sorted(edits, reverse=True):
        updated = updated[:start] + literal + updated[end:]
    if yaml.safe_load(updated) != expected:
        raise ValueError("配置中存在共享值或别名，无法单独写入校准字段")
    with NamedTemporaryFile(dir=path.parent, prefix=".calibration-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(updated.encode("utf-8"))
            stream.close()
            temp.chmod(stat.S_IMODE(path.stat().st_mode))
            if path.read_bytes().decode("utf-8") != original:
                raise ValueError("校准期间配置已被修改，请重试")
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def _field(mapping, key):
    if isinstance(mapping, MappingNode):
        matches = [value for name, value in mapping.value
                   if isinstance(name, ScalarNode) and name.value == key]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(f"配置缺少唯一的 {key} 字段")


def _permissions() -> dict[str, bool | None]:
    if sys.platform != "darwin":
        return {"screen_recording": None, "accessibility": None}
    from ApplicationServices import AXIsProcessTrusted
    from Quartz import CGPreflightScreenCaptureAccess

    return {"screen_recording": bool(CGPreflightScreenCaptureAccess()),
            "accessibility": bool(AXIsProcessTrusted())}


def run(args) -> int:
    started = time.monotonic()
    path = CONFIG_DIR / "bot.yaml"
    result = {"config": str(path), "dry_run": args.dry_run}

    def finish(status, code=0, **details):
        print(json.dumps({**result, **details, "status": status,
                          "elapsed_seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False))
        return code

    if not 2 <= args.duration <= 120:
        return finish("invalid_duration", 2, message="--duration 需要在 2 到 120 秒之间")
    result.update(_permissions())
    if result["screen_recording"] is False:
        return finish("permission_required", 2,
                      message="在系统设置的隐私与安全性中，为启动此命令的 App 打开屏幕录制，然后重启该 App。")

    from mss.exception import ScreenShotError

    try:
        original = path.read_bytes().decode("utf-8")
        cfg = load_config(path)
        previous = Location(cfg.capture.monitor, cfg.channel.x, cfg.channel.y)
        if any(type(value) is not int for value in asdict(previous).values()):
            raise ValueError("坐标和显示器编号需要是整数")
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        return finish("config_error", 2, message=str(exc))
    result["previous"] = asdict(previous)
    try:
        capture = ScreenCapture(cfg.capture)
        monitors = capture.monitor_regions()
        result["displays"] = [{"monitor": i, "x": x, "y": y, "width": w, "height": h}
                              for i, (x, y, w, h) in enumerate(monitors, 1)]
        print(f"正在扫描 {len(monitors)} 台显示器，最长 {args.duration:g} 秒。请让游戏中的主色条保持可见。",
              file=sys.stderr, flush=True)
        confirmed = locate_channel(capture, monitors, args.duration)
    except (CaptureError, ScreenShotError, OSError) as exc:
        return finish("capture_error", 2, message=str(exc))
    except KeyboardInterrupt:
        return finish("interrupted", 130)

    if not confirmed:
        return finish("not_found", 2, message="未找到有活动心跳的主色条。请确认 WowAgent 已加载且 8 个色块可见。")
    if len(confirmed) != 1:
        return finish("ambiguous", 2, candidates=[asdict(location) for location in confirmed])
    (location, evidence), = confirmed.items()
    result.update(location=asdict(location), valid_frames=evidence.frames, message_types=sorted(evidence.kinds))
    if args.dry_run:
        return finish("verified")
    if previous == location and cfg.channel.calibrated:
        return finish("already_calibrated")
    try:
        write_location(path, original, location)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        return finish("config_error", 2, message=str(exc))
    return finish("calibrated")


def add_parser(sub) -> None:
    parser = sub.add_parser("calibrate-channel", help="扫描全部显示器并自动校准 WowAgent 主色条")
    parser.add_argument("--duration", type=float, default=30, help="等待上限，默认 30 秒")
    parser.add_argument("--dry-run", action="store_true", help="只验证，不写配置")
    parser.set_defaults(func=run)
