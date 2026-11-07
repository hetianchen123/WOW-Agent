"""Typed configuration with YAML load/merge and default file generation.

All tunables live in config/bot.yaml (main), config/items.yaml (tracked item
whitelist) and config/routes/*.yaml (navigation routes). Sensible defaults
exist for everything so the bot can run in dry-run/selftest before on-screen
calibration.
"""
from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .paths import CONFIG_DIR, ROOT

Rect = Tuple[int, int, int, int]  # x, y, w, h


# ---------------------------------------------------------------- channel ----
@dataclass
class ChannelConfig:
    # Top-left corner of the addon pixel-block region, in screenshot pixel
    # coordinates. Calibrate with `python -m bot.cli calibrate-channel`.
    x: int = 40
    y: int = -1  # -1 = uncalibrated (bottom strip default replaced on calibrate)
    calibrated: bool = False


# ---------------------------------------------------------------- capture ----
@dataclass
class CaptureConfig:
    monitor: int = 1  # mss monitor index (1 = primary)
    control_fps: int = 15  # main control loop frequency


# ----------------------------------------------------------------- audio ----
@dataclass
class AudioConfig:
    enabled: bool = True
    # sounddevice device index; None = system default output (loopback needs a
    # virtual device on macOS, e.g. BlackHole; on Windows use WASAPI loopback).
    device: Optional[int] = None
    sample_rate: int = 48000
    block_ms: int = 50
    band_low_hz: float = 700.0
    band_high_hz: float = 5000.0
    bite_threshold: float = 6.0  # band-energy ratio vs rolling baseline
    refractory_s: float = 0.6


# ----------------------------------------------------------------- input ----
@dataclass
class InputConfig:
    backend: str = "auto"  # auto | mac | win | mock
    mouse_turn_gain: float = 900.0  # pixels of mouse travel per radian of error
    mouse_turn_max_px: int = 350
    turn_deadband_deg: float = 4.0
    invert_turn: bool = False  # flip mouse-turn direction (calibrate in-game)
    key_jitter: bool = True
    # pynput global hotkey syntax; triggers instant stop + release-all.
    estop_hotkey: str = "<ctrl>+<alt>+<f12>"
    prevent_sleep: bool = True  # spawn caffeinate (macOS) while running


# -------------------------------------------------------------- navigation ----
@dataclass
class NavigationConfig:
    # Facing convention of GetPlayerFacing()/channel HDG:
    #   ccw_west: 0=N, pi/2=W (WoW standard, counterclockwise)  [default]
    #   cw_east : 0=N, pi/2=E  (flip if PoC shows mirrored steering)
    facing_convention: str = "ccw_west"
    arrival_radius_frac: float = 0.4  # x median segment length when route omits one
    stuck_window_s: float = 6.0
    stuck_displacement: float = 0.0008  # map-fraction units
    stuck_max_events: int = 5


# ---------------------------------------------------------------- fishing ----
@dataclass
class FishingConfig:
    key_cast: str = "1"
    key_interact: str = "f"
    spell_ids: List[int] = field(default_factory=lambda: [131296, 7620])  # fishing cast
    cast_confirm_timeout_s: float = 3.0
    bite_window_min_s: float = 3.0
    bite_window_max_s: float = 30.0
    bite_react_s: float = 1.6
    max_cast_retries: int = 3
    loot_confirm_timeout_s: float = 3.0
    use_audio_bite: bool = True
    use_vision_bite: bool = True
    bobber_roi: Rect = (0, 0, 0, 0)  # water area where bobber lands
    bobber_diff_threshold: float = 14.0  # mean abs-diff that counts as a splash
    min_free_slots: int = 2  # fewer free slots -> handoff to seller


# --------------------------------------------------------------- gathering ----
@dataclass
class GatheringConfig:
    minimap_roi: Rect = (0, 0, 0, 0)
    # north-up locked minimap assumed; yards-per-px at your locked zoom:
    yards_per_px: float = 0.55
    node_templates: List[str] = field(default_factory=lambda: ["node_herb", "node_ore", "node_pool"])
    template_threshold: float = 0.80
    scan_interval_s: float = 0.4
    interact_range_yards: float = 9.0
    # node screen position ~= screen center + this offset when aligned & close
    node_click_offset: Tuple[int, int] = (0, 40)
    key_descend: str = "x"
    descend_before_interact: bool = True


# ---------------------------------------------------------------- selling ----
@dataclass
class SellingConfig:
    key_hearthstone: str = "2"
    key_interact: str = "f"
    hearth_timeout_s: float = 45.0
    hub_radius_map_units: float = 0.02  # "we are at the hub" tolerance
    max_posts_per_run: int = 150
    post_confirm_timeout_s: float = 5.0
    post_max_consecutive_failures: int = 3
    mail_max_loops: int = 30
    # template names resolved under templates/<version>/
    templates: Dict[str, str] = field(default_factory=lambda: {
        "ah_open": "ah_window",
        "post_button": "post_button",
        "mail_frame": "mail_frame",
        "open_all_mail": "open_all_mail",
    })
    route_to_ah: str = "hub_to_ah"      # route file name in config/routes/
    route_to_mail: str = "ah_to_mail"
    route_return: str = "hub_to_spot"


# ------------------------------------------------------------------ rhythm ----
@dataclass
class RhythmConfig:
    work_min_s: float = 90 * 60
    work_max_s: float = 150 * 60
    rest_min_s: float = 10 * 60
    rest_max_s: float = 30 * 60
    # local-time maintenance windows (week abbreviation is informational)
    maintenance_windows: List[List[str]] = field(default_factory=lambda: [["Thu 05:00", "Thu 09:00"]])


# ------------------------------------------------------------------ notify ----
@dataclass
class NotifyConfig:
    provider: str = "none"  # none | bark | serverchan
    key: str = ""
    timeout_s: float = 5.0


# ------------------------------------------------------------------ global ----
@dataclass
class BotConfig:
    game_version: str = "12.x"
    dry_run: bool = False  # perception+decisions only, no input injection
    log_level: str = "INFO"
    game_process_names: List[str] = field(default_factory=lambda: ["World of Warcraft"])
    channel: ChannelConfig = field(default_factory=ChannelConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    input: InputConfig = field(default_factory=InputConfig)
    navigation: NavigationConfig = field(default_factory=NavigationConfig)
    fishing: FishingConfig = field(default_factory=FishingConfig)
    gathering: GatheringConfig = field(default_factory=GatheringConfig)
    selling: SellingConfig = field(default_factory=SellingConfig)
    rhythm: RhythmConfig = field(default_factory=RhythmConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def template_dir(self) -> Path:
        return ROOT / "templates" / self.game_version


# ------------------------------------------------------------- load / save ----
def _coerce(rect: Any) -> Rect:
    if isinstance(rect, (list, tuple)):
        return (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    return rect


def _apply(obj: Any, data: Dict[str, Any]) -> None:
    """Recursively apply dict values onto a dataclass instance."""
    for f in fields(obj):
        if f.name not in data:
            continue
        val = data[f.name]
        cur = getattr(obj, f.name)
        if is_dataclass(cur) and isinstance(val, dict):
            _apply(cur, val)
        elif f.name in ("bobber_roi", "minimap_roi"):
            setattr(obj, f.name, _coerce(val))
        elif getattr(f, "metadata", {}).get("rect"):
            setattr(obj, f.name, _coerce(val))
        else:
            if isinstance(cur, tuple) and isinstance(val, list):
                val = tuple(val)
            setattr(obj, f.name, val)


def load_config(path: Path | None = None) -> BotConfig:
    cfg = BotConfig()
    path = path or (CONFIG_DIR / "bot.yaml")
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # accept a flat file with optional top-level sections
        _apply(cfg, data)
    return cfg


def save_config(cfg: BotConfig, path: Path | None = None) -> None:
    path = path or (CONFIG_DIR / "bot.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dataclasses.asdict(cfg)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_items(path: Path | None = None) -> Dict[int, dict]:
    """items.yaml: {itemID: {name, reserve}} — whitelist tracked + posted."""
    path = path or (CONFIG_DIR / "items.yaml")
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: Dict[int, dict] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = v if isinstance(v, dict) else {"name": str(v), "reserve": 0}
        except (TypeError, ValueError):
            continue
    return out


def load_route(name: str, routes_dir: Path | None = None) -> dict:
    routes_dir = routes_dir or (CONFIG_DIR / "routes")
    p = routes_dir / f"{name}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"route file not found: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
