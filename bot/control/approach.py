"""Bounded approach actions. Sending movement is never an arrival receipt."""
from dataclasses import dataclass
from enum import Enum
import math


class ActionState(str, Enum):
    ISSUED = "issued"
    WAITING = "waiting"
    CONFIRMED = "confirmed"
    RECOVERABLE = "recoverable"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ActionResult:
    state: ActionState
    reason: str
    hold_forward: bool = False
    moved_yards: float | None = None


@dataclass(frozen=True)
class ApproachGoal:
    kind: str
    identity: tuple
    pulse_s: float = .24
    settle_s: float = .55
    feedback_timeout_s: float = 1.5
    max_displacement_yards: float = 8.0  # Generous guard; not an assumed walk speed.


@dataclass(frozen=True)
class ApproachObservation:
    identity: tuple
    sequence: int
    in_range: bool | None
    safe: bool
    position: tuple[float, float] | None = None
    world_size: tuple[float, float] | None = None


class ApproachAttempt:
    """One pulse followed by a *new* sample; caller owns retries and routing.

    Continuous-key users must apply hold_forward on EVERY poll. Tap users
    only send their initial bounded pulse. Both share the same receipt rules.
    """

    def __init__(self, goal: ApproachGoal, now: float, observation: ApproachObservation):
        self.goal, self.started, self.origin = goal, now, observation
        self.result: ActionResult | None = None

    def poll(self, now: float, observation: ApproachObservation) -> ActionResult:
        if self.result is not None:
            return self.result
        elapsed = now - self.started
        if not math.isfinite(elapsed) or elapsed < 0:
            self.result = ActionResult(ActionState.CANCELLED, "clock_changed")
        elif observation.identity != self.goal.identity:
            self.result = ActionResult(ActionState.CANCELLED, "target_changed")
        elif not observation.safe:
            self.result = ActionResult(ActionState.CANCELLED, "unsafe_state")
        elif observation.sequence == self.origin.sequence:
            if elapsed >= self.goal.feedback_timeout_s:
                self.result = ActionResult(ActionState.RECOVERABLE, "feedback_stale")
            else:
                return ActionResult(ActionState.WAITING, "fresh_feedback_required",
                                    elapsed < self.goal.pulse_s)
        elif observation.in_range is True:
            self.result = ActionResult(ActionState.CONFIRMED, "range_verified")
        elif elapsed < self.goal.settle_s:
            return ActionResult(ActionState.WAITING, "movement_settling", elapsed < self.goal.pulse_s)
        else:
            moved = None
            def valid_pair(value):
                return (isinstance(value, (tuple, list)) and len(value) == 2
                        and all(type(x) in (int, float) and math.isfinite(x) for x in value))
            if (valid_pair(self.origin.position) and valid_pair(observation.position)
                    and valid_pair(observation.world_size) and min(observation.world_size) > 0
                    and self.origin.world_size == observation.world_size):
                moved = math.hypot(*((end - start) * size for start, end, size in zip(
                    self.origin.position, observation.position, observation.world_size)))
            if moved is None:
                self.result = ActionResult(ActionState.RECOVERABLE, "movement_evidence_missing")
            elif moved > self.goal.max_displacement_yards:
                self.result = ActionResult(ActionState.RECOVERABLE, "displacement_unexpected", moved_yards=moved)
            elif moved < .15:
                self.result = ActionResult(ActionState.RECOVERABLE, "no_displacement", moved_yards=moved)
            else:
                # Displacement alone never confirms arrival or task completion.
                self.result = ActionResult(ActionState.CONFIRMED, "movement_observed", moved_yards=moved)
        return self.result
