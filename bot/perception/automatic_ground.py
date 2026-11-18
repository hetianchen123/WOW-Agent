"""Automatic ground geometry from camera orbits and measured native translations.

No clicked points, guessed feet, quest pin scale, or training labels. Stationary
camera orbits locate the ground pivot; two independent translations fit the
plane and two OTHER movements validate it. A failed fit never becomes a route.
"""
from dataclasses import dataclass, replace
import math

import cv2
import numpy as np

from .ground_calibration import CalibrationSample, ground_correspondences, transform
from .ground_projection import GroundProjection, ProjectionError, finite_pair
from .ground_tracking import track_local_ground
from ..control.navigation import heading_error
from .surface_height import bounded_height_change, fit_height_gradient, height_error, height_proof, height_matches


SOURCE = 'automatic_native_motion'


def orbit_anchor(matrix, size):
    """The real fixed point of a planar camera yaw is its ground-axis pivot.

    A translation has no unique finite pivot and is explicitly rejected. A
    viewport-center prior restricts the search, but never supplies the point.
    """
    values, vectors = np.linalg.eig(matrix)
    real = [i for i, v in enumerate(values) if abs(v.imag) < 1e-6]
    complex_pair = [v for v in values if abs(v.imag) >= 1e-6]
    if len(real) != 1 or len(complex_pair) != 2:
        raise ProjectionError('auto_orbit_unobservable')
    phase = abs(np.angle(complex_pair[0] / values[real[0]]))
    if not math.radians(4) <= phase <= math.radians(35):
        raise ProjectionError('auto_orbit_angle_insufficient')
    p = vectors[:, real[0]].real
    if abs(p[2]) < 1e-7:
        raise ProjectionError('auto_orbit_pivot_at_infinity')
    p = p[:2] / p[2]
    w, h = size
    if not (.35*w < p[0] < .65*w and .45*h < p[1] < .92*h):
        raise ProjectionError('auto_orbit_pivot_outside_avatar_region')
    return p


@dataclass
class MotionPair:
    matrix: np.ndarray  # current pixels -> previous pixels
    delta: np.ndarray  # current native position - previous, in yards
    previous_points: np.ndarray
    current_points: np.ndarray
    ids: tuple
    height_delta: float = 0.


def fit_motion_plane(pairs, anchor, size):
    """Solve M H = T M; native displacement supplies scale and world axes.

    Normalize pixels before solving. M maps a fixed ground pivot to zero; its
    projective row is fitted, rather than assuming constant pixels-per-yard.
    """
    if len(pairs) < 2:
        raise ProjectionError('auto_more_translation_axes_required')
    displacements = np.asarray([p.delta for p in pairs])
    singular = np.linalg.svd(displacements, compute_uv=False)
    if singular[-1] < .15 or singular[-1]/singular[0] < .2:
        raise ProjectionError('auto_more_translation_axes_required')
    scale = max(size)
    normalize = np.array([[1/scale,0,-anchor[0]/scale],
                          [0,1/scale,-anchor[1]/scale],[0,0,1.]])
    base = np.zeros((3,3)); base[2,2] = 1
    basis = []
    for row,col in ((0,0),(0,1),(1,0),(1,1),(2,0),(2,1)):
        b = np.zeros((3,3)); b[row,col] = 1; basis.append(b)
    equations, constants = [], []
    for pair in pairs:
        h = normalize @ pair.matrix @ np.linalg.inv(normalize)
        determinant = np.linalg.det(h)
        if determinant <= 0:
            raise ProjectionError('auto_translation_mirrored')
        h /= np.cbrt(determinant)
        t = np.eye(3); t[:2,2] = pair.delta
        equations.append(np.stack([(b@h-t@b).ravel() for b in basis],axis=1))
        constants.append(-(base@h-t@base).ravel())
    coefficients, _, rank, singular = np.linalg.lstsq(np.vstack(equations), np.hstack(constants), rcond=None)
    if rank != 6 or singular[-1]/singular[0] < 1e-7:
        raise ProjectionError('auto_motion_fit_degenerate')
    result = (base + sum(c*b for c,b in zip(coefficients,basis))) @ normalize
    if not np.isfinite(result).all() or abs(np.linalg.det(result)) < 1e-10:
        raise ProjectionError('auto_motion_fit_degenerate')
    if max(motion_error(result,p) for p in pairs) > .2:
        raise ProjectionError('auto_motion_fit_error')
    return result


def motion_error(matrix, pair):
    errors = transform(matrix,pair.previous_points)-transform(matrix,pair.current_points)-pair.delta
    return float(np.max(np.linalg.norm(errors,axis=1)))


def sample(frame, rgb, observed, *, camera_views=(4,)):
    if (not finite_pair(frame.player_position) or not all(0<=v<=1 for v in frame.player_position)
            or not finite_pair(frame.world_size) or min(frame.world_size)<=0
            or not all(type(v) in (int,float) and math.isfinite(v)
                       for v in (frame.height,frame.heading,frame.captured_at))
            or rgb.dtype!=np.uint8 or rgb.shape!=(frame.size[1],frame.size[0],3)
            or frame.camera_signature[0] not in camera_views):
        raise ProjectionError('auto_native_observation_invalid')
    mask = observed.ground & (observed.semantic_confidence >= .9)
    if not mask.any():
        raise ProjectionError('auto_ground_texture_insufficient')
    # Support point is for registration coverage ONLY, never avatar localization.
    yy,xx = np.nonzero(mask)
    p = float(np.median(xx)),float(np.median(yy))
    return CalibrationSample(replace(frame,player_feet=p),rgb,mask.astype(np.uint8)*255,
                             0,frame.captured_at,SOURCE,True)


def rotation_observed(source, destination):
    """Distinguish an unexecuted turn from a failed planar geometry fit."""
    if len(source) >= 40:
        displacement = np.linalg.norm(source-destination, axis=1)
        if np.median(displacement) < .75 and np.quantile(displacement, .9) < 2:
            error = ProjectionError('auto_camera_rotation_unobserved')
            error.evidence = {'matches': len(source), 'median_px': float(np.median(displacement)),
                              'p90_px': float(np.quantile(displacement, .9))}
            raise error


def match(current, previous, *, require_rotation=False):
    source, destination = ground_correspondences(current, previous)
    if require_rotation:
        rotation_observed(source, destination)
    return track_local_ground(current,previous,seed=(source,destination))


class MotionProjection:
    """Measured projective plane with explicit motion/registration provenance."""
    source = SOURCE
    metric_delta = GroundProjection.metric_delta
    contains = GroundProjection.contains
    delta = GroundProjection.delta
    validate_current = GroundProjection.validate_current
    project_route = GroundProjection.project_route

    def __init__(self, frame, matrix, hull, error, anchor_error_px):
        self.frame, self.matrix, self.hull = frame, matrix, hull
        self.validation_error, self.anchor_error_px = error, anchor_error_px
        if not self.contains(frame.player_feet):
            raise ProjectionError('auto_anchor_outside_measured_ground')
        points = hull.reshape(-1,2)
        denom = np.c_[points,np.ones(len(points))] @ matrix[2]
        if np.min(abs(denom)) < 1e-6 or not (np.all(denom>0) or np.all(denom<0)):
            raise ProjectionError('auto_projection_horizon')


class AutomaticGround:
    """Resident, bounded local calibration. No file or manual-region dependency."""
    def __init__(self, *, camera_views=(4,)):
        self.camera_views=camera_views
        self.reset()

    def reset(self):
        self.base = self.previous = self.reference = None
        self.anchors, self.training, self.validation = [], [], []
        self.anchor_headings = []
        self.anchor = self.matrix = self.hull = self.projection = None
        self.attempts = 0
        self.anchor_error = 0
        self.error = 0
        self.observation_retries = 0
        self.measurements = []
        self.restored_at = None
        self.cache_id = None
        self.height_gradient = None
        self.stair_reference = None

    def update(self, frame, rgb, observed, *, now):
        if not 0 <= now-frame.captured_at <= 15:
            raise ProjectionError('auto_frame_expired')
        current = sample(frame,rgb,observed,camera_views=self.camera_views)
        if self.base:
            old = self.base.frame
            if (frame.map_id != old.map_id or frame.floor_id != old.floor_id
                    or frame.world_size != old.world_size or frame.size != old.size
                    or frame.camera_signature[:2] != old.camera_signature[:2]
                    or now-(self.restored_at if self.restored_at is not None else old.captured_at) > 120):
                self.reset()
        if self.reference:
            try:
                return self.rebase(current)
            except ProjectionError:
                # A different surface/camera cannot inherit a previous scale.
                self.reset()
        self.measurements.append((frame,rgb.copy(),current.ground_mask.copy()))
        if self.base is None:
            self.base = self.previous = current
            return None, dict(stage='orbit',yaw_pixels=110,reason='automatic_ground_pivot_required')
        self.attempts += 1
        if self.attempts > 9:
            raise ProjectionError('auto_calibration_sample_budget')
        old = self.previous.frame
        displacement = np.asarray([(b-a)*s for a,b,s in zip(old.player_position,frame.player_position,frame.world_size)])
        if self.anchor is None:
            from_base=np.asarray([(b-a)*s for a,b,s in zip(self.base.frame.player_position,
                                         frame.player_position,frame.world_size)])
            if (np.linalg.norm(displacement) > .08 or np.linalg.norm(from_base)>.08
                    or abs(frame.height-self.base.frame.height)>.15):
                raise ProjectionError('auto_orbit_player_moved')
            try:
                h, evidence = match(current,self.base,require_rotation=True)
            except ProjectionError as exc:
                # One stationary re-observation for a lagging camera/animation;
                # never accept a failed fit or reset the total recovery budget.
                if str(exc) in {'auto_camera_rotation_unobserved','registration_holdout_error',
                                'registration_insufficient_matches','registration_inlier_ratio',
                                'registration_fit_failed','tracking_validation_failed',
                                'tracking_insufficient_matches','tracking_seed_insufficient',
                                'tracking_seed_unreliable','tracking_local_texture_missing',
                                'tracking_flow_failed','tracking_near_surface_unresolved'} and self.observation_retries < 1:
                    self.observation_retries += 1
                    return None, dict(stage='observe',wait_s=.7,reason=str(exc),
                                      details=getattr(exc,'evidence',None))
                raise
            self.observation_retries = 0
            anchor = orbit_anchor(h,frame.size)
            hull = cv2.convexHull(np.asarray(evidence['source_matches'],np.float32))
            if cv2.pointPolygonTest(hull,tuple(map(float,anchor)),False) < 0:
                raise ProjectionError('auto_orbit_ground_support_missing')
            if self.anchor_headings and abs(heading_error(self.anchor_headings[-1],frame.heading))<math.radians(4):
                return None,dict(stage='orbit',yaw_pixels=-220,reason='independent_rotation_required',
                                 registration={k:v for k,v in evidence.items() if not isinstance(v,list)})
            self.anchors.append(anchor)
            self.anchor_headings.append(frame.heading)
            self.previous = current
            if len(self.anchors) < 2:
                return None, dict(stage='orbit',yaw_pixels=-220,reason='independent_pivot_check',
                                  registration={k:v for k,v in evidence.items() if not isinstance(v,list)})
            self.anchor_error = float(np.linalg.norm(self.anchors[0]-self.anchors[1]))+2
            if self.anchor_error > 5:
                raise ProjectionError('auto_orbit_pivot_inconsistent')
            self.anchor = np.mean(self.anchors,axis=0)
            return None, dict(stage='translation',axis='either',anchor=self.anchor.tolist())
        if abs(heading_error(old.heading,frame.heading)) > math.radians(1):
            raise ProjectionError('auto_translation_camera_turned')
        length = np.linalg.norm(displacement)
        if length > 1.25:
            raise ProjectionError('auto_translation_displacement_excessive')
        height_delta=frame.height-old.height
        if not bounded_height_change(float(length),height_delta):
            raise ProjectionError('auto_surface_height_discontinuity')
        if length < .15:
            self.previous = current
            return None, dict(stage='translation',axis=self.needed_axis(),anchor=self.anchor.tolist(),
                              reason='no_measurable_displacement')
        h, evidence = match(current,self.previous)
        pair = MotionPair(h,displacement,np.asarray(evidence['reference_matches']),
                          np.asarray(evidence['source_matches']),(old.frame_id,frame.frame_id),height_delta)
        self.previous = current
        if self.matrix is None:
            self.training.append(pair)
            try:
                matrix = fit_motion_plane(self.training,self.anchor,frame.size)
                gradient = fit_height_gradient(self.training)
            except ProjectionError as exc:
                if str(exc) != 'auto_more_translation_axes_required': raise
            else:
                # Publish both fits together. A rejected height model cannot
                # leave an apparently fitted plane ready for validation.
                self.matrix, self.height_gradient = matrix, gradient
            return None, dict(stage='translation',axis=self.needed_axis(),anchor=self.anchor.tolist())
        # Never add these movements to the fit: they are independent holdouts.
        error = motion_error(self.matrix,pair)
        if error > .25:
            raise ProjectionError('auto_independent_motion_error')
        if height_error(self.height_gradient,pair)>.15:
            raise ProjectionError('auto_independent_height_error')
        self.validation.append(pair); self.error = max(self.error,error)
        if len(self.validation) < 2:
            return None, dict(stage='translation',axis=self.needed_axis(),anchor=self.anchor.tolist())
        d = np.asarray([p.delta for p in self.validation])
        s = np.linalg.svd(d,compute_uv=False)
        if s[-1] < .1 or s[-1]/s[0] < .2:
            return None, dict(stage='translation',axis=self.needed_axis(),anchor=self.anchor.tolist())
        # Require visual support in the current exposure, independent of feet visibility.
        self.hull = cv2.convexHull(np.asarray(evidence['source_matches'],np.float32))
        self.reference = current
        self.projection = MotionProjection(replace(frame,player_feet=tuple(map(float,self.anchor))),
                                           self.matrix,self.hull,self.error,self.anchor_error)
        return self.projection, self.proof(frame)

    def needed_axis(self):
        pairs = self.validation if self.matrix is not None else self.training
        return 'independent' if pairs else 'either'

    def proof(self, frame, transfer=0):
        return dict(stage='ready',current_frame=frame.frame_id,calibration_source=SOURCE,
                    combined_bound_yards=self.error+transfer,anchor_error_px=self.anchor_error,
                    fit_movements=len(self.training),validation_movements=len(self.validation),
                    fit_frames=[list(p.ids) for p in self.training],
                    validation_frames=[list(p.ids) for p in self.validation],manual_points=0,
                    surface_height=height_proof(self.height_gradient,self.training,self.validation),
                    restored_cache=self.cache_id)

    def rebase(self, current):
        offset = np.asarray([(b-a)*s for a,b,s in zip(self.reference.frame.player_position,
                    current.frame.player_position,current.frame.world_size)])
        proof=height_proof(self.height_gradient,self.training,self.validation)
        if not height_matches(offset.tolist(),current.frame.height-self.reference.frame.height,proof):
            raise ProjectionError('auto_surface_height_changed')
        h,evidence = match(current,self.reference)
        shift = np.eye(3); shift[:2,2] = -offset
        matrix = shift @ self.matrix @ h
        origin = np.linalg.solve(matrix,[0,0,1]); origin = origin[:2]/origin[2]
        # Orbit pivot remains stable in a centered fixed-pitch camera.
        if np.linalg.norm(origin-self.anchor) > self.anchor_error+3:
            raise ProjectionError('auto_current_anchor_mismatch')
        a = transform(self.matrix,transform(h,evidence['source_matches']))
        b = transform(self.matrix,evidence['reference_matches'])
        transfer = float(np.max(np.linalg.norm(a-b,axis=1)))
        if self.error+transfer > .35:
            raise ProjectionError('auto_current_transfer_error')
        hull = cv2.convexHull(np.asarray(evidence['source_matches'],np.float32))
        projection = MotionProjection(replace(current.frame,player_feet=tuple(map(float,origin))),matrix,
                                      hull,self.error+transfer,self.anchor_error)
        return projection,self.proof(current.frame,transfer)
