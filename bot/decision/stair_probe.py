"""Current-image stair trials; never convert stair pixels with a floor plane."""
import math

import cv2
import numpy as np

from .image_navigation import ImageNavigator, ImageRouteError
from ..perception.building_layout import stair_evidence, semantic_region, BARRIER_LABELS
from ..perception.ground_projection import ProjectionError, finite_pair


DIRECTIONS = {'w': (0, -1), 's': (0, 1), 'q': (-1, 0), 'e': (1, 0)}


def observe_stair_trial(observed, names, anchor, *, advance=None, moved=0., anchor_error=3.):
    """Choose one cardinal trial in a locally observed corridor.

    The caller must first confirm camera/player alignment and a stationary
    measured viewport anchor. The suggested key has no promised metric endpoint.
    A new image and two native receipts are required after every emitted pulse.
    """
    if (not finite_pair(anchor) or not math.isfinite(anchor_error) or not 0 <= anchor_error <= 5
            or advance is not None and advance not in DIRECTIONS):
        raise ProjectionError('stair_trial_anchor_invalid')
    h, w = observed.ground.shape
    x, y = (round(v) for v in anchor)
    if not (0 <= x < w and 0 <= y < h):
        raise ProjectionError('stair_trial_anchor_invalid')
    stairs = stair_evidence(observed, names)
    hard = (observed.obstacles | observed.forbidden | observed.water
            | semantic_region(observed, names, BARRIER_LABELS))
    stairs &= ~hard
    possible = (stairs | observed.ground | observed.occluded) & ~hard
    geometry = ImageNavigator.from_masks('stair-trial', possible, hard, ~possible,
        source='observed_stair_trial', body_radius_px=8, uncertainty_px=math.ceil(anchor_error))
    yy, xx = np.mgrid[:h, :w]
    _, components, stats, _ = cv2.connectedComponentsWithStats(stairs.astype(np.uint8), connectivity=4)
    nearby = []
    for i in range(1, len(stats)):
        cy, cx = np.nonzero(components == i)
        gap = float(np.min(np.hypot(cx-x, cy-y)))
        if gap <= 64 and stats[i, cv2.CC_STAT_AREA] >= 64:
            nearby.append((gap, i, float(np.mean(cx)), float(np.mean(cy))))
    nearby.sort()
    if advance is None:
        if not nearby or len(nearby) > 1 and nearby[1][0]-nearby[0][0] < 12:
            raise ProjectionError('stair_trial_connector_ambiguous')
        _, _, cx, cy = nearby[0]
        dx, dy = cx-x, cy-y
        if math.hypot(dx, dy) < 12:
            raise ProjectionError('stair_trial_entry_direction_unknown')
        advance = max(DIRECTIONS, key=lambda k: dx*DIRECTIONS[k][0]+dy*DIRECTIONS[k][1])
    dx, dy = DIRECTIONS[advance]
    along = (xx-x)*dx+(yy-y)*dy
    across = np.abs((xx-x)*dy-(yy-y)*dx)
    front = (along >= 16) & (along <= 40) & (across <= 10)
    # This is a candidate for NEW measurement, not proof of a landing/floor.
    if (moved >= .4 and front.any() and observed.ground[front].mean() >= .85
            and not (hard[front] | stairs[front]).any()
            and geometry.line_clear(anchor, (x+dx*40, y+dy*40))
            and not (stairs & (along >= -12) & (along <= 16) & (across <= 12)).any()):
        return dict(kind='stair_surface_candidate', advance=advance, anchor=list(anchor),
                    requires_new_measurement=True, level_id=None, metric_route=False)
    if not nearby:
        raise ProjectionError('stair_trial_connector_unobserved')
    # The current component supplies the next local direction; old boxes never
    # override a new railing, a wall, water or a missing connection.
    selected = components == nearby[0][1]
    far = float(np.max(along[selected]))
    if far < 0:
        goal = geometry.clear & observed.ground & (along >= 24) & (along <= 40) & (across <= 20)
    else:
        goal = geometry.clear & (selected | observed.ground) & (along >= max(24, far-20)) & (along <= far+24)
        lateral = float(np.max(across[selected]))
        goal &= across <= max(12, lateral-12)
    clearance = cv2.distanceTransform(geometry.clear.astype(np.uint8), cv2.DIST_L2, 5)
    candidates = np.argwhere(goal)
    if not len(candidates):
        raise ProjectionError('stair_trial_no_observed_corridor')
    scores = clearance[goal]-.2*across[goal]
    cy, cx = candidates[int(np.argmax(scores))]
    try:
        route = geometry.plan(tuple(anchor), (int(cx), int(cy)))
    except ImageRouteError as exc:
        raise ProjectionError('stair_trial_no_observed_corridor') from exc
    target = next((p for p in route.points[1:] if math.hypot(p[0]-x, p[1]-y) >= 12), None)
    if target is None:
        raise ProjectionError('stair_trial_no_observed_corridor')
    keys = sorted(DIRECTIONS, key=lambda k: (target[0]-x)*DIRECTIONS[k][0]+(target[1]-y)*DIRECTIONS[k][1], reverse=True)
    for key in keys:
        kx, ky = DIRECTIONS[key]
        if (target[0]-x)*kx+(target[1]-y)*ky <= 0:
            continue
        endpoint = (x+kx*32, y+ky*32)
        if not geometry.line_clear(anchor, endpoint):
            continue
        corridor = np.zeros((h, w), np.uint8)
        cv2.line(corridor, (x+kx*16, y+ky*16), endpoint, 1, 9)
        local = corridor.astype(bool)
        if (stairs | observed.ground)[local].mean() < .8:
            continue
        return dict(kind='stair_probe', key=key, hold_s=.04, advance=advance,
            anchor=list(anchor), screen_segment=[list(anchor), list(endpoint)],
            metric_route=False, requires_new_measurement=True, level_id=None,
            basis='local_stair_corridor_trial')
    raise ProjectionError('stair_trial_no_observed_corridor')
