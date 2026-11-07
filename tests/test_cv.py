import numpy as np

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


def pytest_approx(v):
    import pytest

    return pytest.approx(v, abs=1.0)
