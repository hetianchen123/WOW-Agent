"""Choose measured floor at a door, retaining all wall/collision exclusions."""
import cv2
import numpy as np

from ..perception.ground_projection import ProjectionError
from ..perception.ground_projection import measured_uncertainty_px
from .approach_navigation import ApproachNavigator
from ..perception.building_layout import doorway_floor_box


def doorway_approach_distance(observed,doors,*,crossing=False):
    """Rank measured endpoints toward visible floor, not a door-panel corner.

    A cue may be outside the calibrated hull; it supplies only image direction.
    Movement endpoints still require measured, connected ground below.
    """
    h,w=observed.ground.shape;cue=np.zeros((h,w),bool)
    usable=observed.ground & ~observed.obstacles & ~observed.water & ~observed.forbidden
    for door in doors:
        l,t,r,b=doorway_floor_box(door) if crossing else door['threshold_box']
        patch=np.zeros_like(cue);patch[max(0,t):min(h,b),max(0,l):min(w,r)]=True
        floor=patch&usable
        if floor.any():cue |= floor
        else:
            # The opening center is a look cue only, never added as free ground.
            x,y=round((l+r)/2),round((t+b)/2)
            if 0<=x<w and 0<=y<h:cue[y,x]=True
    if not cue.any():return np.full((h,w),np.inf)
    return cv2.distanceTransform((~cue).astype(np.uint8),cv2.DIST_L2,5)


def building_entry_region(projection, observed, rgb, destination, scene, *, crossing=False, visited=(), crossing_origin=None):
    h,w=observed.ground.shape
    if not scene.get('doorways'):raise ProjectionError('building_entrance_unobserved')
    region=np.zeros((h,w),bool)
    for door in scene.get('doorways',[]):
        x1,y1,x2,y2=door['box']
        l,t,r,b=door['threshold_box']
        if crossing:
            # Only visible floor INSIDE the opening can authorize crossing.
            l,t,r,b=doorway_floor_box(door)
        patch=np.zeros((h,w),bool)
        patch[max(0,t):min(h,b),max(0,l):min(w,r)]=True
        region |= patch & observed.ground & ~observed.obstacles & ~observed.water & ~observed.forbidden
    support=np.zeros((h,w),np.uint8)
    cv2.fillConvexPoly(support,np.rint(projection.hull).astype(np.int32),1)
    region &= support.astype(bool)
    geometry=ApproachNavigator(projection.frame.frame_id,observed.ground,observed.obstacles,
        observed.occluded,source='building_facade',forbidden=observed.water|observed.forbidden|~support.astype(bool),
        uncertainty_px=measured_uncertainty_px(projection))
    x,y=(round(v) for v in projection.frame.player_feet)
    yy,xx=np.mgrid[:h,:w]
    hom=np.stack((xx,yy,np.ones_like(xx)),axis=-1)@projection.matrix.T
    valid=abs(hom[:,:,2])>1e-8
    metric=hom[:,:,:2]/np.where(valid,hom[:,:,2],1)[:,:,None]
    distance=np.linalg.norm(metric,axis=-1)
    if crossing:
        # Leave room for the controller's .15-yard endpoint tolerance while
        # requiring .30 yards of net travel from the actual threshold.
        origin = projection.metric_delta(crossing_origin) if crossing_origin is not None else (0.,0.)
        region &= valid & (distance > .06) & (np.linalg.norm(metric-origin,axis=-1) >= .45)
    connected=[]
    for route in (geometry.known,geometry.possible):
        if not route.clear[y,x]:continue
        _,components=cv2.connectedComponents(route.clear.astype(np.uint8),connectivity=4)
        reachable=geometry.known.clear & (components==components[y,x])
        connected.append(reachable)
        threshold=region & reachable
        if threshold.any():
            return threshold,dict(goal_kind='building_entry_region',basis='door_floor_crossing' if crossing else 'door_threshold',
                entrance_verified=False,requires_native_entry_receipt=True)
    # A visible doorway may be beyond measured support or have a hidden sill.
    # Choose measured, connected ground toward that same-frame observation.
    # The image cue ranks endpoints, but supplies no world coordinate or scale.
    eligible=valid & support.astype(bool) & observed.ground
    eligible &= (distance>=(.5 if crossing else 1)) & (distance<=(2 if crossing else 6))
    for position in visited:
        eligible &= np.linalg.norm(metric-np.asarray(projection.metric_delta(position)),axis=-1)>.8
    region=np.zeros((h,w),bool)
    for reachable in connected:
        candidates=eligible & reachable
        if not candidates.any():continue
        scores=doorway_approach_distance(observed,scene['doorways'],crossing=crossing)
        # Require useful progress toward the observed threshold; never follow XY.
        candidates &= scores < scores[y,x]-8
        if not candidates.any():continue
        best=scores[candidates].min()
        region=candidates & (scores<=best+3)
        break
    if not region.any():raise ProjectionError('building_door_floor_unobserved' if crossing else 'building_door_approach_unconfirmed')
    return region,dict(goal_kind='building_entry_region',basis='door_floor_observation' if crossing else 'door_approach_frontier',
                       entrance_verified=False,requires_reobservation=True,requires_native_entry_receipt=True)
