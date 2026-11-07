import io
import json
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from bot.decision.startlux import LocalStartLux, QuestPlanner


def response(**updates):
    answer = {
        "choice": "pending", "confidence": .9, "none_probability": .01,
        "probabilities": {"pending": .99, "wait": .01},
        **updates,
    }
    return io.StringIO(json.dumps({"model": "StartLux-Decision-test", "answers": {"action": answer}}))


def test_local_client_posts_only_to_loopback_and_rejects_stale_state():
    client = LocalStartLux()
    requests = []

    def send(request, timeout):
        requests.append(request)
        assert request.full_url == "http://127.0.0.1:8090/v1/systemone"
        assert json.loads(request.data)["questions"]["action"]["criteria"] == {"pending": "接任务", "wait": "等待"}
        return response()

    client.opener = SimpleNamespace(open=send)
    result = client.decide({"observation_age_ms": 100}, {"pending": "接任务", "wait": "等待"})
    assert result["recommendation"] == "pending"
    assert client.decide({"observation_age_ms": 3000}, {})["reason"] == "stale_state"
    assert len(requests) == 1


@pytest.mark.parametrize("updates, reason", [
    ({"confidence": .4}, "low_confidence"),
    ({"none_probability": .3}, "no_option_fits"),
])
def test_local_client_abstains_on_uncertain_result(updates, reason):
    client = LocalStartLux()
    client.opener = SimpleNamespace(open=lambda *_, **__: response(**updates))
    result = client.decide({}, {"pending": "接任务", "wait": "等待"})
    assert result["recommendation"] is None and result["reason"] == reason


@pytest.mark.parametrize("updates", [{"choice": "invented"}, {"confidence": float("nan")}])
def test_local_client_rejects_invalid_result(updates):
    client = LocalStartLux()
    client.opener = SimpleNamespace(open=lambda *_, **__: response(**updates))
    with pytest.raises(ValueError):
        client.decide({}, {"pending": "接任务", "wait": "等待"})


def test_planner_discards_changed_phase_and_expired_response(monkeypatch):
    from bot.decision import startlux

    monkeypatch.setattr(startlux.time, "monotonic", lambda: 10)
    planner = QuestPlanner(LocalStartLux())
    try:
        planner.future = Future()
        planner.signature, planner.started = (1, "pending"), 9
        planner.future.set_result({"recommendation": "pending"})
        assert planner.poll((1, "active"), {}, {}) is None
        planner.future = Future()
        planner.signature, planner.started = (1, "pending"), 0
        planner.future.set_result({"recommendation": "pending"})
        result = planner.poll((1, "pending"), {}, {})
        assert result["recommendation"] is None and result["reason"] == "stale_decision"
    finally:
        planner.close()


def test_planner_retries_timeout_with_fresh_state_and_accepts_slow_response(monkeypatch):
    from bot.decision import startlux

    now = [10.0]
    monkeypatch.setattr(startlux.time, "monotonic", lambda: now[0])
    planner = QuestPlanner(LocalStartLux())
    signature = (90882, "READY", "CLOSED")
    requests = []

    def submit(_, state, criteria):
        future = Future()
        requests.append((state, future))
        return future

    try:
        monkeypatch.setattr(planner.pool, "submit", submit)
        assert planner.poll(signature, {"observation_age_ms": 200}, {}) is None
        requests[0][1].set_exception(TimeoutError())
        now[0] = 18
        assert planner.poll(signature, {"observation_age_ms": 50}, {}) is None
        assert planner.poll(signature, {"observation_age_ms": 25}, {}) is None
        assert requests[1][0]["observation_age_ms"] == 25
        now[0] = 22
        requests[1][1].set_result({"recommendation": "turn_in"})
        assert planner.poll(signature, {"observation_age_ms": 50}, {})["recommendation"] == "turn_in"
    finally:
        planner.close()
