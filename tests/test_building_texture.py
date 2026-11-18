"""Real low-contrast failure, followed by synthetic correspondence validation."""
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.perception.automatic_ground import AutomaticGround, sample, match
from bot.perception.ground_calibration import ground_features
from bot.perception.ground_projection import ProjectionFrame, ProjectionError
from experiments.route_models.segmentation_worker import building_job


def recorded():
    path=Path(__file__).parent/'fixtures/building-low-texture-20261118'
    meta=json.loads(path.with_suffix('.json').read_text())
    with np.load(path.with_suffix('.npz')) as raw:
        a={k:raw[k] for k in raw.files}
    observed=SimpleNamespace(**{k:a[k] for k in ['ground','obstacles','occluded','water','forbidden']},
        semantic_labels=a['labels'],semantic_confidence=a['confidence'])
    values=dict(meta['request']['frame'])
    for k in ['size','world_size','player_position','camera_signature']:values[k]=tuple(values[k])
    frame=replace(ProjectionFrame(**values),captured_at=time.monotonic())
    return meta,a['rgb'],observed,frame


def test_latest_real_failure_can_start_measurement_without_inventing_scale():
    meta,rgb,observed,frame=recorded()
    before=observed.ground.copy()
    calibrator=AutomaticGround(camera_views=(2,4,5))
    result=building_job(calibrator,frame,rgb,observed,
                        dict(meta['request'],building_scene=meta['scene']))
    measurement=result['ground_measurement']
    assert measurement['high_contrast_features']==10
    assert measurement['feature_threshold']==5 and measurement['texture_features']>=400
    assert result['kind']=='calibration_orbit'
    assert result['executable'] is False and 'next_segment_world' not in result
    assert calibrator.matrix is None and np.array_equal(before,observed.ground)


def test_low_contrast_image_still_needs_geometric_and_native_motion_validation():
    _,rgb,observed,frame=recorded()
    previous=sample(frame,rgb,observed,camera_views=(2,4,5))
    # Synthetic 2px translation is explicitly not a measured game movement.
    shift=np.array([[1,0,2],[0,1,1]],dtype=float)
    image=cv2.warpAffine(rgb,shift,frame.size)
    mask=cv2.warpAffine(previous.ground_mask,shift,frame.size,flags=cv2.INTER_NEAREST)
    current=replace(previous,image=image,ground_mask=mask)
    matrix,evidence=match(current,previous)
    assert evidence['holdout_ratio']>=.9 and len(evidence['holdout_indices'])>=16
    assert matrix[0,2]==pytest.approx(-2,abs=.5)
    assert matrix[1,2]==pytest.approx(-1,abs=.5)
    assert frame.player_feet is None  # Matching is not a character anchor.


def test_blank_ground_never_gains_features_from_threshold_recovery():
    rgb=np.full((100,100,3),42,np.uint8)
    points,desc,evidence=ground_features(rgb,np.full((100,100),255,np.uint8))
    assert len(points)==0 and desc is None and evidence['texture_features']==0


def test_texture_outside_measured_ground_cannot_seed_alignment():
    rgb=np.random.default_rng(1).integers(0,256,(100,100,3),dtype=np.uint8)
    points,_,evidence=ground_features(rgb,np.zeros((100,100),np.uint8))
    assert not points and evidence['texture_features']==0
