"""Pick reachable local observation locations inside/enclosing a building.

The destination pin ranks no unseen entrance, floor or corridor. This is
incremental exploration of the current measured plane, not full-building SLAM.
"""
import cv2
import numpy as np

from .approach_navigation import ApproachNavigator
from .building_entry import building_entry_region
from .automatic_approach import npc_search_region
from ..perception.ground_projection import ProjectionError, measured_uncertainty_px


def connector_observation_region(projection,observed,scene,*,visited=(),door_related=False):
    """Approach a visible surface boundary on measured floor, from any side.

    Stairs can be viewed from above, below or obliquely. Their lower bounding-box
    edge is not necessarily the entry. Only connected flat floor may be chosen;
    no stair pixel supplies a world endpoint or a flat-plane scale.
    """
    h,w=observed.ground.shape;layout=scene.get('layout',{})
    if (layout.get('schema')!=1 or layout.get('coordinate_space')!='current_image'
            or layout.get('size')!=[w,h]):
        raise ProjectionError('building_connector_approach_unobserved')
    connectors=[c for c in layout.get('connectors',[]) if c.get('kind')=='stairs'
                and c.get('requires_separate_surface_measurement') is True]
    if door_related:
        def related(c):
            a=c['box']
            area=max(1,(a[2]-a[0])*(a[3]-a[1]))
            return any(max(0,min(a[2],b[2])-max(a[0],b[0]))
                       *max(0,min(a[3],b[3])-max(a[1],b[1]))/area>=.25
                       for door in scene.get('doorways',[]) for b in (door['box'],door['threshold_box']))
        connectors=[c for c in connectors if related(c)]
    if not connectors:raise ProjectionError('building_connector_approach_unobserved')
    support=np.zeros((h,w),np.uint8)
    cv2.fillConvexPoly(support,np.rint(projection.hull).astype(np.int32),1)
    support=support.astype(bool)
    geometry=ApproachNavigator(projection.frame.frame_id,observed.ground,observed.obstacles,
        observed.occluded,source='building_connector_observation',
        forbidden=observed.forbidden|observed.water|~support,
        uncertainty_px=measured_uncertainty_px(projection))
    x,y=(round(v) for v in projection.frame.player_feet)
    if not (0<=x<w and 0<=y<h):raise ProjectionError('building_local_origin_outside_frame')
    yy,xx=np.mgrid[:h,:w]
    hom=np.stack((xx,yy,np.ones_like(xx)),axis=-1)@projection.matrix.T
    valid=abs(hom[:,:,2])>1e-8
    metric=hom[:,:,:2]/np.where(valid,hom[:,:,2],1)[:,:,None]
    distance=np.linalg.norm(metric,axis=-1)
    eligible=valid&support&geometry.known.clear&(distance>=.5)&(distance<=2)
    for point in visited:
        eligible &= np.linalg.norm(metric-np.asarray(projection.metric_delta(point)),axis=-1)>.8
    for nav in (geometry.known,geometry.possible):
        if not nav.clear[y,x]:continue
        _,components=cv2.connectedComponents(nav.clear.astype(np.uint8),connectivity=4)
        reachable=eligible&(components==components[y,x])
        choices=[]
        for connector in connectors:
            boundary=np.asarray(connector.get('boundary',[]),float)
            if (boundary.ndim!=2 or boundary.shape[1]!=2 or len(boundary)<3
                    or not np.isfinite(boundary).all() or (boundary<0).any()
                    or (boundary[:,0]>=w).any() or (boundary[:,1]>=h).any()):
                continue  # Legacy boxes remain cues for ordinary observation.
            edge=np.ones((h,w),np.uint8)
            cv2.polylines(edge,[np.rint(boundary).astype(np.int32)],True,0,1)
            gap=cv2.distanceTransform(edge,cv2.DIST_L2,5)
            candidates=reachable&(gap<gap[y,x]-8)
            if not candidates.any():continue
            minimum=float(gap[candidates].min())
            candidates &= gap<=minimum+3
            choices.append((minimum,connector,candidates))
        if choices:
            _,connector,region=min(choices,key=lambda x:x[0])
            return region,dict(goal_kind='building_entry_region',basis='connector_observation_frontier',
                connector_kind=connector['kind'],connector_box=list(connector['box']),
                requires_separate_surface_measurement=True,requires_reobservation=True,
                entrance_verified=False,requires_native_entry_receipt=True,level_id=None)
    raise ProjectionError('building_connector_approach_unobserved')


def local_observation_region(projection, observed, scene, *, visited=(), threshold_origin=None):
    layout = scene.get('layout', {})
    h, w = observed.ground.shape
    if (layout.get('schema') != 1 or layout.get('coordinate_space') != 'current_image'
            or layout.get('size') != [w, h]
            or not (layout.get('floor_regions') or layout.get('exterior_regions'))):
        raise ProjectionError('building_local_surface_unobserved')
    support = np.zeros((h,w), np.uint8)
    cv2.fillConvexPoly(support, np.rint(projection.hull).astype(np.int32), 1)
    support = support.astype(bool)
    geometry = ApproachNavigator(projection.frame.frame_id, observed.ground, observed.obstacles,
        observed.occluded, source='building_local_observation',
        forbidden=observed.forbidden | observed.water | ~support,
        uncertainty_px=measured_uncertainty_px(projection))
    x, y = (round(v) for v in projection.frame.player_feet)
    if not (0 <= x < w and 0 <= y < h):
        raise ProjectionError('building_local_origin_outside_frame')
    yy, xx = np.mgrid[:h,:w]
    hom = np.stack((xx,yy,np.ones_like(xx)),axis=-1) @ projection.matrix.T
    valid = abs(hom[:,:,2]) > 1e-8
    metric = hom[:,:,:2] / np.where(valid,hom[:,:,2],1)[:,:,None]
    distance = np.linalg.norm(metric,axis=-1)
    eligible = valid & support & observed.ground & (distance >= 1) & (distance <= 4)
    if threshold_origin is not None:
        origin_delta = np.asarray(projection.metric_delta(threshold_origin))
        eligible &= (distance <= 2) & (np.linalg.norm(metric-origin_delta,axis=-1) <= 2)
    for point in visited:
        eligible &= np.linalg.norm(metric-np.asarray(projection.metric_delta(point)),axis=-1) > .8
    # Prefer visible standing space, including behind/alongside the avatar.
    # Distance here is MEASURED; screen length is never converted by a guess.
    clearance = cv2.distanceTransform(geometry.known.clear.astype(np.uint8),cv2.DIST_L2,5)
    for nav in (geometry.known, geometry.possible):
        if not nav.clear[y,x]:
            continue
        _, components = cv2.connectedComponents(nav.clear.astype(np.uint8),connectivity=4)
        candidates = eligible & geometry.known.clear & (components == components[y,x])
        if not candidates.any():
            continue
        # Prefer useful displacement and clearance; visible stairs/door cues
        # can break ties without authorizing traversal across their surface.
        score = np.minimum(distance,3.) + np.minimum(clearance,32.)/64
        cues = [] if threshold_origin is not None else layout.get('connectors',[]) + layout.get('portals',[])
        for cue in cues:
            if cue.get('kind') == 'closed':
                continue
            box = cue.get('approach_box')
            if box is None:
                continue
            l,t,r,b = box
            cue_distance = np.hypot(np.maximum(np.maximum(l-xx,xx-r),0),
                                    np.maximum(np.maximum(t-yy,yy-b),0))
            score += .2*np.maximum(0,1-cue_distance/max(w,h))
        score = np.where(candidates,score,-np.inf)
        target_y, target_x = np.unravel_index(np.argmax(score),score.shape)
        region = np.zeros((h,w),bool)
        region[target_y,target_x] = True
        # A small known patch contains planner grid nodes; do not add free space.
        region = cv2.dilate(region.astype(np.uint8),np.ones((7,7),np.uint8)).astype(bool) & candidates
        return region, dict(goal_kind='building_entry_region',basis='local_observation_frontier' if threshold_origin is None else 'threshold_reobservation',
            threshold_origin=threshold_origin,
            entrance_verified=False,requires_reobservation=True,requires_native_entry_receipt=True,
            scene_hints=layout.get('hints',[]),level_id=None,
            connector_pending=bool(layout.get('connectors')))
    raise ProjectionError('building_no_reachable_observation_region')


def _unvisited_portals(projection, scene, passages):
    """Suppress only currently measured views of an already traversed doorway.

    Saved screen boxes are never reused. Both endpoints were native receipts;
    their proximity to a newly observed threshold is evaluated in measured yards.
    """
    doors=[]
    for door in scene.get('doorways',[]):
        l,t,r,b=door['threshold_box']
        point=((l+r)/2,(t+b)/2)
        if not projection.contains(point):
            doors.append(door);continue
        metric=projection.delta(point)
        repeated=False
        for receipt in passages:
            if abs(receipt['height']-projection.frame.height)>.25:continue
            if any(np.linalg.norm(metric-projection.metric_delta(receipt[key]))<1.
                   for key in ('threshold_position','position')):
                repeated=True;break
        if not repeated:doors.append(door)
    return dict(scene,doorways=doors)


def _door_at_threshold(projection, observed, scene, origin):
    """Match a new image of the entrance to the native position reached outside.

    Only the visible, measured floor in the threshold band supplies distance.
    A second doorway in a room may be nearer to the camera after a turn.
    """
    if not scene.get('doorways'):
        raise ProjectionError('building_entrance_unobserved')
    h, w = observed.ground.shape
    hull = np.zeros((h,w), np.uint8)
    cv2.fillConvexPoly(hull, np.rint(projection.hull).astype(np.int32), 1)
    usable = observed.ground & hull.astype(bool) & ~observed.water & ~observed.forbidden & ~observed.obstacles
    origin_delta = np.asarray(projection.metric_delta(origin))
    # The approach hands over within half a yard of the planned threshold.
    tolerance = .5 + projection.validation_error
    matches = []
    for door in scene['doorways']:
        l,t,r,b = door['threshold_box']
        l,r = np.clip([l,r],0,w)
        t,b = np.clip([t,b],0,h)
        ys,xs = np.nonzero(usable[t:b,l:r])
        if not len(xs):
            continue
        points = np.column_stack((xs+l,ys+t,np.ones(len(xs)))) @ projection.matrix.T
        points = points[np.abs(points[:,2]) > 1e-8]
        if len(points):
            distance = np.linalg.norm(points[:,:2]/points[:,2,None]-origin_delta,axis=1)
            if distance.min() <= tolerance:
                matches.append(door)
    if len(matches) != 1:
        raise ProjectionError('building_threshold_identity_unconfirmed')
    return dict(scene, doorways=matches)


def _connected_search(projection, observed, rgb, destination, visited):
    region,goal=npc_search_region(projection,observed,rgb,destination,visited=visited)
    h,w=observed.ground.shape
    support=np.zeros((h,w),np.uint8)
    cv2.fillConvexPoly(support,np.rint(projection.hull).astype(np.int32),1)
    geometry=ApproachNavigator(projection.frame.frame_id,observed.ground,observed.obstacles,
        observed.occluded,source='building_target_search',
        forbidden=observed.water|observed.forbidden|~support.astype(bool),
        uncertainty_px=measured_uncertainty_px(projection))
    x,y=(round(v) for v in projection.frame.player_feet)
    if not (0<=x<w and 0<=y<h):raise ProjectionError('building_local_origin_outside_frame')
    for nav in (geometry.known,geometry.possible):
        if not nav.clear[y,x]:continue
        _,components=cv2.connectedComponents(nav.clear.astype(np.uint8),connectivity=4)
        reachable=region & geometry.known.clear & (components==components[y,x])
        if reachable.any():
            return reachable,dict(goal,goal_kind='building_entry_region',
                basis='building_task_search_region',source_basis=goal['basis'],
                entrance_verified=False,requires_reobservation=True,
                requires_native_entry_receipt=True,level_id=None)
    raise ProjectionError('building_task_region_disconnected')


def _building_navigation_region(projection, observed, rgb, destination, scene, *, crossing=False,
                               searching=False, visited=(), passages=(), crossing_origin=None):
    """Try a current door; otherwise relocate on a measured connected surface."""
    if crossing and crossing_origin is not None:
        try:
            scene = _door_at_threshold(projection, observed, scene, crossing_origin)
        except ProjectionError as exc:
            if str(exc) not in {'building_entrance_unobserved','building_threshold_identity_unconfirmed'}:
                raise
            if not scene.get('layout'):
                raise
            return local_observation_region(projection, observed, scene, visited=visited,
                                            threshold_origin=crossing_origin)
    if searching:
        # The pin can rank a locally CONNECTED search area after entry, but
        # never confirms its floor or task arrival. A wall sends us back to
        # current-frame portals/frontiers instead of direct steering at XY.
        try:
            return _connected_search(projection,observed,rgb,destination,visited)
        except ProjectionError as exc:
            if str(exc) not in {'auto_no_reachable_measured_frontier','auto_npc_search_region_exhausted',
                                'building_task_region_disconnected'}:raise
        scene=_unvisited_portals(projection,scene,passages)
    try:
        result=building_entry_region(projection,observed,rgb,destination,scene,
                                     crossing=crossing,visited=visited,crossing_origin=crossing_origin)
        if result[1]['basis']=='door_floor_observation':
            try:
                return connector_observation_region(projection,observed,scene,visited=visited,door_related=True)
            except ProjectionError as exc:
                if str(exc)!='building_connector_approach_unobserved':raise
        return result
    except ProjectionError as exc:
        if str(exc) in {'building_entrance_unobserved','building_door_approach_unconfirmed',
                        'building_door_floor_unobserved'}:
            try:
                return connector_observation_region(projection,observed,scene,visited=visited,
                                                     door_related=crossing or bool(scene.get('doorways')))
            except ProjectionError as connector_error:
                if str(connector_error)!='building_connector_approach_unobserved':raise
            if crossing and crossing_origin is not None and scene.get('layout'):
                return local_observation_region(projection, observed, scene, visited=visited,
                                                threshold_origin=crossing_origin)
        if crossing or str(exc) not in {'building_entrance_unobserved',
                                        'building_door_approach_unconfirmed'}:
            raise
        if not scene.get('layout'):
            raise  # Old-format observations retain their explicit capability limit.
        return local_observation_region(projection,observed,scene,visited=visited)


def _return_edge(context):
    if not isinstance(context,dict) or context.get('schema')!=1:return None
    edge=context.get('return_edge')
    if (not isinstance(edge,dict) or type(edge.get('passage_id')) is not int
            or edge.get('to_area')!=context.get('current_area') or edge.get('returned') is not False):
        return None
    from ..perception.ground_projection import finite_pair
    if not all(finite_pair(edge.get(k)) for k in ('threshold_position','position')):return None
    return edge


def _return_approach(projection,observed,rgb,scene,edge,visited):
    """Re-find the actual exit in this image; recorded screen boxes are unused."""
    matched=[]
    for door in scene.get('doorways',[]):
        for endpoint in (edge['position'],edge['threshold_position']):
            try:
                _door_at_threshold(projection,observed,dict(scene,doorways=[door]),endpoint)
                matched.append(door);break
            except ProjectionError:
                pass
    if len(matched)>1:raise ProjectionError('building_return_entrance_ambiguous')
    if matched:
        return building_entry_region(projection,observed,rgb,edge['position'],
            dict(scene,doorways=matched),visited=())
    # The old exit position can rank a newly measured local approach, never
    # supply an unseen corridor, a floor identity, or a crossing by itself.
    if np.linalg.norm(projection.metric_delta(edge['position']))>1.:
        try:
            region,goal=_connected_search(projection,observed,rgb,edge['position'],visited)
            return region,dict(goal,basis='return_observation_frontier',requires_reobservation=True)
        except ProjectionError as exc:
            if str(exc) not in {'auto_no_reachable_measured_frontier','auto_npc_search_region_exhausted',
                                'building_task_region_disconnected'}:raise
    raise ProjectionError('building_return_entrance_unobserved')


def building_navigation_region(projection, observed, rgb, destination, scene, *, crossing=False,
                               searching=False, visited=(), passages=(), crossing_origin=None,
                               navigation_context=None, returning=None):
    """Explore unvisited local space, then re-observe a measured parent passage."""
    edge=_return_edge(navigation_context)
    args=dict(crossing=crossing,searching=searching,visited=visited,passages=passages,
              crossing_origin=crossing_origin)
    if returning is not None and (edge is None or returning!=edge['passage_id']):
        raise ProjectionError('building_return_identity_changed')
    if returning is None:
        try:
            return _building_navigation_region(projection,observed,rgb,destination,scene,**args)
        except ProjectionError as exc:
            if (not searching or edge is None or str(exc) not in {
                    'building_no_reachable_observation_region','building_entrance_unobserved',
                    'building_door_approach_unconfirmed'}):raise
    if crossing:
        # The normal crossing still needs a re-observed door and measured floor.
        # Restrict its destination to the previous side of the measured passage.
        region,goal=_building_navigation_region(projection,observed,rgb,destination,scene,**args)
        if goal['basis']=='door_floor_crossing':
            yy,xx=np.mgrid[:region.shape[0],:region.shape[1]]
            hom=np.stack((xx,yy,np.ones_like(xx)),axis=-1)@projection.matrix.T
            valid=np.abs(hom[:,:,2])>1e-8
            metric=hom[:,:,:2]/np.where(valid,hom[:,:,2],1)[:,:,None]
            a,b=[np.asarray(projection.metric_delta(edge[k])) for k in ('threshold_position','position')]
            region &= valid & (np.linalg.norm(metric-a,axis=-1)+.15<=np.linalg.norm(metric-b,axis=-1))
            if not region.any():raise ProjectionError('building_return_side_unobserved')
    else:
        region,goal=_return_approach(projection,observed,rgb,scene,edge,visited)
    return region,dict(goal,return_passage_id=edge['passage_id'],
                       from_area=edge['to_area'],to_area=edge['from_area'],level_id=None)
