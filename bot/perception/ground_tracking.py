"""Local ground tracking with photometric checks and held-out correspondences.

A sparse match only seeds image alignment. It cannot authorize projection.
The final transform is fitted on separate tracked texture points, then checked
on held-out points with the same two-pixel bound and spatial coverage checks.
"""
import cv2
import numpy as np

from .ground_calibration import ground_correspondences, transform
from .ground_projection import ProjectionError


def _fail(reason, **evidence):
    error = ProjectionError(reason)
    error.evidence = evidence
    raise error


def _near_surface(source, destination, train, size):
    """Select a local surface from TRAINING geometry, never holdout residuals.

    Ground semantics are not a coplanarity label. A distant slope or animated
    grass can have good image matches while moving differently from nearby
    ground. Use the foreground texture as a plane hypothesis; spatially
    contradictory bands in front of it bound its measured extent. This region
    selects texture only, not an avatar point or a walkable corridor.
    """
    w,h = size
    core = train & (destination[:,1] >= .65*h)
    if np.count_nonzero(core) < 40:
        return np.ones(len(source),bool),dict(surface_selection='whole_support')
    matrix,_ = cv2.findHomography(source[core],destination[core],cv2.USAC_MAGSAC,
                                  2,maxIters=5000,confidence=.999)
    if matrix is None:
        _fail('tracking_near_surface_unresolved')
    # Do not inspect held-out destinations, including when selecting coverage.
    errors = np.linalg.norm(transform(matrix,source[train])-destination[train],axis=1)
    rows = destination[train,1]
    core_ratio = float(np.mean(errors[rows >= .65*h] <= 2))
    if core_ratio < .9:
        _fail('tracking_near_surface_unresolved',near_fit_ratio=core_ratio)
    boundary = 0
    contradictory = []
    for band in range(13):
        selected = (rows >= band*h/20) & (rows < (band+1)*h/20)
        count = int(selected.sum())
        if count >= 6 and np.mean(errors[selected] <= 2) < .6:
            boundary = (band+1)*h/20
            contradictory.append(band)
    keep = destination[:,1] >= boundary
    return keep,dict(surface_selection='near_training_plane',
        surface_reference_y_min=boundary,near_fit_ratio=core_ratio,
        excluded_surface_matches=int(np.count_nonzero(~keep)),
        contradictory_reference_bands=contradictory)


def validate_tracking(source, destination, size, *, local_surface=False):
    """Robust training; validation points never participate in fitting."""
    source, destination = np.asarray(source, float), np.asarray(destination, float)
    if (source.shape != destination.shape or source.ndim != 2 or source.shape[1] != 2
            or len(source) < 80 or not np.isfinite([source,destination]).all()):
        _fail('tracking_insufficient_matches', matches=len(source))
    train = np.arange(len(source)) % 5 != 0
    surface = {}
    if local_surface:
        keep,surface = _near_surface(source,destination,train,size)
        source,destination,train = source[keep],destination[keep],train[keep]
        # Preserve the original train/holdout partition after spatial selection.
        if len(source)<80 or np.count_nonzero(~train)<16:
            _fail('tracking_insufficient_matches',matches=len(source),**surface)
    matrix, _ = cv2.findHomography(source[train], destination[train], cv2.USAC_MAGSAC,
                                   2, maxIters=5000, confidence=.999)
    if matrix is None:
        _fail('tracking_fit_failed')
    errors = np.linalg.norm(transform(matrix, source)-destination, axis=1)
    valid = errors <= 2
    fit_ratio, check_ratio = float(valid[train].mean()), float(valid[~train].mean())
    if fit_ratio < .9 or check_ratio < .9 or np.count_nonzero(valid & ~train) < 16:
        _fail('tracking_validation_failed', matches=len(source), fit_ratio=fit_ratio,
              holdout_ratio=check_ratio, holdout_max_px=float(errors[~train].max()),**surface)
    hull = cv2.convexHull(source[train & valid].astype(np.float32))
    check_hull = cv2.convexHull(source[~train & valid].astype(np.float32))
    area, check_area = cv2.contourArea(hull), cv2.contourArea(check_hull)
    # Usable local geometry must not depend on what fraction of the screenshot
    # is ground. Check two-dimensional support against our 2 px error bound;
    # held-out points must still cover the fitted patch. Projection separately
    # restricts the avatar and every route point to this measured hull.
    points=source[train & valid]
    spread=np.linalg.svd(points-points.mean(axis=0),compute_uv=False)/np.sqrt(len(points))
    if spread[-1] < 8 or spread[-1]/spread[0] < .05 or check_area < .4*area:
        _fail('tracking_spatial_support_missing', fit_area=area, holdout_area=check_area)
    points = hull.reshape(-1,2)
    denominators = np.c_[points,np.ones(len(points))] @ matrix[2]
    if (np.min(abs(denominators)) < 1e-6
            or not (np.all(denominators>0) or np.all(denominators<0))):
        _fail('registration_horizon')
    mapped = transform(matrix, points).astype(np.float32)
    if cv2.contourArea(points,oriented=True)*cv2.contourArea(mapped,oriented=True) <= 0:
        _fail('registration_mirrored')
    # Rejected matches are reported, never propagated as measured ground.
    # Native-world movement validation is an additional independent gate.
    return matrix, dict(method='local_photometric_tracking',matches=len(source),
        inliers=int(np.count_nonzero(train & valid)),fit_ratio=fit_ratio,holdout_ratio=check_ratio,
        fit_max_px=float(errors[train & valid].max()),holdout_max_px=float(errors[~train & valid].max()),
        rejected_matches=int(np.count_nonzero(~valid)),holdout_rejected=int(np.count_nonzero(~train & ~valid)),
        raw_holdout_max_px=float(errors[~train].max()),
        source_matches=source[valid].tolist(),reference_matches=destination[valid].tolist(),
        holdout_indices=np.flatnonzero(~train[valid]).tolist(),
        holdout_source_matches=source[~train & valid].tolist(),
        fit_area_px=area,holdout_area_px=check_area,**surface)


def track_local_ground(current, reference, *, seed=None):
    source, destination = seed if seed is not None else ground_correspondences(current,reference)
    if len(source) < 16:
        _fail('tracking_seed_insufficient', matches=len(source))
    seed_h, inliers = cv2.findHomography(source,destination,cv2.RANSAC,1.5,
                                         maxIters=5000,confidence=.999)
    if seed_h is None or inliers is None or inliers.sum()<16 or inliers.mean()<.4:
        _fail('tracking_seed_unreliable', matches=len(source))
    current_gray = cv2.cvtColor(current.image,cv2.COLOR_RGB2GRAY)
    reference_gray = cv2.cvtColor(reference.image,cv2.COLOR_RGB2GRAY)
    h,w = current_gray.shape
    mask_a = cv2.erode(current.ground_mask,np.ones((7,7),np.uint8))
    mask_b = cv2.erode(reference.ground_mask,np.ones((7,7),np.uint8))
    warped = cv2.warpPerspective(current_gray,seed_h,(w,h))
    warped_mask = cv2.warpPerspective(mask_a,seed_h,(w,h),flags=cv2.INTER_NEAREST)
    overlap = mask_b & warped_mask
    # Only the nearby surface, not far slopes and rooftops, estimates local scale.
    # This region chooses texture; it does not supply the character's ground point.
    overlap[:round(h*.35)] = 0
    overlap[:,:round(w*.2)] = 0
    overlap[:,round(w*.8):] = 0
    points = cv2.goodFeaturesToTrack(reference_gray,maxCorners=2400,qualityLevel=.003,
                                     minDistance=6,mask=overlap)
    if points is None or len(points)<80:
        _fail('tracking_local_texture_missing', corners=0 if points is None else len(points))
    settings=dict(winSize=(9,9),maxLevel=0,criteria=(3,40,.001))
    forward,ok,_ = cv2.calcOpticalFlowPyrLK(reference_gray,warped,points,None,**settings)
    if forward is None:
        _fail('tracking_flow_failed')
    backward,back_ok,_ = cv2.calcOpticalFlowPyrLK(warped,reference_gray,forward,None,**settings)
    if backward is None:
        _fail('tracking_flow_failed')
    keep = ok.ravel().astype(bool) & back_ok.ravel().astype(bool)
    keep &= np.isfinite(forward).all(axis=(1,2)) & np.isfinite(backward).all(axis=(1,2))
    keep &= np.linalg.norm(backward-points,axis=2).ravel() < .25
    for index in np.flatnonzero(keep):
        a,b = points[index,0],forward[index,0]
        x,y = np.rint(b).astype(int)
        if not (6<=x<w-6 and 6<=y<h-6 and warped_mask[y,x]):
            keep[index]=False
            continue
        left = cv2.getRectSubPix(reference_gray,(11,11),tuple(a)).astype(float).ravel()
        right = cv2.getRectSubPix(warped,(11,11),tuple(b)).astype(float).ravel()
        left-=left.mean();right-=right.mean()
        correlation = left@right/max(np.linalg.norm(left)*np.linalg.norm(right),1e-5)
        keep[index] = correlation>=.85
    source = transform(np.linalg.inv(seed_h),forward[:,0][keep])
    destination = points[:,0][keep]
    matrix,evidence = validate_tracking(source,destination,(w,h),local_surface=True)
    evidence.update(seed_matches=len(inliers),seed_inliers=int(inliers.sum()),
                    tracked_candidates=len(points),photometric_matches=int(keep.sum()))
    return matrix,evidence
