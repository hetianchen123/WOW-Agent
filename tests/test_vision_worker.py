import json
import sys
from pathlib import Path

import pytest

from bot.perception.vision_worker import VisionWorker
from experiments.qwen3_vl.run import parse_near_observation


@pytest.fixture
def worker(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text('''import sys, json, os, time
print(json.dumps({"ready": True, "protocol": 1}), flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if r["target"] == "slow": time.sleep(10)
 print(json.dumps({"id": "wrong" if r["target"] == "wrong" else r["id"], "cases": [{"pid": os.getpid()}]}), flush=True)
''')
    client = VisionWorker([sys.executable, str(script)], tmp_path / "stderr.log")
    yield client
    client.close()


def test_worker_reuses_model_process_and_closes_owned_child(worker):
    first = worker.inspect([Path("a")])
    assert worker.inspect([Path("b")]) == first
    process = worker.process
    worker.close()
    assert process.poll() is not None
    with pytest.raises(ValueError, match="closed"):
        worker.inspect([])


def test_cancel_discards_inflight_response_and_next_request_starts_clean(worker):
    worker.inspect([])
    old = worker.process
    checks = 0
    def guard():
        nonlocal checks
        checks += 1
        if checks > 3:
            raise ValueError("cancelled")
    with pytest.raises(ValueError, match="cancelled"):
        worker.inspect([], target="slow", guard=guard)
    assert old.poll() is not None and worker.process is None
    assert worker.inspect([])[0]["pid"] != old.pid


def test_wrong_receipt_and_timeout_terminate_worker(worker):
    with pytest.raises(ValueError, match="another request"):
        worker.inspect([], target="wrong")
    assert worker.process is None
    with pytest.raises(ValueError, match="超时"):
        worker.inspect([], target="slow", timeout=.2)
    assert worker.process is None


def near(**changes):
    value = dict(schema=2, feet=[500, 800], front="open", left="unknown", right="unknown",
                 next_view="front", feature="open_ground", landmark=[500, 700], evidence="near grass")
    return json.dumps(value | changes)


def test_no_player_origin_is_unknown_even_when_model_says_blocked():
    value = json.loads(near())
    value.update(feet=None, front="blocked", left="blocked", right="blocked", feature="rock_or_wall")
    observed = parse_near_observation(json.dumps(value))
    assert (observed["front"], observed["left"], observed["right"]) == ("unknown",) * 3
    assert "landmark" not in observed


@pytest.mark.parametrize("endpoint", [None, [500, 800], [500, 900], [800, 700]])
def test_near_front_requires_endpoint_ahead_of_identified_feet(endpoint):
    value = json.loads(near())
    value["landmark"] = endpoint
    assert parse_near_observation(json.dumps(value))["front"] == "unknown"


def test_valid_near_corridor_keeps_origin_and_short_scope():
    value = parse_near_observation(near())
    assert value["front"] == "open" and value["scope"] == "one_short_step"
    assert value["feet"] == [500, 800]


@pytest.mark.parametrize('view,player,ground,usable', [
    ('normal', True, True, True), ('normal', True, False, False),
    ('normal', False, True, True), ('low_angle', True, True, False),
    ('occluded', True, True, False), ('unknown', True, True, False),
])
def test_composition_visibility_is_not_path_authorization(view, player, ground, usable):
    from experiments.qwen3_vl.run import parse_composition
    value = parse_composition(json.dumps({'view': view, 'player_visible': player,
        'near_ground_visible': ground, 'evidence': 'visible scene'}))
    assert value['usable'] is usable
    assert 'front' not in value and 'landmark' not in value


def test_composition_rejects_non_boolean_or_injected_extra_fields():
    from experiments.qwen3_vl.run import parse_composition
    value = {'view': 'normal', 'player_visible': 'true', 'near_ground_visible': True, 'evidence': 'ground'}
    with pytest.raises(ValueError): parse_composition(json.dumps(value))
    value.update(player_visible=True, move='forward')
    with pytest.raises(ValueError): parse_composition(json.dumps(value))
