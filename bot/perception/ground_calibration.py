"""Register observed ground to a reference exposure, then fit measured map scale.

No model calls, game input, inferred world anchors, or execution authorization.
Masks must contain only reviewed static ground on the same locally planar surface.
"""
from dataclasses import dataclass
import math

import cv2
import numpy as np

from .ground_projection import GroundAnchor, GroundProjection, ProjectionError, finite_pair


@dataclass(frozen=True)
class CalibrationSample:
    frame: object
    image: np.ndarray
    ground_mask: np.ndarray
    sequence: int
    telemetry_at: float
    source: str = 'native_position_ground_correspondence'
    reviewed: bool = False


def transform(matrix, points):
    points = np.asarray(points, dtype=np.float64)
    homogeneous = np.c_[points, np.ones(len(points))] @ matrix.T
    if not np.isfinite(homogeneous).all() or np.any(np.abs(homogeneous[:, 2]) < 1e-8):
        raise ProjectionError('registration_invalid_transform')
    return homogeneous[:, :2] / homogeneous[:, 2:]


def register_points(source, destination, feet):
    """Fit on 80% of texture matches; check independent matches and feet coverage."""
    source, destination = np.asarray(source, np.float64), np.asarray(destination, np.float64)
    if (source.shape != destination.shape or source.ndim != 2 or source.shape[1] != 2
            or len(source) < 40 or not np.isfinite(source).all() or not np.isfinite(destination).all()):
        raise ProjectionError('registration_insufficient_matches')
    train = np.arange(len(source)) % 5 != 0
    matrix, inliers = cv2.findHomography(source[train], destination[train], cv2.RANSAC, 1.5)
    if matrix is None or inliers is None:
        raise ProjectionError('registration_fit_failed')
    keep = inliers.ravel().astype(bool)
    if keep.sum() < 24 or keep.mean() < .8:
        raise ProjectionError('registration_inlier_ratio')
    train_error = np.linalg.norm(transform(matrix, source[train][keep])-destination[train][keep], axis=1)
    check_error = np.linalg.norm(transform(matrix, source[~train])-destination[~train], axis=1)
    if np.max(train_error) > 2 or np.max(check_error) > 2:
        raise ProjectionError('registration_holdout_error')
    hull = cv2.convexHull(source[train][keep].astype(np.float32))
    if (cv2.contourArea(hull) < 1000 or not finite_pair(feet)
            or cv2.pointPolygonTest(hull, tuple(map(float, feet)), False) < 0):
        raise ProjectionError('registration_feet_outside_support')
    # No projective pole or mirror in the measured region.
    hp = hull.reshape(-1, 2)
    denominators = np.c_[hp, np.ones(len(hp))] @ matrix[2]
    if (np.min(np.abs(denominators)) < 1e-6
            or not (np.all(denominators > 0) or np.all(denominators < 0))):
        raise ProjectionError('registration_horizon')
    mapped = transform(matrix, hp)
    if cv2.contourArea(hp, oriented=True)*cv2.contourArea(mapped.astype(np.float32), oriented=True) <= 0:
        raise ProjectionError('registration_mirrored')
    anchor = transform(matrix, [feet])[0]
    return matrix, tuple(map(float, anchor)), {
        'matches': len(source), 'inliers': int(keep.sum()),
        'holdout_max_px': float(check_error.max()), 'fit_max_px': float(train_error.max()),
        'source_matches': source.tolist(), 'reference_matches': destination.tolist(),
        'holdout_indices': np.flatnonzero(~train).tolist(),
    }


def ground_features(rgb, ground_mask):
    """Expose low-contrast road texture without changing geometric validation.

    FAST's contrast threshold is a feature proposal, not a confidence in ground
    or motion. Dark game roads can contain hundreds of trackable corners while
    the original threshold finds fewer than sixteen. Both passes use the same
    observed ground mask; downstream mutual matching and holdouts still decide.
    """
    mask = cv2.erode(ground_mask, np.ones((7, 7), np.uint8))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    def detect(threshold):
        return cv2.ORB_create(nfeatures=4000, fastThreshold=threshold,
                             nlevels=1).detectAndCompute(gray, mask)
    points, descriptors = detect(12)
    strong = len(points)
    threshold = 12
    if strong < 40:
        points, descriptors = detect(5)
        threshold = 5
    return points, descriptors, dict(feature_threshold=threshold,
        high_contrast_features=strong, texture_features=len(points))


def ground_correspondences(sample, reference):
    # Fixed zoom collection: avoid coarse pyramid keypoints adding several pixels
    # of quantization error to the independent correspondence check.
    def features(item):
        points, descriptors, _ = ground_features(item.image, item.ground_mask)
        return points, descriptors
    a, da = features(sample)
    b, db = features(reference)
    if da is None or db is None:
        raise ProjectionError('registration_ground_texture_missing')
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    forward = matcher.knnMatch(da, db, k=2)
    reverse = matcher.knnMatch(db, da, k=2)
    backward = {m.queryIdx: m.trainIdx for pair in reverse if len(pair) == 2
                for m, n in [pair] if m.distance < .7*n.distance}
    pairs = [m for pair in forward if len(pair) == 2 for m, n in [pair]
             if m.distance < .7*n.distance and backward.get(m.trainIdx) == m.queryIdx]
    return (np.asarray([a[m.queryIdx].pt for m in pairs], np.float64).reshape(-1, 2),
            np.asarray([b[m.trainIdx].pt for m in pairs], np.float64).reshape(-1, 2))


def register_ground(sample, reference):
    source, destination = ground_correspondences(sample, reference)
    return register_points(source, destination, sample.frame.player_feet)


def build_calibration(reference, training, validation):
    samples = [reference, *training, *validation]
    ids = [s.frame.frame_id for s in samples]
    if len(set(ids)) != len(ids):
        raise ProjectionError('calibration_duplicate_exposure')
    if len(training) < 6 or len(validation) < 3:
        raise ProjectionError('projection_insufficient_correspondences')
    base = reference.frame
    for item in samples:
        frame = item.frame
        if (not item.reviewed or item.source not in {'native_position_ground_correspondence', 'synthetic_regression'}
                or item.source != reference.source):
            raise ProjectionError('calibration_ground_not_reviewed')
        if (item.image.dtype != np.uint8 or item.image.shape != (base.size[1], base.size[0], 3)
                or item.ground_mask.dtype != np.uint8 or item.ground_mask.shape != item.image.shape[:2]
                or np.count_nonzero(item.ground_mask) < 1000):
            raise ProjectionError('calibration_image_or_mask_invalid')
        if (frame.map_id != base.map_id or frame.floor_id != base.floor_id
                or frame.world_size != base.world_size or frame.size != base.size
                or frame.camera_signature != base.camera_signature):
            raise ProjectionError('calibration_scene_changed')
        if (not math.isfinite(frame.height) or abs(frame.height-base.height) > .25
                or not finite_pair(frame.player_position) or not finite_pair(frame.player_feet)):
            raise ProjectionError('calibration_ground_plane_changed')
        if (type(item.sequence) is not int or not math.isfinite(item.telemetry_at)
                or not math.isfinite(frame.captured_at) or abs(frame.captured_at-item.telemetry_at) > .25
                or not 0 <= base.captured_at-frame.captured_at <= 600):
            raise ProjectionError('calibration_unsynchronized_sample')
    anchors, records = [], []
    for item in [*training, *validation]:
        matrix, pixel, evidence = register_ground(item, reference)
        anchors.append(GroundAnchor(pixel, item.frame.player_position, item.frame.frame_id, item.source))
        records.append({'frame_id': item.frame.frame_id, 'sequence': item.sequence,
                        'to_reference': matrix.tolist(), 'reference_feet': pixel, **evidence})
    projection = GroundProjection(base, anchors[:len(training)], anchors[len(training):])
    return projection, anchors[:len(training)], anchors[len(training):], records
