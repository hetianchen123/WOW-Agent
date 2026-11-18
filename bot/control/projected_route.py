"""Segmentation geometry -> local search -> measured map points; no model or I/O."""
from dataclasses import asdict,replace
import math

from ..decision.image_navigation import ImageNavigator
from ..perception.ground_projection import ProjectionError,measured_uncertainty_px
from .navigation import compass_from_delta, heading_error


def plan_projected_route(projection, scene, goal, current, *, now, convention='ccw_west'):
    if convention not in {'ccw_west', 'cw_east'}:
        raise ProjectionError('projection_heading_convention_invalid')
    if scene.frame_id != projection.frame.frame_id or scene.size != projection.frame.size:
        raise ProjectionError('projection_scene_mismatch')
    projection.validate_current(current, now=now)
    image_route = ImageNavigator(scene).plan(projection.frame.player_feet, goal)
    world_route = projection.project_route(image_route, current, now=now)
    segments = []
    heading = current.heading
    for a, b in zip(world_route.world_points, world_route.world_points[1:]):
        dx, dy = [(y-x)*s for x, y, s in zip(a, b, current.world_size)]
        target = compass_from_delta(dx, dy, convention)
        segments.append({'from': a, 'to': b, 'yards': math.hypot(dx, dy),
                         'heading_radians': target, 'turn_radians': heading_error(heading, target)})
        heading = target
    return {'image_route': asdict(image_route), 'world_route': asdict(world_route),
            'segments': segments, 'heading_convention': convention, 'executable': False,
            'world_collision_verified': False}


def interaction_region(projection, target_world, radius_yards, *, position_error_yards=0):
    """Project a measured candidate approach radius, never a screen-pixel radius.

    This region does not assert line of sight, target identity or interaction.
    Caller must supply an actual target-position estimate, not a quest map pin.
    """
    import numpy as np
    from ..perception.ground_projection import finite_pair
    if (not finite_pair(target_world) or not all(0 <= v <= 1 for v in target_world)
            or not math.isfinite(radius_yards) or not math.isfinite(position_error_yards)
            or not 0 <= position_error_yards < radius_yards <= 10):
        raise ProjectionError('approach_target_region_invalid')
    w,h = projection.frame.size
    yy,xx = np.mgrid[:h,:w]
    pixels = np.stack((xx,yy,np.ones_like(xx)),axis=-1)
    values = pixels @ projection.matrix.T
    denominator = values[:,:,2]
    valid = np.abs(denominator) > 1e-8
    delta = values[:,:,:2]/np.where(valid,denominator,1)[:,:,None]
    target = np.asarray(projection.metric_delta(target_world))
    region = valid & (np.linalg.norm(delta-target,axis=-1) <= radius_yards-position_error_yards)
    import cv2
    support = np.zeros((h,w),np.uint8)
    cv2.fillConvexPoly(support,np.rint(projection.hull).astype(np.int32),1)
    return region & support.astype(bool)


def plan_segmented_approach(projection, segmented, goal_region, current, *, now,
                            frame_id, image_sha256, allow_probe=True, convention='ccw_west', max_step_yards=None):
    """Local masks -> candidate region -> bounded measured probe, no game inputs.

    The start comes from calibrated native position, not a ground-class pixel.
    Unknown traversal stops after at most 0.75 yards and needs fresh observations.
    """
    import numpy as np
    from ..decision.approach_navigation import ApproachNavigator
    if convention not in {'ccw_west','cw_east'}:
        raise ProjectionError('projection_heading_convention_invalid')
    projection.validate_current(current, now=now)
    frame = projection.frame
    if (frame_id != frame.frame_id or image_sha256 != frame.image_sha256
            or segmented.ground.shape != (frame.size[1],frame.size[0])):
        raise ProjectionError('projection_scene_mismatch')
    # Clip planning to measured support before searching, not after a long detour.
    import cv2
    support = np.zeros(segmented.ground.shape,np.uint8)
    cv2.fillConvexPoly(support,np.rint(projection.hull).astype(np.int32),1)
    forbidden = segmented.forbidden | segmented.water | ~support.astype(bool)
    uncertainty=measured_uncertainty_px(projection)
    nav = ApproachNavigator(frame_id,segmented.ground,segmented.obstacles,segmented.occluded,
                            source='local_segmentation_candidate',forbidden=forbidden,
                            uncertainty_px=uncertainty)
    # Inverse measured transform locates native position even under grass/effects.
    native = [*projection.metric_delta(current.player_position),1]
    anchor = np.linalg.solve(projection.matrix,native)
    start = tuple(float(v) for v in anchor[:2]/anchor[2])
    if math.dist(start,frame.player_feet)>2:
        raise ProjectionError('projection_route_start_changed')
    x,y = (int(v) for v in start)
    if segmented.water[y,x]:
        raise ProjectionError('approach_water_mode_required')
    plan = nav.plan(start, goal_region, anchor_confirmed=True, allow_probe=allow_probe)
    # Remove tiny grid turns only when the full segment passes the SAME inflated
    # obstacle/unknown checks. A short keyboard pulse must not overshoot a tiny
    # first grid edge and accidentally cut the next corner.
    geometry=nav.possible if plan.requires_probe else nav.known
    points=list(plan.route.points)
    cursor=1
    while cursor+1<len(points) and geometry.line_clear(points[0],points[cursor+1]):
        cursor+=1
    if cursor>1:
        points=[points[0],*points[cursor:]]
        geometry.validate(points)
        plan=replace(plan,route=replace(plan.route,points=tuple(points),
            length_px=sum(math.dist(a,b) for a,b in zip(points,points[1:]))))
    world = projection.project_route(plan.route,current,now=now)
    # Retain complete candidate but export only the short measured prefix for review.
    limit = .75 if plan.requires_probe else 2.0
    if max_step_yards is not None:
        if not math.isfinite(max_step_yards) or not 0 < max_step_yards <= 2:
            raise ProjectionError('approach_step_budget_invalid')
        limit = min(limit,max_step_yards)
    prefix = [world.world_points[0]]
    remaining = limit
    for a,b in zip(world.world_points,world.world_points[1:]):
        distance = math.hypot(*((y-x)*s for x,y,s in zip(a,b,frame.world_size)))
        if distance <= remaining:
            prefix.append(b); remaining -= distance
        else:
            prefix.append(tuple(x+(y-x)*remaining/distance for x,y in zip(a,b)))
            break
    commands = []
    heading = current.heading
    for a,b in zip(prefix,prefix[1:]):
        dx,dy = [(y-x)*s for x,y,s in zip(a,b,frame.world_size)]
        if math.hypot(dx,dy)<1e-8:
            continue
        desired = compass_from_delta(dx,dy,convention)
        commands.append(dict(to=b,yards=math.hypot(dx,dy),heading_radians=desired,
                             turn_radians=heading_error(heading,desired)))
        heading = desired
    return dict(approach=asdict(plan),world_route=asdict(world),next_segment_world=prefix,next_segments=commands,
        movement_budget_yards=limit,requires_fresh_feedback=True,arrival_confirmed=False,
        executable=False,world_collision_verified=False,
        next_step=('check_interaction_range' if not commands else
                   'probe_then_reobserve' if plan.requires_probe else 'approach_then_check_range'))
