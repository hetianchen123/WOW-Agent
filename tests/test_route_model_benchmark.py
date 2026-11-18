"""The benchmark must preserve failures and never label image plans as verified routes."""
import json

import pytest
from PIL import Image
from experiments.route_models.compare import overlay, parse


def plan(**updates):
    data = {'player_feet': [500, 750], 'goal_feet': [300, 750],
            'obstacles': [], 'path': [[500, 750], [500, 900], [300, 900], [300, 750]],
            'uncertain_segments': [2]}
    data.update(updates)
    return data


def test_reasoning_and_fenced_json_remain_parseable():
    result = parse('<think>reasoning, not a route</think>\n```json\n' + json.dumps(plan()) + '\n```')
    assert result['uncertain_segments'] == [2]
    assert 'world_route_verified' not in result


@pytest.mark.parametrize('updates', [
    {'path': [[500, 750], [500, 750]]},
    {'path': [[-1, 500], [500, 700]]},
    {'path': [[True, 500], [500, 700]]},
    {'player_feet': [float('nan'), 500]},
    {'uncertain_segments': [3]},
    {'obstacles': [{'kind': 'wood', 'polygon': [100, 100, 200, 100, 200, 200]}]},
])
def test_invalid_coordinates_and_actual_flat_polygon_failure_are_not_silently_repaired(updates):
    with pytest.raises(ValueError):
        parse(json.dumps(plan(**updates)))


def test_unknown_route_remains_unknown():
    result = parse(json.dumps(plan(path=[], player_feet=None, uncertain_segments=[])))
    assert result['path'] == []
    assert result['player_feet'] is None


def test_overlay_uses_image_size_and_marks_uncertain_segment(tmp_path):
    source, dest = tmp_path/'source.png', tmp_path/'overlay.png'
    Image.new('RGB', (1000, 500), 'black').save(source)
    overlay(source, plan(), dest)
    rendered = Image.open(dest)
    assert rendered.getpixel((400, 450)) == (0, 255, 255)
    assert rendered.getpixel((300, 420)) == (255, 165, 0)
