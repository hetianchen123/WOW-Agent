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


class HelperInput(InputBase):
    name = "helper"

    def __init__(self, key_jitter: bool = True, *, simulate: bool = False) -> None:
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
        try:
            if self._request("info").get("simulate") is not simulate:
                raise ValueError("键鼠执行器模式不符")
            self._request("ready")
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
            if reply.get("ok") is not True:
                raise OSError(str(reply.get("error", "键鼠执行器拒绝操作")))
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

    def _key_up_raw(self, key: str) -> None:
        self._request("up", key=key.strip().lower())

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
    backend = HelperInput(key_jitter=False, simulate=True)
    backend.down("w")
    backend.move_to(-1800, 500, jitter_px=0)
    backend.right_click()
    backend.rmb_turn(35)
    assert backend.position() == (-1765, 500)
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
    backend.release_all()
    assert backend._request("info")["events"] == 8
    backend.click_world_object()
    assert backend._request("info")["events"] == 10
    backend.click_ui()
    assert backend._request("info")["events"] == 12
    backend.down("w")
    try:
        backend.click_ui()
    except OSError:
        pass
    else:
        raise AssertionError("UI click accepted while moving")
    backend._request("simulate_blur")
    assert backend.interrupted()
    info = backend._request("info")
    assert info["events"] == 14 and not info["armed"]
    print("Signed controller, simulated input, window bounds, focus loss and key release passed")


if __name__ == "__main__":
    _smoke()
