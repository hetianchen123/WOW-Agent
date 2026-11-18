from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.perception.ground_calibration import CalibrationSample, build_calibration, register_ground, register_points
from bot.perception.ground_projection import ProjectionFrame, ProjectionError


def scene():
    rng = np.random.default_rng(48)
    image = rng.integers(0, 255, (600, 800, 3), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (3, 3), 0)
    frame = ProjectionFrame('reference', 'a'*64, (800, 600), 2451, 'test-plane',
        (1000, 500), (.25, .36), (400, 300), 100, 0, (4, 32, 7), 100)
    mask = np.full((600, 800), 255, np.uint8)
    mask[:30] = 0; mask[-30:] = 0; mask[:, :30] = 0; mask[:, -30:] = 0
    cv2.circle(mask, (400, 300), 35, 0, -1)
    reference = CalibrationSample(frame, image, mask, 20, 100.05, 'synthetic_regression', True)
    def world(p):
        x, y = p[0]-400, p[1]-300
        denominator = 1+.0001*x-.00015*y
        return (.25+(.025*x+.006*y)/denominator/1000, .36+(-.007*x+.03*y)/denominator/500)
    def sample(p, index):
        dx, dy = 400-p[0], 300-p[1]
        moved = cv2.warpAffine(image, np.float32([[1, 0, dx], [0, 1, dy]]), (800, 600))
        moved_frame = replace(frame, frame_id=f'sample-{index}', image_sha256=f'{index:064x}',
                              player_position=world(p), captured_at=80+index)
        return CalibrationSample(moved_frame, moved, mask.copy(), index, 80+index+.05, 'synthetic_regression', True)
    train = [sample(p, i) for i, p in enumerate([(280, 200), (400, 200), (520, 200),
                                               (520, 400), (400, 400), (280, 400)])]
    check = [sample(p, i+6) for i, p in enumerate([(340, 260), (460, 260), (400, 350)])]
    return reference, train, check, world


def test_follow_camera_registration_yields_world_route_with_fixed_player_center():
    ref, train, check, world = scene()
    projection, anchors, heldout, evidence = build_calibration(ref, train, check)
    assert all(s.frame.player_feet == (400, 300) for s in [ref, *train, *check])
    assert np.allclose(anchors[0].pixel, (280, 200), atol=.2)
    assert np.allclose(anchors[3].pixel, (520, 400), atol=.2)
    assert len(evidence) == 9 and all(row['holdout_max_px'] <= 2 for row in evidence)
    route = SimpleNamespace(frame_id=ref.frame.frame_id, points=[(400, 300), (480, 320), (480, 240)])
    result = projection.project_route(route, ref.frame, now=101)
    assert np.allclose(result.world_points, [world(p) for p in route.points], atol=1e-5)
    assert not result.executable and result.calibration_source == 'synthetic_regression'


def test_independent_position_error_rejects_otherwise_good_visual_registration():
    ref, train, check, _ = scene()
    check[1] = replace(check[1], frame=replace(check[1].frame, player_position=(.255, .36)))
    with pytest.raises(ProjectionError, match='holdout_error'):
        build_calibration(ref, train, check)


@pytest.mark.parametrize('change,reason', [
    ({'reviewed': False}, 'not_reviewed'), ({'telemetry_at': 0}, 'unsynchronized'),
    ({'source': 'model_guess'}, 'not_reviewed'),
])
def test_unreviewed_unsynchronized_or_invented_sample_is_not_a_measurement(change, reason):
    ref, train, check, _ = scene(); train[0] = replace(train[0], **change)
    with pytest.raises(ProjectionError, match=reason): build_calibration(ref, train, check)


@pytest.mark.parametrize('change,reason', [
    ({'floor_id': 'upstairs'}, 'scene_changed'), ({'camera_signature': (4, 20, 7)}, 'scene_changed'),
    ({'height': 102}, 'ground_plane_changed'),
])
def test_scene_changes_reject_calibration_before_feature_matching(change, reason):
    ref, train, check, _ = scene(); train[0] = replace(train[0], frame=replace(train[0].frame, **change))
    with pytest.raises(ProjectionError, match=reason): build_calibration(ref, train, check)


def test_duplicate_exposure_cannot_validate_itself():
    ref, train, check, _ = scene(); check[0] = train[0]
    with pytest.raises(ProjectionError, match='duplicate_exposure'): build_calibration(ref, train, check)


def test_textureless_ground_does_not_fall_back_to_screen_center_or_camera_angle():
    ref, train, _, _ = scene()
    empty = replace(train[0], image=np.zeros_like(train[0].image))
    with pytest.raises(ProjectionError, match='texture_missing'): register_ground(empty, ref)


def test_ground_matches_need_independent_check_and_spatial_support():
    points = np.array([(x, y) for y in range(100, 501, 40) for x in range(100, 701, 40)], np.float64)
    shifted = points+np.array([20, -10])
    _, feet, _ = register_points(points, shifted, (400, 300))
    assert np.allclose(feet, (420, 290))
    broken = shifted.copy(); broken[::5] += 6
    with pytest.raises(ProjectionError, match='holdout_error'):
        register_points(points, broken, (400, 300))
    with pytest.raises(ProjectionError, match='outside_support'):
        register_points(points, shifted, (750, 300))


def test_mirror_cannot_reverse_world_steering():
    points = np.array([(x, y) for y in range(100, 501, 40) for x in range(100, 701, 40)], np.float64)
    mirrored = points.copy(); mirrored[:, 0] = 800-mirrored[:, 0]
    with pytest.raises(ProjectionError, match='mirrored'): register_points(points, mirrored, (400, 300))
