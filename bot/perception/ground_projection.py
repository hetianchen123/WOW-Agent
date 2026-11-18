"""Measured, frame-bound ground-plane projection; no guessed pitch or input I/O.

Correspondences must refer to ground contact on one plane in the reference image.
This module validates the numerical transform, not perception or game collision.
The runtime overhead movement gate remains closed pending real calibration trials.
"""
from dataclasses import dataclass
import math

import cv2
import numpy as np

from ..control.navigation import heading_error


class ProjectionError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectionFrame:
    frame_id: str
    image_sha256: str
    size: tuple[int,int]
    map_id: int
    floor_id: str
    world_size: tuple[float,float]
    player_position: tuple[float,float]
    player_feet: tuple[float,float]
    height: float
    heading: float
    camera_signature: tuple  # view, measured zoom, epoch; optional measured FOV (legacy)
    captured_at: float


@dataclass(frozen=True)
class GroundAnchor:
    pixel: tuple[float,float]
    world_position: tuple[float,float]
    observation_id: str
    source: str  # native_position_ground_correspondence or synthetic_regression


@dataclass(frozen=True)
class ProjectedRoute:
    frame_id: str
    world_points: tuple
    length_yards: float
    validation_max_error_yards: float
    calibration_source: str
    executable: bool = False


def finite_pair(p):
    return (isinstance(p,(list,tuple)) and len(p)==2 and
            all(type(v) in (int,float) and math.isfinite(v) for v in p))


def measured_uncertainty_px(projection):
    """Same localization margin for route goals and route clearance checks."""
    uncertainty=max(4,math.ceil(getattr(projection,'anchor_error_px',0)))
    if getattr(projection,'source',None)=='automatic_native_motion':
        px,py=projection.frame.player_feet
        jacobian=np.stack((projection.delta((px+1,py))-projection.delta((px,py)),
                           projection.delta((px,py+1))-projection.delta((px,py))),axis=1)
        minimum=float(np.linalg.svd(jacobian,compute_uv=False)[-1])
        if minimum<=1e-6:raise ProjectionError('auto_local_scale_degenerate')
        # Reserve the same 0.15-yard controller tracking tolerance used by the
        # bounded route window; lateral movement error must fit inside clearance.
        uncertainty+=math.ceil((projection.validation_error+.15)/minimum)
    return uncertainty


class GroundProjection:
    def __init__(self,frame,training,validation):
        self.frame=frame
        if (not frame.frame_id or not frame.floor_id or frame.floor_id in {'unknown','unverified'} or len(frame.image_sha256)!=64 or
            any(c not in '0123456789abcdef' for c in frame.image_sha256) or
            type(frame.map_id) is not int or frame.map_id<=0 or
            not finite_pair(frame.world_size) or min(frame.world_size)<=0 or
            not finite_pair(frame.size) or min(frame.size)<8 or
            not finite_pair(frame.player_position) or not all(0<=v<=1 for v in frame.player_position) or
            not finite_pair(frame.player_feet) or not isinstance(frame.camera_signature,(tuple,list)) or
            len(frame.camera_signature) not in (3,4) or
            any(type(v) not in (int,float) or not math.isfinite(v) for v in frame.camera_signature) or
            not 1<=frame.camera_signature[0]<=5 or frame.camera_signature[1]<=0 or
            (len(frame.camera_signature)==4 and frame.camera_signature[3]<=0) or
            any(type(v) not in (int,float) or not math.isfinite(v)
                for v in [frame.height,frame.heading,frame.captured_at])):
            raise ProjectionError('projection_frame_incomplete')
        if len(training)<6 or len(validation)<3:
            raise ProjectionError('projection_insufficient_correspondences')
        anchors=tuple(training)+tuple(validation)
        sources={a.source for a in anchors}
        if len(sources)!=1 or not sources<={'native_position_ground_correspondence','synthetic_regression'}:
            raise ProjectionError('projection_unmeasured_correspondence')
        self.source=next(iter(sources))
        ids=[a.observation_id for a in anchors]
        if not all(ids) or len(set(ids))!=len(ids):
            raise ProjectionError('projection_validation_not_independent')
        for a in anchors:
            if (not finite_pair(a.pixel) or not all(0<=v<n for v,n in zip(a.pixel,frame.size)) or
                not finite_pair(a.world_position) or not all(0<=v<=1 for v in a.world_position)):
                raise ProjectionError('projection_correspondence_invalid')
        train_pixels=np.array([a.pixel for a in training],np.float64)
        self.hull=cv2.convexHull(train_pixels.astype(np.float32))
        if cv2.contourArea(self.hull)<400:
            raise ProjectionError('projection_anchor_coverage_degenerate')
        # A single forward/backward trace cannot determine lateral scale.
        singular=np.linalg.svd(train_pixels-train_pixels.mean(axis=0),compute_uv=False)
        if singular[-1]/singular[0]<.08:
            raise ProjectionError('projection_anchor_coverage_degenerate')
        for a in validation:
            if not self.contains(a.pixel):raise ProjectionError('projection_validation_outside_support')
            if any(math.dist(a.pixel,t.pixel)<2 for t in training):
                raise ProjectionError('projection_validation_not_independent')
        checks=np.array([a.pixel for a in validation],np.float64)
        if cv2.contourArea(cv2.convexHull(checks.astype(np.float32)))<100:
            raise ProjectionError('projection_validation_coverage_degenerate')
        world=np.array([self.metric_delta(a.world_position) for a in training],np.float64)
        matrix,_=cv2.findHomography(train_pixels,world,method=0)
        if matrix is None or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix))<1e-12:
            raise ProjectionError('projection_transform_degenerate')
        self.matrix=matrix
        hull_points=self.hull.reshape(-1,2)
        denominators=np.c_[hull_points,np.ones(len(hull_points))]@matrix[2]
        if (np.min(np.abs(denominators))<1e-6 or
                not (np.all(denominators>0) or np.all(denominators<0))):
            raise ProjectionError('projection_horizon_inside_support')
        train_error=self.errors(training)
        if max(train_error)>.2:raise ProjectionError('projection_fit_error')
        self.validation_error=max(self.errors(validation))
        if self.validation_error>.25:raise ProjectionError('projection_holdout_error')
        if not self.contains(frame.player_feet) or np.linalg.norm(self.delta(frame.player_feet))>.25:
            raise ProjectionError('projection_player_anchor_mismatch')

    def metric_delta(self,world):
        return [(v-o)*scale for v,o,scale in zip(world,self.frame.player_position,self.frame.world_size)]

    def contains(self,pixel):
        return finite_pair(pixel) and cv2.pointPolygonTest(self.hull,tuple(map(float,pixel)),False)>=0

    def delta(self,pixel):
        value=self.matrix@np.array([*pixel,1.0])
        if abs(value[2])<1e-8:raise ProjectionError('projection_near_horizon')
        return value[:2]/value[2]

    def errors(self,anchors):
        return [float(np.linalg.norm(self.delta(a.pixel)-self.metric_delta(a.world_position))) for a in anchors]

    def validate_current(self,current,*,now,max_age_s=20):
        """Check pose BEFORE searching. Visibility of feet is not a requirement."""
        frame=self.frame
        if current.frame_id!=frame.frame_id or current.image_sha256!=frame.image_sha256:
            raise ProjectionError('projection_frame_mismatch')
        if current.map_id!=frame.map_id or current.floor_id!=frame.floor_id or current.world_size!=frame.world_size:
            raise ProjectionError('projection_map_or_floor_changed')
        if current.size!=frame.size or current.camera_signature!=frame.camera_signature:
            raise ProjectionError('projection_camera_changed')
        if not 0<=now-frame.captured_at<=max_age_s:
            raise ProjectionError('projection_frame_expired')
        if (not finite_pair(current.player_position) or
                any(type(v) not in (int,float) or not math.isfinite(v) for v in [current.height,current.heading]) or
                abs(current.height-frame.height)>.25 or
                abs(heading_error(frame.heading,current.heading))>math.radians(2) or
                np.linalg.norm(self.metric_delta(current.player_position))>.15):
            raise ProjectionError('projection_pose_changed')

    def project_route(self,route,current,*,now,max_age_s=20):
        self.validate_current(current, now=now, max_age_s=max_age_s)
        frame=self.frame
        if route.frame_id!=frame.frame_id:
            raise ProjectionError('projection_frame_mismatch')
        if len(route.points)<2 or math.dist(route.points[0],frame.player_feet)>2:
            raise ProjectionError('projection_route_start_changed')
        points=[]
        for pixel in route.points:
            if not self.contains(pixel):raise ProjectionError('projection_outside_calibrated_ground')
            delta=self.delta(pixel)
            if np.linalg.norm(delta)>20:raise ProjectionError('projection_route_distance_budget')
            world=tuple(float(o+d/scale) for o,d,scale in zip(frame.player_position,delta,frame.world_size))
            if not all(0<=v<=1 for v in world):raise ProjectionError('projection_outside_map')
            points.append(world)
        lengths=[math.dist(self.metric_delta(a),self.metric_delta(b)) for a,b in zip(points,points[1:])]
        if max(lengths)>15 or sum(lengths)>40:raise ProjectionError('projection_route_distance_budget')
        return ProjectedRoute(frame.frame_id,tuple(points),sum(lengths),self.validation_error,self.source)
