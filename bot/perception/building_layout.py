"""Local building structure, separate from the surface supporting the player.

All regions belong to one exposure. Room/floor identity and passage success
require native motion and a new observation; a semantic label cannot supply them.
"""
import cv2
import numpy as np


STAIR_LABELS = {'stairs', 'stairway', 'step', 'steps', 'escalator'}
BARRIER_LABELS = {'wall', 'column', 'fence', 'railing', 'banister', 'balustrade',
                  'window', 'windowpane', 'cabinet', 'wardrobe', 'bookcase',
                  'table', 'desk', 'counter', 'bed', 'sofa', 'chest of drawers','shelf','bannister'}
FLOOR_LABELS = {'floor', 'rug', 'carpet'}
OUTSIDE_LABELS = {'grass', 'road', 'earth', 'sidewalk', 'field', 'path', 'land'}


def doorway_floor_box(door):
    """Interior observation band, excluding the detection's foreground sill.

    A detection box often includes the bottom of the jamb/front molding. Its
    lowest pixels cannot by themselves prove that a target is past the opening.
    This image band remains a candidate, never a world-space doorway plane.
    """
    l,t,r,b=door['box'];width=r-l;height=b-t
    return [round(l+.2*width),round(t+.65*height),round(r-.2*width),round(t+.85*height)]


def semantic_region(observed, names, words, threshold=.65):
    ids = [int(k) for k, value in names.items() if str(value).strip().lower() in words]
    return np.isin(observed.semantic_labels, ids) & (observed.semantic_confidence >= threshold)


def stair_evidence(observed, names):
    """A detected staircase cannot be silently projected as flat semantic floor.

    Instance evidence reserves a separate surface; it does not authorize stair
    traversal or establish slope/level. Ambiguous stair masks only reduce the
    available flat plane, retaining the original raw semantic observation.
    """
    result=semantic_region(observed,names,STAIR_LABELS)
    for item in observed.instances:
        mask=item.get('mask')
        if (item['label'].strip().lower() in STAIR_LABELS
                and item.get('detection_score',0)>=.25 and item.get('mask_score',0)>=.85
                and isinstance(mask,np.ndarray) and mask.shape==result.shape and mask.dtype==bool):
            result |= mask
    return result


def door_state(item, labels, confidence, names):
    """Grounded-DINO phrases can overlap: 'closed door' may be an open stairway.

    Require door-panel semantics to call it closed. A stair/floor observation
    can propose an opening, but still needs surrounding structure downstream.
    """
    label=item['label'].strip().lower()
    if not any(word in label for word in ('door','entrance','gate')):
        return None
    h,w=labels.shape
    box=np.asarray(item['box'],float)
    if box.shape!=(4,) or not np.isfinite(box).all():return 'unknown'
    l,t,r,b=np.rint(box).astype(int)
    l,r=max(0,l),min(w,r);t,b=max(0,t),min(h,b)
    if l>=r or t>=b:return 'unknown'
    ids={int(k):str(v).strip().lower() for k,v in names.items()}
    region=np.zeros((h,w),bool);region[t:b,l:r]=True
    # The box interior, not the jamb-shaped SAM mask, supplies opening evidence.
    visible=region & (confidence>=.65)
    if not visible.any():return 'unknown'
    opening=np.isin(labels,[k for k,v in ids.items() if v in FLOOR_LABELS|OUTSIDE_LABELS|STAIR_LABELS])
    panel=np.isin(labels,[k for k,v in ids.items() if v in {'door','screen door'}])
    opening_share=float(opening[visible].mean())
    panel_share=float(panel[visible].mean())
    if ('closed' in label or 'locked' in label) and panel_share>=.5 and opening_share<.08:
        return 'closed'
    if opening_share>=.08 or any(word in label for word in ('open','entrance','doorway')):
        return 'opening'
    return 'unknown'


def regions(mask, *, minimum=16):
    """Keep independent islands. No closing holes or joining different floors."""
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=4)
    result = []
    for x, y, w, h, area in stats[1:]:
        if area >= minimum:
            result.append(dict(box=[int(x), int(y), int(x+w), int(y+h)], pixels=int(area)))
    return sorted(result, key=lambda r: r['pixels'], reverse=True)[:32]


def building_layout(observed, names, *, doors=()):
    """Describe local candidates without requiring a large visible-ground ratio.

    A door is a portal cue; stairs connect surfaces that must be measured
    separately. Their projected overlap with floor never makes them coplanar.
    """
    shape = observed.semantic_labels.shape
    empty = np.zeros(shape, bool)
    excluded = getattr(observed, 'forbidden', empty) | observed.water
    solid = semantic_region(observed, names, BARRIER_LABELS)
    stairs = stair_evidence(observed, names)
    floors = semantic_region(observed, names, FLOOR_LABELS) & ~excluded & ~solid & ~stairs
    outside = semantic_region(observed, names, OUTSIDE_LABELS) & ~excluded & ~solid & ~stairs
    ceiling = semantic_region(observed, names, {'ceiling'})
    portals = [dict(kind='opening', box=list(d['box']), approach_box=list(d['threshold_box']),
                    opening_observed=True, passage_verified=False) for d in doors]
    # Closed or unresolved doors are retained for observation/interaction, not
    # treated as free space because an open-door detector also returned a box.
    for item in observed.instances:
        label = item['label'].strip().lower()
        if item.get('detection_score', 0) < .3 or item.get('mask_score', 0) < .85:
            continue
        state=door_state(item,observed.semantic_labels,observed.semantic_confidence,names)
        if state=='closed':
            portals.append(dict(kind='closed', box=list(item['box']),
                                opening_observed=False, passage_verified=False))
    connectors = []
    _, stair_parts, stats, _ = cv2.connectedComponentsWithStats((stairs & ~excluded).astype(np.uint8),connectivity=4)
    ordered=sorted(range(1,len(stats)),key=lambda i:int(stats[i,cv2.CC_STAT_AREA]),reverse=True)[:32]
    for index in ordered:
        x1,y1,width,height,area=[int(v) for v in stats[index]]
        if area<16:continue
        x2,y2=x1+width,y1+height
        contours,_=cv2.findContours((stair_parts==index).astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        contour=max(contours,key=cv2.contourArea)
        boundary=cv2.approxPolyDP(contour,.75,True).reshape(-1,2).tolist()
        # Only the adjacent floor can be a flat-plane approach target. The
        # tread mask itself remains a separate, unmeasured connector.
        radius = max(4, min(16, round((x2-x1)*.1)))
        connectors.append(dict(kind='stairs', box=[x1,y1,x2,y2],boundary=boundary,
            approach_box=[max(0, x1-radius), max(0, y2-radius),
                          min(shape[1], x2+radius), min(shape[0], y2+radius)],
            direction='unknown', from_level=None, to_level=None,
            passage_verified=False, requires_separate_surface_measurement=True))
    floor_regions, outside_regions = regions(floors), regions(outside)
    # These are candidates with evidence, not a global scene classification.
    # A courtyard can be OUTDOORS while still enclosed, and an attic has floor
    # under a roof. Neither IsOutdoors nor a ceiling picture proves a level.
    hints = []
    if portals:
        hints.append('opening' if any(p['kind']=='opening' for p in portals) else 'closed_door')
    if floor_regions:
        hints.append('interior_floor')
    if connectors:
        hints.append('stairs')
    if outside_regions and (solid.any() or portals):
        hints.append('enclosed_outdoor_candidate')
    if floor_regions and ceiling.any():
        hints.append('covered_interior_candidate')
    return dict(schema=1, coordinate_space='current_image', size=[shape[1], shape[0]],
        hints=hints, floor_regions=floor_regions, exterior_regions=outside_regions,
        portals=portals, connectors=connectors, level_id=None, room_id=None,
        world_coordinates=None, executable=False)


def flat_navigation_masks(observed, names):
    """Do not use a floor calibration to project across steps or indoor solids."""
    from dataclasses import replace, is_dataclass
    from copy import copy
    stairs = stair_evidence(observed, names)
    barriers = semantic_region(observed, names, BARRIER_LABELS)
    result = replace(observed) if is_dataclass(observed) else copy(observed)
    result.obstacles = observed.obstacles | barriers
    result.forbidden = observed.forbidden | stairs
    result.ground = observed.ground & ~result.obstacles & ~result.forbidden
    result.occluded = observed.occluded & ~result.obstacles & ~result.forbidden
    return result
