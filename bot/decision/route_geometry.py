"""Independent consistency checks for image paths and goal-relative metric paths.

These checks do NOT certify model-detected obstacles as actual game geometry.
"""
import math


def signed_area(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def simple_polygon(poly):
    if len(set(map(tuple,poly))) != len(poly):
        return False
    area=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(poly,poly[1:]+poly[:1]))
    if abs(area)<1e-6:
        return False
    edges=list(zip(poly,poly[1:]+poly[:1]))
    for i,(a,b) in enumerate(edges):
        for j,(c,d) in enumerate(edges):
            if j<=i or j==i+1 or (i==0 and j==len(edges)-1):
                continue
            # Non-adjacent touching is also not a valid simple obstacle boundary.
            if segment_distance(a,b,c,d)<1e-8:
                return False
    return True


def point_segment_distance(p,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    n=dx*dx+dy*dy
    t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/n)) if n else 0
    return math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)


def segment_distance(a,b,c,d):
    if signed_area(a,b,c)*signed_area(a,b,d)<0 and signed_area(c,d,a)*signed_area(c,d,b)<0:
        return 0.0
    return min(point_segment_distance(a,c,d),point_segment_distance(b,c,d),
               point_segment_distance(c,a,b),point_segment_distance(d,a,b))


def clearance_departure(a,b,poly,radius):
    """Allow only an outward first leg when the start already lacks padding.

    The centre must remain outside the solid boundary. Distance from every edge
    must never decrease, and the endpoint must restore the full body clearance.
    This never authorizes sliding alongside a wall inside the margin.
    """
    from .route_planner import segment_hits_polygon
    if segment_hits_polygon(a,b,poly):return False
    edges=list(zip(poly,poly[1:]+poly[:1]))
    distance=lambda p:min(point_segment_distance(p,c,d) for c,d in edges)
    initial=distance(a)
    if not 0<initial<radius or distance(b)<radius-1e-3:return False
    length=math.dist(a,b)
    if length<2:return False
    previous=initial
    for step in range(1,9):
        t=step/8;p=[a[j]+(b[j]-a[j])*t for j in (0,1)]
        current=distance(p)
        if current-previous<length/8*.7:return False
        previous=current
    return True


def image_metric_error(plan):
    """A planar camera projection preserves which side of the start-goal line a point lies on.

    Image y increases downward. A positive cross(goal-start, waypoint-start)
    therefore corresponds to the RIGHT side in the goal-aligned ground frame.
    This uses topology, not an unjustified pixels-to-yards scale.
    """
    start,goal=plan['player_feet'],plan['goal_feet']
    length=math.dist(start,goal)
    if length<20:
        return 'route_goal_image_scale_too_small'
    for index,wp in enumerate(plan['waypoints']):
        lateral=signed_area(start,goal,wp['image_point'])/length
        right=wp['right_yards']
        # Keep a tolerance around the goal line for pixel localization noise.
        if abs(lateral)>15 and abs(right)>.35 and lateral*right<0:
            return f'route_image_world_side_mismatch_{index}'
        if abs(lateral)>45 and abs(right)<.05:
            return f'route_image_detour_world_straight_{index}'
    if math.dist(plan['waypoints'][-1]['image_point'],goal)>70:
        return 'route_image_endpoint_far_from_goal'
    return None


def clearance_error(plan, size, radius_px=8):
    """Check the footprint of the moving character, not just a zero-width line."""
    w,h=size
    xy=lambda p:(p[0]*w/1000,p[1]*h/1000)
    path=list(map(xy,[plan['player_feet']]+[p['image_point'] for p in plan['waypoints']]))
    for index,(a,b) in enumerate(zip(path,path[1:])):
        for obstacle in plan['obstacles']:
            poly=list(map(xy,obstacle['polygon']))
            if min(segment_distance(a,b,c,d) for c,d in zip(poly,poly[1:]+poly[:1]))<radius_px-1e-3 and not (
                    index==0 and clearance_departure(a,b,poly,radius_px)):
                return f'route_clearance_insufficient_segment_{index}'
    return None
