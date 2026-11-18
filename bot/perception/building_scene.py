"""Surface/doorway evidence from local segmentation; never pixel-to-world guesses."""
import cv2
import numpy as np
from .building_layout import building_layout, door_state
from .ground_calibration import ground_features
from .scene_scale import SCENE_BOUNDS

BUILDING_PROMPT = ('roof. building. wall. open doorway. entrance. closed door. '
                   'stairs. railing. column. table. bed. wardrobe. bookcase. chest. rug. floor. grass. road.')
EXTERIOR = {'grass', 'road', 'earth', 'sidewalk', 'field', 'path', 'land'}
STRUCTURE = {'building', 'house', 'wall', 'roof', 'ceiling'}

# Observability failures may change view, but input/state/identity failures may not.
BUILDING_REOBSERVE = frozenset({
    'building_entrance_unobserved', 'building_door_floor_unobserved', 'building_threshold_identity_unconfirmed',
    'building_door_approach_unconfirmed', 'auto_ground_texture_insufficient',
    'registration_ground_texture_missing', 'registration_insufficient_matches',
    'registration_inlier_ratio', 'registration_holdout_error', 'registration_fit_failed',
    'tracking_local_texture_missing', 'tracking_insufficient_matches', 'tracking_validation_failed',
    'tracking_seed_insufficient', 'tracking_seed_unreliable', 'tracking_near_surface_unresolved',
    'tracking_flow_failed', 'auto_camera_rotation_unobserved', 'auto_orbit_angle_insufficient',
    'auto_orbit_ground_support_missing', 'auto_orbit_unobservable', 'auto_orbit_pivot_inconsistent',
    'auto_orbit_pivot_outside_avatar_region', 'auto_anchor_outside_measured_ground',
    'auto_no_observed_probe_corridor', 'tracking_spatial_support_missing',
    'approach_start_needs_other_evidence', 'approach_no_supported_connection',
    'building_local_surface_unobserved', 'building_no_reachable_observation_region',
    'building_threshold_occluded',
    'auto_calibration_repeated_collision',
    'building_return_entrance_unobserved', 'building_return_entrance_ambiguous',
    'building_return_side_unobserved',
})


def support_zone(shape, box, full_window):
    """Express the same player neighbourhood in the crop or the full window."""
    h,w=shape
    x1,y1,x2,y2=box
    if full_window:
        l,t,r,b=SCENE_BOUNDS
        x1,x2=l+x1*(r-l),l+x2*(r-l)
        y1,y2=t+y1*(b-t),t+y2*(b-t)
    return np.s_[round(h*y1):round(h*y2),round(w*x1):round(w*x2)]


def turnaway_view(observed,names,hidden_ui_flags):
    """Inspect a short forward corridor after turning, without estimating metres."""
    shape=observed.ground.shape
    visible=~building_ui_exclusion(shape,full_window=True,hidden_ui_flags=hidden_ui_flags)
    water_ids=[int(k) for k,v in names.items() if str(v).lower() in
               {'water','river','sea','swimming pool','lake','stairs','stairway'}]
    hazard=observed.obstacles | (np.isin(observed.semantic_labels,water_ids)&(observed.semantic_confidence>=.65))
    h,w=shape
    lanes={}
    for name,cx in [('left',.40),('center',.50),('right',.60)]:
        bands=[]
        for top,bottom in [(.40,.45),(.45,.50),(.50,.55)]:
            region=np.s_[round(top*h):round(bottom*h),round((cx-.045)*w):round((cx+.045)*w)]
            valid=visible[region]
            if valid.any():
                bands.append((float(hazard[region][valid].mean()),float(observed.ground[region][valid].mean())))
        blocked=any(solid>=.52 for solid,_ in bands)
        ground=sum(floor>=.16 for _,floor in bands)
        lanes[name]='blocked' if blocked else 'open' if ground>=2 else 'unknown'
    return dict(schema=1,lanes=lanes,executable=False)


def building_ground_measurement(rgb, observed, *, hidden_ui_flags=None, full_window=False):
    """Texture and a LOOK cue, never a walk target or an assumed avatar foot.

    Looking toward an occluded doorway again does not resolve missing ground
    texture. Rank visible ground patches separately from the entrance cue.
    """
    excluded=building_ui_exclusion(observed.ground.shape,full_window=full_window,
                                   hidden_ui_flags=hidden_ui_flags)
    excluded |= getattr(observed,'forbidden',np.zeros_like(excluded))
    mask=(observed.ground & (observed.semantic_confidence>=.9) & ~excluded).astype(np.uint8)*255
    _,_,texture=ground_features(rgb,mask)
    mask=cv2.erode(mask,np.ones((7,7),np.uint8))
    h,w=mask.shape
    # Select connected NEAR ground only. A distant hillside cannot stand in for
    # the surface supporting the player. This cue only turns a stationary view.
    local=mask.copy()
    near=support_zone((h,w),(0.,.6,1.,1.),full_window)
    local[:near[0].start]=0
    _,components,stats,_=cv2.connectedComponentsWithStats(local,connectivity=8)
    patch=max(range(1,len(stats)),key=lambda i:int(stats[i,cv2.CC_STAT_AREA]),default=0)
    hint=None
    if patch and stats[patch,cv2.CC_STAT_AREA]>=1000:
        center=float(np.median(np.nonzero(components==patch)[1]))/w
        hint='left' if center<.45 else 'right' if center>.55 else 'center'
    return dict(ground_pixels=int((observed.ground & (observed.semantic_confidence>=.9)
                                  & ~excluded).sum()),
                **texture,minimum_features=40,
                look_hint=hint,look_basis='near_ground_patch',translation_authorized=False)


def building_ui_exclusion(shape, *, full_window=False, hidden_ui_flags=None):
    """Conservative HUD guard for the task scene crop (.20,.08,.80,.68).

    Its lower edge contains the action bar, with chat/tracker at the sides.
    Exclusion is not terrain evidence; it only removes pixels from measurement
    and movement. Camera recovery must find more visible ground when needed.
    """
    h,w=shape
    mask=np.zeros((h,w),bool)
    hidden=hidden_ui_flags if type(hidden_ui_flags) is int and 0<=hidden_ui_flags<=15 else 0
    if full_window:
        # Existing verified HUD layout: title/top buffs, minimap, tracker,
        # action bars, chat, portrait, and pixel telemetry. Leave the lower
        # right world patch visible; do not crop away all ground at a facade.
        boxes=[(0,0,1,.04),(.70,.04,1,.31),(0,.89,.20,1),(.02,.31,.20,.96)]
        if not hidden&1:boxes.extend([(.94,.30,1,1),(.27,.62,.72,1)])
        if not hidden&2:boxes.append((.77,.30,1,.80))
        if not hidden&4:boxes.append((.01,.39,.34,.65))
        if not hidden&8:boxes.append((.18,.65,.31,.79))
        for x1,y1,x2,y2 in boxes:
            mask[round(y1*h):round(y2*h),round(x1*w):round(x2*w)]=True
        return mask
    if not hidden&1:mask[round(h*.90):,:]=True
    # These crop strips overlap two supported HUD groups. Free them only when
    # BOTH have current receipts. Unknown/unversioned telemetry stays masked.
    if hidden&12 != 12:mask[round(h*.56):,:round(w*.16)]=True
    if hidden&10 != 10:mask[round(h*.44):,round(w*.95):]=True
    return mask


def describe_building_scene(observed, names, *, camera_view=None, native_outdoors=None, full_window=False):
    labels, confidence = observed.semantic_labels, observed.semantic_confidence
    h, w = labels.shape
    def mask(words, threshold=.65):
        return (np.isin(labels, [int(k) for k, v in names.items() if str(v).strip().lower() in words])
                & (confidence >= threshold) & ~getattr(observed,'forbidden',np.zeros_like(labels,bool)))
    exterior = mask(EXTERIOR)
    floor = mask({'floor'})
    structure = mask(STRUCTURE)
    # Broad neighbourhood around the centered avatar, not an exact visible foot.
    # Occluders stay unknown; they are never filled in as traversable terrain.
    zone = np.zeros((h,w), bool)
    zone[support_zone((h,w),(.25,.60,.75,.92),full_window)] = True
    nearby = np.zeros((h,w), bool)
    nearby[support_zone((h,w),(.40,.68,.60,.90),full_window)] = True
    ratios = {key: float(value[zone].mean()) for key,value in
              [('exterior', exterior), ('floor', floor), ('structure', structure), ('water', observed.water)]}
    local_structure = float(structure[nearby].mean())
    local_water = float(observed.water[nearby].mean())
    visible = exterior | floor | structure | observed.water
    coverage = float(visible[zone].mean())
    ground_share = ratios['exterior'] / coverage if coverage else 0.
    # Ground glimpses are useful only when they form an actual connected patch
    # near the avatar. Distant grass or many disconnected specks do not qualify.
    # Do not close holes, erase obstacles or infer any screen/world route here.
    _, components, stats, _ = cv2.connectedComponentsWithStats(
        (exterior & zone & ~observed.water).astype(np.uint8), connectivity=8)
    overlaps = np.bincount(components[nearby], minlength=len(stats))
    patch = max(range(1, len(stats)), key=lambda i: int(overlaps[i]), default=0)
    patch_area = float(stats[patch, cv2.CC_STAT_AREA] / zone.sum()) if patch else 0.
    patch_nearby = float(overlaps[patch] / nearby.sum()) if patch else 0.
    conflict_free = (ratios['structure'] < .12 and ratios['water'] < .05
                     and local_structure < .12 and local_water < .05)
    requirements = dict(overhead=camera_view == 4, visible_ground=ratios['exterior'] >= .20,
        surface_agreement=ground_share >= .85, connected_patch=patch_area >= .08,
        nearby_patch=patch_nearby >= .20, no_surface_conflict=conflict_free)
    occluded_exterior = all(requirements.values())
    surface = ('structure' if ratios['structure'] >= .45 else
               'water' if ratios['water'] >= .25 else
               'exterior' if conflict_free and (ratios['exterior'] >= .32 or occluded_exterior) else
               'interior_floor' if ratios['floor'] >= .45 and ratios['structure'] < .12 else 'unknown')
    local_layout=building_layout(observed,names)
    if surface=='structure' and native_outdoors is False and local_layout['floor_regions']:
        # Close walls/ceilings can dominate an indoor exposure. Their area is
        # not evidence that the character stands on a roof. Local floor allows
        # further measurement, not an arrival or a permission to walk.
        surface='unknown'
    evidence = dict(schema=1, camera_view=camera_view, visible_surface_fraction=coverage,
        exterior_share_of_visible_surface=ground_share, connected_patch_fraction=patch_area,
        nearby_patch_fraction=patch_nearby, nearby_structure_fraction=local_structure,
        nearby_water_fraction=local_water,
        basis=('nearby_connected_exterior' if surface == 'exterior' and ratios['exterior'] < .32
               else 'broad_surface_support' if surface != 'unknown' else 'unconfirmed'),
        occlusion_requirements=requirements)
    doors, rejected = [], []
    opening = mask({'door', 'stairs', 'stairway', 'floor'}, threshold=.5)
    for item in observed.instances:
        if (door_state(item,labels,confidence,names)!='opening'
                or item['detection_score'] < .3 or item['mask_score'] < .85):
            continue
        x1,y1,x2,y2 = [float(v) for v in item['box']]
        l,r = max(0,round(x1)), min(w,round(x2))
        top,bottom = max(0,round(y1)), min(h,round(y2))
        if r<=l or bottom<=top:continue
        dx,dy=max(4,round((x2-x1)*.2)),max(4,round((y2-y1)*.15))
        # Inspect the frame AROUND the opening. Stairs/air inside an open door
        # are expected, not missing wall evidence. 'house' is an ADE structure.
        bands=[structure[max(0,top-dy):top,max(0,l-dx):min(w,r+dx)],
               structure[top:bottom,max(0,l-dx):l],structure[top:bottom,r:min(w,r+dx)]]
        surround=[float(a.mean()) if a.size else 0. for a in bands]
        t,b = max(0,round(y2-6)), min(h,round(y2+max(12,(y2-y1)*.18)))
        band = (exterior | floor)[t:b,l:r]
        ground_fraction=float(band.mean()) if band.size else 0.
        opening_fraction=float(opening[top:bottom,l:r].mean())
        interior_solid=float(structure[top:bottom,l:r].mean())
        closed=any(door_state(i,labels,confidence,names)=='closed'
                   and i.get('detection_score',0)>=.3 and i.get('mask_score',0)>=.85
                   and _box_overlap([x1,y1,x2,y2],i['box'])>.45 for i in observed.instances)
        reason=('closed_door' if closed else
                'frame_unconfirmed' if surround[0]<.18 or max(surround[1:])<.18 else
                'opening_unconfirmed' if interior_solid>=.7 or
                    opening_fraction<.08 and ground_fraction<.35 else
                'roof_surface' if surface=='structure' else None)
        details=dict(box=[x1,y1,x2,y2],surround_structure=surround,
                     opening_fraction=opening_fraction,threshold_ground_fraction=ground_fraction)
        if reason:
            rejected.append(dict(details,reason=reason));continue
        # This is an entrance observation only. A hidden sill stays unconfirmed;
        # actual approach/crossing still intersect fresh ground and obstacles.
        if any(_box_overlap([x1,y1,x2,y2],d['box'])>.45 for d in doors):continue
        doors.append(dict(box=[x1,y1,x2,y2], threshold_box=[l,t,r,b],
                          score=item['detection_score'], connected_ground_fraction=ground_fraction,
                          threshold_observed=ground_fraction>=.35,passage_verified=False,
                          surround_structure=surround,opening_fraction=opening_fraction))
    return dict(schema=1, surface=surface, native_outdoors=native_outdoors,
                support=ratios, surface_evidence=evidence, doorways=doors,
                layout=building_layout(observed,names,doors=doors),
                rejected_doorways=rejected,
                look_hint=_look_hint([dict(label='open doorway',box=d['box'],detection_score=d['score'])
                                      for d in doors] or observed.instances,w),
                building_visible=bool(structure.mean() > .06), executable=False,
                world_coordinates=None, entrance_verified=False)


def _box_overlap(a,b):
    intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return intersection/max(1,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection)


def _look_hint(instances,width):
    candidates=[i for i in instances if any(s in i['label'] for s in ('doorway','entrance'))
                and i['detection_score']>=.25]
    if not candidates:return None
    item=max(candidates,key=lambda i:i['detection_score'])
    center=(item['box'][0]+item['box'][2])/(2*width)
    return 'left' if center<.45 else 'right' if center>.55 else 'center'
