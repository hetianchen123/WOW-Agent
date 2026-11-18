"""Register a measured local calibration into a fresh screenshot; no guessed scale."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from .ground_projection import ProjectionFrame, GroundAnchor, GroundProjection, ProjectionError
from .ground_calibration import CalibrationSample, register_ground, transform


def read_bundle(path, *, native_only=True):
    path = Path(path)
    data = json.loads(path.read_text())
    f = dict(data['frame'])
    for k in ('size','world_size','player_position','player_feet','camera_signature'):
        f[k] = tuple(f[k])
    frame = ProjectionFrame(**f)
    train = [GroundAnchor(**a) for a in data['training']]
    checks = [GroundAnchor(**a) for a in data['validation']]
    projection = GroundProjection(frame, train, checks)
    if native_only and projection.source != 'native_position_ground_correspondence':
        raise ProjectionError('calibration_native_measurements_required')
    image_path = path.parent/data['reference_image']
    mask_path = path.parent/data['reference_mask']
    if (hashlib.sha256(image_path.read_bytes()).hexdigest() != frame.image_sha256
            or hashlib.sha256(mask_path.read_bytes()).hexdigest() != data['reference_mask_sha256']):
        raise ProjectionError('calibration_reference_hash_mismatch')
    image = cv2.cvtColor(cv2.imread(str(image_path)),cv2.COLOR_BGR2RGB)
    mask = cv2.imread(str(mask_path),0)
    if image.shape[:2] != (frame.size[1],frame.size[0]) or mask.shape != image.shape[:2]:
        raise ProjectionError('calibration_reference_size_mismatch')
    reference = CalibrationSample(frame,image,mask,0,frame.captured_at,projection.source,True)
    return projection, reference, train, checks


def rebase_projection(bundle, current, rgb, matching_mask, *, now):
    """Two independent gates: original world holdouts + new ground match holdouts.

    A confirmed same local plane is required; matching vegetation/canopies is not
    permitted. Camera epoch may change but view/zoom must match; image registration
    accounts for in-plane translation and rotation, not a new pitch preset.
    """
    old,reference,train,checks = bundle
    base = old.frame
    if (current.map_id != base.map_id or current.floor_id != base.floor_id
            or current.world_size != base.world_size or current.size != base.size
            or current.camera_signature[:2] != base.camera_signature[:2]
            or abs(current.height-base.height) > .25):
        raise ProjectionError('calibration_local_scene_changed')
    if not 0 <= now-current.captured_at <= 15:
        raise ProjectionError('calibration_current_frame_expired')
    if (rgb.dtype != np.uint8 or rgb.shape != reference.image.shape
            or matching_mask.dtype != bool or matching_mask.shape != rgb.shape[:2]
            or matching_mask.sum()<1000):
        raise ProjectionError('calibration_current_ground_insufficient')
    # Registration only needs a support point to check fit coverage. It does not
    # identify the avatar. Native position is projected AFTER registration.
    yy,xx = np.nonzero(matching_mask)
    support_point = (float(np.median(xx)),float(np.median(yy)))
    sample = CalibrationSample(replace(current,player_feet=support_point),rgb,
        matching_mask.astype(np.uint8)*255,0,current.captured_at,old.source,True)
    matrix,_,evidence = register_ground(sample,reference)
    inverse = np.linalg.inv(matrix)
    anchors = [replace(a,pixel=tuple(map(float,p))) for a,p in zip(
        [*train,*checks], transform(inverse,[a.pixel for a in [*train,*checks]]))]
    offset = np.asarray(old.metric_delta(current.player_position))
    shift = np.eye(3);shift[:2,2] = -offset
    new_matrix = shift @ old.matrix @ matrix
    origin = np.linalg.solve(new_matrix,[0,0,1])
    origin = tuple(map(float,origin[:2]/origin[2]))
    frame = replace(current,player_feet=origin)
    projected = GroundProjection(frame,anchors[:len(train)],anchors[len(train):])
    # Check new geometric support at the avatar and each measured anchor. Do not
    # extrapolate into a corner absent from the new matching evidence.
    hull = cv2.convexHull(np.asarray(evidence['source_matches'],np.float32))
    if any(cv2.pointPolygonTest(hull,tuple(map(float,p)),False)<0
           for p in [origin,*[a.pixel for a in anchors]]):
        raise ProjectionError('calibration_current_support_insufficient')
    # Convert observed registration residual to world units (separate from the
    # original calibration holdout error; transformed holdouts are not new samples).
    source = np.asarray(evidence['source_matches'])
    dest = np.asarray(evidence['reference_matches'])
    predicted = transform(matrix,source)
    errors = [np.linalg.norm(old.delta(a)-old.delta(b)) for a,b in zip(predicted,dest)]
    combined = old.validation_error + max(errors)
    if combined > .35:
        raise ProjectionError('calibration_transfer_error')
    evidence.update(original_holdout_yards=old.validation_error,
                    transfer_max_yards=float(max(errors)),combined_bound_yards=float(combined),
                    reference_frame=base.frame_id,current_frame=frame.frame_id)
    return projected,evidence
