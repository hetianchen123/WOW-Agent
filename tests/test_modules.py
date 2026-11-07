import time

import pytest

from bot.core.statemachine import Machine, State


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
