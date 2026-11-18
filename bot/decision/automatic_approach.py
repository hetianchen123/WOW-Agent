"""Automatic calibration probes and NPC SEARCH regions, never interaction claims."""
import math
import cv2
import numpy as np

from .grounded_route import icon_regions
from .image_navigation import ImageNavigator
from ..perception.ground_projection import ProjectionError, finite_pair,measured_uncertainty_px


def _measured_frontier(projection,observed,valid,eligible,pin_distance,pin):
    """Reach observable local ground when the NPC lies outside measured support.

    This is an intermediate observation location, not a claimed route across
    unmeasured terrain. The ordinary route planner still checks every segment.
    """
    from .approach_navigation import ApproachNavigator
    nav=ApproachNavigator(projection.frame.frame_id,observed.ground,observed.obstacles,
        observed.occluded,source='measured_local_frontier',
        forbidden=observed.forbidden|observed.water|~valid,
        uncertainty_px=measured_uncertainty_px(projection))
    x,y=(round(v) for v in projection.frame.player_feet)
    progress=np.linalg.norm(pin)-pin_distance
    for geometry in (nav.known,nav.possible):
        if not geometry.clear[y,x]:continue
        _,labels=cv2.connectedComponents(geometry.clear.astype(np.uint8),connectivity=4)
        # The endpoint itself must be visible ground; explicit occlusion may
        # only make a bounded probe connecting the measured start to that ground.
        candidates=(labels==labels[y,x]) & nav.known.clear & eligible & (progress>=.75)
        if candidates.any():
            best=float(pin_distance[candidates].min())
            return candidates & (pin_distance<=best+.5)
    raise ProjectionError('auto_no_reachable_measured_frontier')


def calibration_probe(observed, anchor, history, *, strafe_verified, anchor_error=3, blocked_keys=()):
    """Select a bounded exploratory pulse in a visible local corridor.

    This is not a metric route: scale is still being measured. Native feedback
    limits displacement, and failed directions are excluded. Camera yaw was
    coupled to player facing by a verified stationary right-mouse orbit.
    """
    h,w = observed.ground.shape
    hard = observed.obstacles | observed.forbidden | observed.water
    possible = (observed.ground | observed.occluded) & ~hard
    nav = ImageNavigator.from_masks('calibration-probe',possible,hard,~possible,
        source='automatic_local_probe',body_radius_px=8,uncertainty_px=math.ceil(anchor_error))
    directions = {'s':(0,1),'w':(0,-1),'e':(1,0),'q':(-1,0)}
    if not strafe_verified:
        raise ProjectionError('auto_strafe_bindings_required')
    counts = {k:sum(row.get('key')==k and row.get('moved',0)>=.15 for row in history) for k in directions}
    blocked = {row['key'] for row in history if row.get('key') in directions and row.get('moved',0)<.15}
    forward = sum(counts[k] for k in ('w','s')); lateral = sum(counts[k] for k in ('q','e'))
    choices=[]
    for key,(dx,dy) in directions.items():
        if key in blocked or key in blocked_keys: continue
        endpoint=(anchor[0]+dx*32,anchor[1]+dy*32)
        if not nav.line_clear(anchor,endpoint): continue
        # Grass over the anchor may occlude it. The corridor beyond it must have
        # positive ground evidence, not just an arbitrary unknown screen region.
        corridor=np.zeros((h,w),np.uint8)
        a=(round(anchor[0]+dx*16),round(anchor[1]+dy*16))
        cv2.line(corridor,a,tuple(round(v) for v in endpoint),1,9)
        selected=corridor.astype(bool)
        if not selected.any() or observed.ground[selected].mean()<.65: continue
        axis_count=forward if key in ('w','s') else lateral
        choices.append((axis_count,counts[key],list(directions).index(key),key,endpoint))
    if not choices: raise ProjectionError('auto_no_observed_probe_corridor')
    *_,key,endpoint=min(choices)
    return dict(kind='calibration_probe',key=key,hold_s=.07,screen_segment=[list(anchor),list(endpoint)],
                metric_route=False,reason='measure_independent_native_motion')


def npc_search_region(projection, observed, rgb, destination, *, visited=()):
    """Use automatic quest-marker/body candidates and a native task search area.

    A yellow icon does not prove NPC identity. Multiple candidates are retained
    as a search set; only native selected-NPC range may finish the approach.
    """
    if not finite_pair(destination) or not all(0<=v<=1 for v in destination):
        raise ProjectionError('auto_npc_search_destination_missing')
    h,w=observed.ground.shape
    yy,xx=np.mgrid[:h,:w]
    coords=np.stack((xx,yy,np.ones_like(xx)),axis=-1)@projection.matrix.T
    valid=abs(coords[:,:,2])>1e-8
    metric=coords[:,:,:2]/np.where(valid,coords[:,:,2],1)[:,:,None]
    pin=np.asarray(projection.metric_delta(destination))
    pin_distance=np.linalg.norm(metric-pin,axis=-1)
    support=np.zeros((h,w),np.uint8)
    cv2.fillConvexPoly(support,np.rint(projection.hull).astype(np.int32),1)
    valid &= support.astype(bool) & (np.linalg.norm(metric,axis=-1)<=8)
    bodies=[]
    for item in observed.instances:
        if 'person' not in item['label'] or item['detection_score']<.3: continue
        x1,y1,x2,y2=item['box']
        # Keep a body extent as an uncertain search cue; do not call its bottom
        # pixel an observed foot or an exact NPC world coordinate.
        for ix,iy,iw,ih in icon_regions(rgb):
            if x1-iw<=ix+iw/2<=x2+iw and y1-ih*3<=iy+ih<=y1+ih:
                bodies.append(((x1+x2)/2,(y1+y2)/2,max(12,(y2-y1)/2)))
                break
    region=np.zeros((h,w),bool)
    for x,y,r in bodies:
        if not projection.contains((x,y)):continue
        center=projection.delta((x,y))
        # Pixel extent is translated through the measured plane only as an
        # uncertain search footprint, not as a claimed collision footprint.
        radius=min(3.,1.+float(np.linalg.norm(projection.delta((x,y+r))-center)))
        region |= np.linalg.norm(metric-center,axis=-1)<=radius
    visual=bool(region.any())
    region &= pin_distance<=10
    if not region.any():
        # Start near the candidate center, then widen after an unsuccessful
        # approach. Otherwise a four-yard disk can repeatedly select a point
        # next to the player on the WRONG side of the fence.
        region=pin_distance<=(4 if visited else 1.5)
        visual=False
    # Do not extrapolate a pixel waypoint for a world target outside the plane.
    outside_support=not bool((region & valid).any())
    eligible=valid & ~observed.water & ~observed.forbidden & ~observed.obstacles
    # Being inside a search area isn't interaction success. Search another
    # nearby standing location until native range says yes or the budget ends.
    for position in [projection.frame.player_position,*visited]:
        d=np.asarray(projection.metric_delta(position))
        eligible &= np.linalg.norm(metric-d,axis=-1)>1.0
    region &= eligible
    frontier=False
    if not region.any() and outside_support:
        region=_measured_frontier(projection,observed,valid,eligible,pin_distance,pin)
        frontier=True
    if not region.any():raise ProjectionError('auto_npc_search_region_exhausted')
    return region,dict(goal_kind='automatic_npc_search_region',
        basis=('measured_local_frontier' if frontier else
               'quest_marker_body_candidates' if visual else 'native_task_search_area'),
        intermediate=frontier,requires_reobservation=frontier,
        candidate_count=len(bodies),identity_confirmed=False,interaction_verified=False,
        manual_regions=0)
