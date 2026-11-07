"""Input factory + convenience wrapper used by modules."""
from __future__ import annotations

import logging
import platform
from typing import Optional

from ..core.config import InputConfig
from .input_base import InputBase
from .input_mock import MockInput

log = logging.getLogger(__name__)


def create_input(cfg: InputConfig, force_mock: bool = False) -> InputBase:
    if force_mock or cfg.backend == "mock":
        return MockInput()
    from .input_helper import HelperInput, in_controller_app

    if in_controller_app():
        return HelperInput(cfg.key_jitter)
    backend = cfg.backend
    if backend == "auto":
        backend = "mac" if platform.system() == "Darwin" else "win"
    if backend == "mac":
        from .input_mac import MacInput

        return MacInput(cfg.key_jitter)
    if backend == "win":
        from .input_win import WinInput

        return WinInput(cfg.key_jitter)
    raise ValueError(f"unknown input backend: {cfg.backend}")
