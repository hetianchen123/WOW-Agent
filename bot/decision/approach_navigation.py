"""Approach a region using separate localization and traversal evidence.

Occlusion may produce a PROBE candidate, never confirmed walkability. Unknown
terrain, UI and explicit obstacles are not opened around an avatar anchor.
All results remain image-space, non-executable candidates.
"""
from dataclasses import dataclass
import heapq
import math
import time

import numpy as np

from .image_navigation import ImageNavigator, ImageRoute, ImageRouteError, _point


@dataclass(frozen=True)
class ApproachPlan:
    route: ImageRoute
    requires_probe: bool
    goal_kind: str = 'interaction_candidate_region'
    arrival_confirmed: bool = False
    executable: bool = False


@dataclass(frozen=True)
class TraversalEvidence:
    frame_id: str
    mask: np.ndarray
    observed_at: float
    source: str  # Registered into THIS image; raw world/screen points are not masks.


class ApproachNavigator:
    def __init__(self, frame_id, ground, obstacles, occluded, *, source,
                 traversed=None, forbidden=None, now=None, body_radius_px=8, uncertainty_px=4, cell_px=3):
        if traversed is not None:
            if (not isinstance(traversed, TraversalEvidence) or traversed.frame_id != frame_id
                    or traversed.source not in {'registered_native_trace','synthetic_regression'}
                    or now is None or not 0 <= now-traversed.observed_at <= 2):
                raise ImageRouteError('approach_trace_unregistered_or_stale')
            traversed = traversed.mask
        arrays = [ground, obstacles, occluded]
        arrays += [a for a in (traversed, forbidden) if a is not None]
        if any(not isinstance(a, np.ndarray) or a.dtype != bool or a.shape != ground.shape
               or a.ndim != 2 for a in arrays):
            raise ImageRouteError('approach_mask_invalid')
        zero = np.zeros_like(ground)
        forbidden = zero if forbidden is None else forbidden
        traversed = zero if traversed is None else traversed
        hard = obstacles | forbidden
        # Recent traversed evidence is allowed to resolve occlusion, never new solids.
        known = (ground | traversed) & ~hard
        possible = (known | occluded) & ~hard
        options = dict(source=source, body_radius_px=body_radius_px,
                       uncertainty_px=uncertainty_px, cell_px=cell_px)
        self.known = ImageNavigator.from_masks(frame_id, known, hard, ~known, **options)
        self.possible = ImageNavigator.from_masks(frame_id, possible, hard, ~possible, **options)

    def plan(self, start, goal_region, *, anchor_confirmed, allow_probe=True,
             max_nodes=80_000, timeout_s=2, cancelled=lambda: False):
        began = time.monotonic()
        if anchor_confirmed is not True:
            raise ImageRouteError('approach_anchor_unconfirmed')
        if (not isinstance(goal_region, np.ndarray) or goal_region.dtype != bool
                or goal_region.shape != self.possible.clear.shape or not goal_region.any()):
            raise ImageRouteError('approach_goal_region_invalid')
        if not _point(start, self.possible.scene.size):
            raise ImageRouteError('approach_start_outside_frame')
        start = tuple(start)
        def guard():
            if cancelled():
                raise ImageRouteError('image_route_cancelled')
            if time.monotonic()-began >= timeout_s:
                raise ImageRouteError('image_route_time_budget')
        guard()
        # Search proven/visual ground first. Only then consider explicit occlusion.
        expanded = 0
        for nav, probing in [(self.known, False), (self.possible, True)]:
            if probing and not allow_probe:
                break
            if not nav.line_clear(start, start):
                continue
            sx, sy = (round(v) for v in start)
            if goal_region[sy, sx]:
                points = (start, start)
                return self._result(nav, points, probing, expanded, began)
            ends = goal_region[np.ix_(nav.ys, nav.xs)] & nav.nodes
            if not ends.any():
                continue
            ey,ex = np.nonzero(ends)
            left,right = int(nav.xs[ex.min()]),int(nav.xs[ex.max()])
            top,bottom = int(nav.ys[ey.min()]),int(nav.ys[ey.max()])
            def heuristic(node):
                x,y = nav._xy(node)
                return math.hypot(max(left-x,0,x-right),max(top-y,0,y-bottom))
            costs = nav._links(start)
            previous = {node: None for node in costs}
            queue = [(cost+heuristic(node), cost, node) for node, cost in costs.items()]
            heapq.heapify(queue)
            reached = None
            while queue:
                _, cost, node = heapq.heappop(queue)
                if cost != costs[node]:
                    continue
                expanded += 1
                if expanded > max_nodes:
                    raise ImageRouteError('image_route_node_budget')
                if expanded % 32 == 1:
                    guard()
                x, y = node
                if ends[y, x]:
                    reached = node
                    break
                for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                    nx,ny = x+dx,y+dy
                    if not (0 <= nx < len(nav.xs) and 0 <= ny < len(nav.ys)) or not nav.nodes[ny,nx]:
                        continue
                    if dx and dy and (not nav.nodes[y,nx] or not nav.nodes[ny,x]):
                        continue
                    nxt = nx,ny
                    a,b = nav._xy(node),nav._xy(nxt)
                    if not nav.line_clear(a,b):
                        continue
                    penalty = 5 if probing and not self.known.line_clear(a,b) else 1
                    new_cost = cost + math.hypot(dx,dy)*nav.cell*penalty
                    if new_cost < costs.get(nxt, math.inf):
                        costs[nxt],previous[nxt] = new_cost,node
                        heapq.heappush(queue,(new_cost+heuristic(nxt),new_cost,nxt))
            if reached is not None:
                path = []
                while reached is not None:
                    path.append(nav._xy(reached)); reached = previous[reached]
                path.append(start); path.reverse()
                # Remove only collinear points: no shortcuts across unknown or costs.
                points = [path[0]]
                for i in range(1,len(path)-1):
                    a,b,c = points[-1],path[i],path[i+1]
                    if (b[0]-a[0])*(c[1]-b[1]) != (b[1]-a[1])*(c[0]-b[0]):
                        points.append(b)
                points.append(path[-1])
                nav.validate(points); guard()
                return self._result(nav, tuple(points), probing, expanded, began)
        guard()
        reason = ('approach_start_needs_other_evidence' if not self.possible.line_clear(start,start)
                  else 'approach_no_supported_connection')
        raise ImageRouteError(reason)

    def _result(self, nav, points, probing, expanded, began):
        route = ImageRoute(nav.scene.frame_id, tuple(points),
            sum(math.dist(a,b) for a,b in zip(points,points[1:])), nav.radius,
            expanded, (time.monotonic()-began)*1000, nav.scene.source)
        return ApproachPlan(route, probing)
