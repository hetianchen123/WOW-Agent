import numpy as np
import cv2

from bot.perception import cv as cvmod


def _dot_template():
    """Node icon with a border — uniform templates are degenerate for NCC."""
    t = np.full((20, 20, 3), (255, 180, 40), dtype=np.uint8)
    t[0, :] = (120, 80, 10)
    t[-1, :] = (120, 80, 10)
    t[:, 0] = (120, 80, 10)
    t[:, -1] = (120, 80, 10)
    return t


def test_match_best_finds_dot():
    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    frame[:] = (30, 30, 30)
    # node icon at (170, 80)
    frame[80:100, 170:190] = _dot_template()
    m = cvmod.match_best(frame, _dot_template(), threshold=0.75, template_name="node_herb")
    assert m is not None
    assert abs(m.x - 170) <= 3 and abs(m.y - 80) <= 3
    assert m.score > 0.9


def test_match_best_miss():
    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    assert cvmod.match_best(frame, _dot_template(), threshold=0.9) is None


def test_absdiff_score():
    a = np.full((60, 60, 3), 100, dtype=np.uint8)
    b = np.full((60, 60, 3), 100, dtype=np.uint8)
    assert cvmod.absdiff_score(a, b) == 0.0
    c = np.full((60, 60, 3), 170, dtype=np.uint8)
    assert cvmod.absdiff_score(a, c) == pytest_approx(70.0)
    d = np.zeros((10, 10, 3), dtype=np.uint8)
    assert cvmod.absdiff_score(a, d) == 255.0  # shape mismatch sentinel


def test_pumpkin_probes_include_screen_edges_and_exclude_hud():
    frame = np.full((720, 1280, 3), (60, 74, 48), dtype=np.uint8)
    expected = ((800, 46), (195, 706))
    for point in (*expected, (1160, 120)):
        cv2.ellipse(frame, point, (18, 12), 0, 0, 360, (140, 70, 62), -1)
    probes = cvmod.pumpkin_probe_points(frame, [(.79, 0, 1, 1)])
    assert len(probes) == 2
    for x, y in expected:
        assert any(abs(u * 1280 - x) < 5 and abs(v * 720 - y) < 5 for u, v in probes)


def test_red_pumpkin_is_separated_from_orange_soil():
    frame = np.full((720, 1280, 3), (120, 80, 45), dtype=np.uint8)
    cv2.ellipse(frame, (824, 258), (17, 13), 0, 0, 360, (140, 60, 54), -1)

    probes = cvmod.pumpkin_probe_points(frame)

    assert any(abs(u * 1280 - 824) < 5 and abs(v * 720 - 258) < 5 for u, v in probes[:6])


def pytest_approx(v):
    import pytest

    return pytest.approx(v, abs=1.0)
