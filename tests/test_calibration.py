import json
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from bot import calibrate_cli, cli
from bot.modules import quest
from bot.perception import calibration, channel_spec as wire
from bot.core.config import CaptureConfig
from bot.perception import capture as screen_capture
from bot.perception.capture import game_ui_bounds


def test_stock_minimap_dimensions_exclude_the_window_title_bar():
    minimap = SimpleNamespace(size=(120 / 1023, 213 / 1023))
    assert game_ui_bounds((79, 65, 1688, 977), minimap) == (79, 91, 1688, 951)
    assert game_ui_bounds((0, 0, 1688, 951), minimap) == (0, 0, 1688, 951)


def test_pinned_game_capture_uses_original_desktop_coordinates(monkeypatch):
    def game_window(*, require_focus):
        assert require_focus
        return {"kCGWindowNumber": 9, "kCGWindowBounds": {"X": 114, "Y": 51, "Width": 1692, "Height": 979}}

    calls = []

    def grab(x, y, w, h, *, window_id):
        calls.append((x, y, w, h, window_id))
        return np.zeros((h, w, 3), dtype=np.uint8)

    monkeypatch.setattr(screen_capture, "_game_window", game_window)
    monkeypatch.setattr(screen_capture.ScreenCapture, "_grab_mac_srgb", staticmethod(grab))
    screen = screen_capture.ScreenCapture(CaptureConfig())
    screen.bind_game_window(stream=False)
    assert screen.monitor_regions() == [(114, 51, 1692, 979)]
    assert screen.grab(154, 884, 142, 16).shape == (16, 142, 3)
    assert calls == [(154, 884, 142, 16, 9)]
    with pytest.raises(screen_capture.CaptureError, match="越界"):
        screen.grab(0, 0, 1920, 1080)
    assert len(calls) == 1


def test_pinned_game_capture_falls_back_on_unavailable_stream_and_rejects_moved_window(monkeypatch):
    bounds = {"X": -1000, "Y": 32, "Width": 950, "Height": 600}
    window = {"kCGWindowNumber": 23, "kCGWindowBounds": bounds}
    now = [0.]
    calls = []
    images = [None, np.full((16, 142, 3), 10, dtype=np.uint8)]

    def start(window_id, width, height):
        assert (window_id, width, height) == (23, 950, 600)

        def grab(x, y, w, h):
            calls.append((x, y, w, h))
            return images.pop(0)

        return SimpleNamespace(grab=grab, close=lambda: calls.append("closed"))

    def quartz(x, y, w, h, *, window_id):
        assert (x, y, window_id) == (-960, 552, 23)
        return np.zeros((h, w, 3), dtype=np.uint8)

    monkeypatch.setattr(screen_capture.sys, "platform", "darwin")
    monkeypatch.setattr(screen_capture.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(screen_capture, "_game_window", lambda **_: window)
    monkeypatch.setattr(screen_capture, "MacWindowStream", start)
    monkeypatch.setattr(screen_capture.ScreenCapture, "_grab_mac_srgb", staticmethod(quartz))

    screen = screen_capture.ScreenCapture(CaptureConfig())
    screen.bind_game_window()
    assert not screen.grab(-960, 552, 142, 16).any()
    assert (screen.grab(-960, 552, 142, 16) == 10).all()
    assert calls == [(40, 520, 142, 16)] * 2

    now[0] = .6
    bounds["Y"] += 1
    with pytest.raises(screen_capture.CaptureError, match="位置或尺寸"):
        screen.grab(-960, 552, 142, 16)
    screen.close()
    assert calls[-1] == "closed"


class Desktop:
    regions = [(0, 0, 340, 100), (-340, -45, 340, 100)]
    left_palette = {
        0: (0, 0, 0), 1: (0, 85, 11), 2: (0, 170, 33), 3: (27, 237, 49),
        6: (83, 171, 32), 7: (90, 236, 48), 8: (170, 0, 1), 9: (170, 85, 3),
        10: (169, 171, 27), 11: (173, 236, 45), 14: (255, 170, 15), 15: (255, 235, 39),
    }

    def __init__(self, *, two_live=False, frozen=False):
        self.now = 0.0
        self.two_live = two_live
        self.frozen = frozen

    def sleep(self, duration):
        self.now += duration

    def monitor_regions(self):
        return self.regions

    def grab(self, left, top, width, height):
        tick = int(self.now * wire.CHANNEL_FPS)
        kind = wire.schedule_kind(tick)
        if kind == wire.MSG_STATUS:
            beat = (tick % wire.CHANNEL_FPS) == 10
            value = wire.pack_status(wire.StatusFlags.HEARTBEAT if beat else wire.StatusFlags(0), 15)
        else:
            kind = wire.MSG_ZONE if kind is None else kind
            value = 123
        image = np.full((height, width, 3), (42, 49, 62), dtype=np.uint8)
        static = (wire.MSG_POS, 789)
        for x, y, frame in [(17, 23, (kind, value) if self.two_live else static),
                            (-301, -15, static if self.frozen else (kind, value)),
                            (-301, -31, (0xA, 70783))]:
            if left <= x <= left + width - wire.REGION_W and top <= y <= top + height - wire.REGION_H:
                nibbles = wire.encode_frame(*frame)
                local_x, local_y = x - left, y - top
                wire.render_frame(image, local_x, local_y, nibbles)
                if left < 0:
                    for i, nibble in enumerate(nibbles):
                        if nibble in self.left_palette:
                            bx = local_x + i * wire.STRIDE
                            image[local_y:local_y + wire.BLOCK, bx:bx + wire.BLOCK] = self.left_palette[nibble]
        return image


def test_reordered_monitors_verify_live_row_over_frozen_and_quest_rows(monkeypatch):
    screen = Desktop()
    monkeypatch.setattr(calibration, "time", SimpleNamespace(monotonic=lambda: screen.now, sleep=screen.sleep))
    found = calibration.locate_channel(screen, screen.regions, 5)
    assert list(found) == [calibration.Location(2, -301, -15)]
    assert next(iter(found.values())).verified


def test_frozen_desktop_never_calibrates(monkeypatch):
    screen = Desktop(frozen=True)
    monkeypatch.setattr(calibration, "time", SimpleNamespace(monotonic=lambda: screen.now, sleep=screen.sleep))
    assert calibration.locate_channel(screen, screen.regions, 3) == {}
    assert screen.now >= 3


def test_task_start_relocates_moved_channel(monkeypatch):
    screen = Desktop()
    monkeypatch.setattr(calibration, "time", SimpleNamespace(monotonic=lambda: screen.now, sleep=screen.sleep))
    old = calibration.Location(2, -309, -42)
    current = calibration.Location(2, -301, -15)
    assert calibration.startup_location(screen, old) == current
    monkeypatch.setattr(calibration, "locate_channel", lambda *_: pytest.fail("saved live row triggered full scan"))
    assert calibration.startup_location(screen, current) == current


def test_prepared_start_retries_if_channel_becomes_live_after_first_scan(monkeypatch):
    screen = Desktop(frozen=True)

    def sleep(duration):
        screen.sleep(duration)
        screen.frozen = screen.now < 7

    clock = SimpleNamespace(monotonic=lambda: screen.now, sleep=sleep)
    monkeypatch.setattr(calibration, "time", clock)
    monkeypatch.setattr(cli, "time", clock)
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: True)
    assert cli._prepared_channel_location(screen, calibration.Location(2, -309, -42), 20) == (
        calibration.Location(2, -301, -15)
    )


def test_managed_start_waits_in_background_until_cancelled(monkeypatch):
    screen = Desktop()
    clock = SimpleNamespace(monotonic=lambda: screen.now, sleep=screen.sleep)
    monkeypatch.setattr(cli, "time", clock)
    monkeypatch.setattr(quest, "game_is_frontmost", lambda: False)
    monkeypatch.setattr(calibration, "startup_location", lambda *_a, **_kw: pytest.fail("background capture"))
    assert cli._prepared_channel_location(screen, calibration.Location(2, -309, -42), None,
                                          cancelled=lambda: screen.now >= 121) is None
    assert screen.now >= 121
    assert screen.now >= 7


def test_cli_preserves_yaml_and_refuses_ambiguous_results(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bot.yaml"
    original = """# config comments
channel:
  x: 2018  # global position
  y: 982
  calibrated: false
capture: {monitor: 1, control_fps: 15}
extra: keep
"""
    path.write_text(original)
    monkeypatch.setattr(calibrate_cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(calibrate_cli, "_permissions",
                        lambda: {"screen_recording": True, "accessibility": False})

    def run(screen, dry_run=False):
        monkeypatch.setattr(calibration, "time", SimpleNamespace(monotonic=lambda: screen.now, sleep=screen.sleep))
        monkeypatch.setattr(calibrate_cli, "ScreenCapture", lambda _: screen)
        code = calibrate_cli.run(SimpleNamespace(duration=5, dry_run=dry_run))
        return code, json.loads(capsys.readouterr().out)

    code, result = run(Desktop(two_live=True))
    assert code == 2 and result["status"] == "ambiguous"
    assert path.read_text() == original
    code, result = run(Desktop(), dry_run=True)
    assert code == 0 and result["status"] == "verified"
    assert path.read_text() == original
    code, result = run(Desktop())
    assert code == 0 and result["status"] == "calibrated"
    assert result["location"] == {"monitor": 2, "x": -301, "y": -15}
    assert [display["x"] for display in result["displays"]] == [0, -340]
    updated = path.read_text()
    assert "# config comments" in updated and "# global position" in updated
    assert yaml.safe_load(updated) == {
        "channel": {"x": -301, "y": -15, "calibrated": True},
        "capture": {"monitor": 2, "control_fps": 15},
        "extra": "keep",
    }
    code, result = run(Desktop())
    assert code == 0 and result["status"] == "already_calibrated"
    assert path.read_text() == updated


def test_cli_reports_missing_permission_without_capturing(monkeypatch, capsys):
    monkeypatch.setattr(calibrate_cli, "_permissions",
                        lambda: {"screen_recording": False, "accessibility": False})

    def fail_if_called(*args):
        raise AssertionError("capture started without permission")

    monkeypatch.setattr(calibrate_cli, "ScreenCapture", fail_if_called)
    assert calibrate_cli.run(SimpleNamespace(duration=30, dry_run=False)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "permission_required"
