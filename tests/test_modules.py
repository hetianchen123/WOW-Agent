import time
from types import SimpleNamespace

import pytest

from bot.control.input_helper import HelperSessionStopped
from bot.core.statemachine import Machine, State
from bot.modules.base import Module


def test_helper_disarm_between_focus_check_and_key_release_is_a_clean_stop():
    events, releases = [], []

    class ReleaseKey(State):
        name = "ReleaseKey"

        def tick(self, ctx):
            raise HelperSessionStopped("键鼠会话已停止")

    class TravelModule(Module):
        initial_state = "ReleaseKey"

        def build_states(self):
            self.machine.add(ReleaseKey())

    ctx = SimpleNamespace(
        input=SimpleNamespace(release_all=lambda: releases.append(True)),
        estop=SimpleNamespace(check=lambda: False),
        log_event=lambda name, data: events.append((name, data)),
        begin_tick=lambda: None,
        pump_channel=lambda: None,
        stats={},
    )
    outcome = TravelModule(ctx).run(1)
    assert outcome.status == "stopped:input_session_stopped"
    assert releases == [True]
    assert [name for name, _ in events] == ["module_start", "input_session_stopped", "module_end"]


class Counting(State):
    name = "counting"

    def __init__(self):
        self.ticks = 0

    def on_enter(self, ctx):
        ctx["entered"] = ctx.get("entered", 0) + 1

    def tick(self, ctx):
        self.ticks += 1
        ctx["ticks"] = self.ticks
        if self.ticks >= 3:
            return "end"
        return None


class End(State):
    name = "end"

    def tick(self, ctx):
        ctx["done"] = True
        return None


class Slow(State):
    name = "slow"
    timeout_s = 1.0
    timeout_next = "end"

    def tick(self, ctx):
        return None


def test_transitions_and_enter():
    ctx = {}
    m = Machine(ctx)
    m.add(Counting())
    m.add(End())
    m.start("counting")
    for _ in range(5):
        m.tick(0.1)
    assert ctx["ticks"] == 3
    assert ctx["entered"] == 1
    assert m.state_name == "end"
    assert ctx.get("done") is True


def test_timeout_transition():
    ctx = {}
    m = Machine(ctx)
    m.add(Slow())
    m.add(End())
    m.start("slow")
    m.tick(0.5)
    assert m.state_name == "slow"
    m.tick(0.6)
    assert m.state_name == "end"


def test_unknown_state_raises():
    m = Machine({})
    m.add(End())
    m.start("end")
    with pytest.raises(KeyError):
        m.transition_to("nope")
