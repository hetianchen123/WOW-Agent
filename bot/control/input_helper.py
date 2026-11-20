"""Input backend for the socket handed off by the native controller app."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

from .input_base import InputBase


def in_controller_app() -> bool:
    return os.environ.get("WOWAGENT_APP_MODE") == "1"


def has_helper_socket() -> bool:
    return in_controller_app() and os.environ.get("WOWAGENT_INPUT_SOCKET") == "1"


class HelperSessionStopped(OSError):
    """The input helper has already disarmed and released its held keys."""


class HelperActionRejected(OSError):
    """The connected helper refused a particular input request."""


class HelperForegroundPaused(OSError):
    """The managed session is alive, with input gated until fresh telemetry."""


class HelperInput(InputBase):
    name = "helper"

    def __init__(self, key_jitter: bool = True, *, simulate: bool = False, keep_online: bool = False) -> None:
        super().__init__(key_jitter)
        if not has_helper_socket():
            raise ValueError("请先启动 WowAgent Keyboard Mouse.app，然后从主 App 启动命令")
        fd = os.dup(0)
        self._socket = socket.socket(fileno=fd)
        # Keep the capability out of subprocesses such as caffeinate.
        with open(os.devnull, "rb") as null:
            os.dup2(null.fileno(), 0)
        self._socket.settimeout(2)
        self._reader = self._socket.makefile("rb")
        self._lock = threading.Lock()
        self._next_check = 0.0
        self._failed = False
        self.focus_epoch = None
        try:
            info = self._request("info")
            if info.get("simulate") is not simulate:
                raise ValueError("键鼠执行器模式不符")
            self.supports_navigation_pulse = info.get("navigationPulse") is True
            self._request("ready", keep_online=keep_online)
            self.supports_focus_pause = keep_online and self.focus_epoch is not None
        except (OSError, ValueError):
            self._reader.close()
            self._socket.close()
            raise

    def _request(self, op: str, **fields) -> dict:
        with self._lock:
            if self._failed:
                raise OSError("键鼠连接已停止")
            try:
                packet = json.dumps({"op": op, **fields}).encode() + b"\n"
                self._socket.sendall(packet)
                line = self._reader.readline(513)
                if not line.endswith(b"\n") or len(line) > 512:
                    raise OSError("键鼠连接中断或返回无效")
                reply = json.loads(line)
                if not isinstance(reply, dict):
                    raise OSError("键鼠执行器返回无效")
            except (ValueError, OSError):
                self._failed = True
                raise
            if type(reply.get("focus_epoch")) is int:
                self.focus_epoch = reply["focus_epoch"]
                self._held_keys.clear()
                self._held_buttons.clear()
                if reply.get("ok") is not True:
                    raise HelperForegroundPaused(str(reply.get("error")))
            if reply.get("ok") is not True:
                message = str(reply.get("error", "键鼠执行器拒绝操作"))
                if message in {"键鼠会话已停止", "魔兽窗口不在前台"}:
                    raise HelperSessionStopped(message)
                raise HelperActionRejected(message)
            return reply

    def interrupted(self) -> bool:
        now = time.monotonic()
        if self._failed:
            return True
        if now >= self._next_check:
            self._next_check = now + .20
            try:
                self._request("heartbeat")
            except (OSError, ValueError):
                return True
        return False

    def _key_down_raw(self, key: str) -> None:
        self._request("down", key=key.strip().lower())

    def resume_foreground(self, epoch: int) -> None:
        self._request("resume_foreground", focus_epoch=epoch)
        self.focus_epoch = None

    def _key_up_raw(self, key: str) -> None:
        self._request("up", key=key.strip().lower())

    def start_navigation_pulse(self, key: str, hold_s: float) -> bool:
        """Schedule one measured movement key release on the native helper."""
        if not self.supports_navigation_pulse:
            return False
        if key not in {"w", "s", "q", "e", "left", "right"} or not 0 < hold_s <= .12:
            raise ValueError("precise_pulse_duration_invalid")
        self._request("navigation_pulse", key=key, hold_us=max(1, round(hold_s * 1_000_000)))
        return True

    def cancel_navigation_pulse(self) -> None:
        if self.supports_navigation_pulse:
            self._request("cancel_navigation_pulse")

    def _move_raw(self, x: int, y: int) -> None:
        self._request("move", x=x, y=y)

    def _mouse_down_raw(self, button: str) -> None:
        self._request("mouse_down", button=button)

    def _mouse_up_raw(self, button: str) -> None:
        self._request("mouse_up", button=button)

    def click_world_object(self) -> None:
        self._request("world_click")

    def click_ui(self) -> None:
        self._request("ui_click")

    def publish_run_status(self, state: int) -> bool:
        try:
            self._request("run_status_v2", state=state, wire=2)
        except OSError:
            return False
        return True

    def request_quest_preparation(self) -> bool:
        try:
            self._request("prepare_quest")
        except HelperActionRejected:
            return False
        return True

    def position(self) -> tuple[int, int]:
        reply = self._request("position")
        return int(reply["x"]), int(reply["y"])

    def scroll(self, dy: int) -> None:
        self._request("scroll", dy=dy)

    def release_all(self) -> None:
        try:
            self._request("release")
        except (OSError, ValueError):
            pass
        finally:
            self._held_keys.clear()
            self._held_buttons.clear()


def _smoke() -> None:
    backend = HelperInput(key_jitter=False, simulate=True, keep_online=True)
    assert backend.supports_focus_pause and backend.focus_epoch is not None
    backend.resume_foreground(backend.focus_epoch)
    backend.down("w")
    assert backend.start_navigation_pulse("left", .12)
    events = backend._request("info")["events"]
    backend._request("simulate_blur")
    heartbeat = backend._request("heartbeat")
    assert not backend.interrupted() and heartbeat["focus_epoch"] == backend.focus_epoch
    assert backend._request("info")["events"] == events + 2
    blurred_epoch = backend.focus_epoch
    backend._request("simulate_focus")
    try:
        backend.resume_foreground(blurred_epoch)
    except HelperForegroundPaused:
        pass
    else:
        raise AssertionError("resume accepted with the old foreground epoch")
    try:
        backend.down("w")
    except HelperForegroundPaused:
        pass
    else:
        raise AssertionError("movement accepted before fresh foreground confirmation")
    backend.resume_foreground(backend.focus_epoch)
    backend.down("w")
    backend._request("simulate_stop")
    backend._next_check = 0
    assert backend.interrupted()
    assert backend._request("info")["events"] == events + 4
    backend.release_all()
    backend._request("simulate_arm")
    backend._request("ready")
    backend._next_check = 0
    assert not backend.interrupted()
    print("Managed session paused, released keys, resumed with new foreground epoch and honored stop")
    events = backend._request("info")["events"]
    assert backend.request_quest_preparation()
    backend._request("simulate_blur")
    assert not backend.request_quest_preparation()
    assert backend._request("heartbeat")["ok"]
    backend._request("simulate_focus")
    assert backend._request("info")["events"] == events + 2
    events += 2
    assert backend.start_navigation_pulse("left", .12)
    backend.cancel_navigation_pulse()
    assert backend._request("info")["events"] == events + 2
    assert backend.start_navigation_pulse("right", .035)
    deadline = time.monotonic() + 1
    while backend._request("info")["events"] < events + 4 and time.monotonic() < deadline:
        time.sleep(.01)
    assert backend._request("info")["events"] == events + 4
    assert backend.publish_run_status(3)
    assert not backend.publish_run_status(10)
    assert backend.publish_run_status(9)
    assert backend.request_quest_preparation()
    assert backend._request("info")["events"] == events + 12
    backend.down("w")
    assert not backend.request_quest_preparation()
    backend.move_to(-1800, 500, jitter_px=0)
    backend.right_click()
    backend.rmb_turn(35)
    assert backend.position() == (-1765, 500)
    events = backend._request("info")["events"]
    try:
        backend.move_to(3000, 500, jitter_px=0)
    except OSError:
        pass
    else:
        raise AssertionError("outside-window move accepted")
    try:
        backend.click_world_object()
    except OSError:
        pass
    else:
        raise AssertionError("world interaction accepted while moving")
    assert backend._request("info")["events"] == events
    backend.release_all()
    assert backend._request("info")["events"] == events + 1
    backend.click_world_object()
    assert backend._request("info")["events"] == events + 3
    backend.click_ui()
    assert backend._request("info")["events"] == events + 5
    backend.down("w")
    try:
        backend.click_ui()
    except OSError:
        pass
    else:
        raise AssertionError("UI click accepted while moving")
    assert backend.start_navigation_pulse("left", .12)
    backend._request("simulate_blur")
    backend._next_check = 0
    assert backend.interrupted()
    info = backend._request("info")
    assert info["events"] == events + 9 and not info["armed"]
    time.sleep(.15)
    assert backend._request("info")["events"] == events + 9
    print("Signed controller, simulated input, window bounds, focus loss and key release passed")


if __name__ == "__main__":
    _smoke()
