"""Resolve color-driven surface conflicts using a bounded second semantic view.

Detection and SAM select candidate regions. A grayscale semantic pass may
corroborate them; it cannot erase a strong original structural label. Outputs
remain visual candidates, not collision-free ground or measured world points.
"""
import cv2
import numpy as np


def review_regions(labels,confidence,names,instances,excluded=None):
    from .segmentation import GROUND_LABELS,SOLID_LABELS
    ids={int(k):str(v).strip().lower() for k,v in names.items()}
    structures=SOLID_LABELS|{'door','screen door','window','stairs','stairway'}
    structural=np.isin(labels,[k for k,v in ids.items() if v in structures])
    ground_support=np.zeros(labels.shape,bool);instance_support=np.zeros(labels.shape,bool)
    words=GROUND_LABELS|{'ground','wooden','stone'}
    for item in instances:
        mask=item.get('mask')
        if (not isinstance(mask,np.ndarray) or mask.dtype!=bool or mask.shape!=labels.shape
                or not np.isfinite([item.get('mask_score',0),item.get('detection_score',0)]).all()):
            continue
        label=set(str(item.get('label','')).strip().lower().split())
        if item.get('mask_score',0)>=.85 and item.get('detection_score',0)>=.25:
            instance_support |= mask
        if (label and label<=words and label & GROUND_LABELS
                and item.get('mask_score',0)>=.9 and item.get('detection_score',0)>=.3):
            ground_support |= mask
    # Structures, UI and stair treads cannot be recolored into free ground.
    ground_support &= ~(structural & (confidence>=.65))
    weak_structure=instance_support & structural & (confidence>=.25) & (confidence<.65)
    if excluded is not None:
        ground_support &= ~excluded;weak_structure &= ~excluded
    return ground_support,weak_structure


def needs_surface_review(labels,confidence,margin,names,instances,excluded=None):
    from .segmentation import GROUND_LABELS
    ids=[int(k) for k,v in names.items() if str(v).strip().lower() in GROUND_LABELS]
    ground,weak=review_regions(labels,confidence,names,instances,excluded)
    conflict=ground & ~(np.isin(labels,ids)&(confidence>=.8)&(margin>=.2))
    # A local patch suffices; no fraction-of-whole-image gate is used.
    return np.count_nonzero(conflict|weak)>=16


def reconcile_surfaces(labels,confidence,margin,review,names,instances,excluded=None):
    from .segmentation import GROUND_LABELS,SOLID_LABELS
    gray_labels,gray_confidence,gray_margin=review
    if (any(a.shape!=labels.shape for a in (confidence,margin,gray_labels,gray_confidence,gray_margin))
            or not all(np.isfinite(a).all() for a in (confidence,margin,gray_confidence,gray_margin))):
        raise ValueError('surface_review_invalid')
    ids={int(k):str(v).strip().lower() for k,v in names.items()}
    ground_ids=[k for k,v in ids.items() if v in GROUND_LABELS]
    barrier_ids=[k for k,v in ids.items() if v in SOLID_LABELS|{'door','screen door','window','stairs','stairway'}]
    support,weak=review_regions(labels,confidence,names,instances,excluded)
    initial_ground=np.isin(labels,ground_ids)&(confidence>=.8)&(margin>=.2)
    add_ground=support & ~initial_ground & np.isin(gray_labels,ground_ids) & (gray_confidence>=.8)&(gray_margin>=.2)
    water_ids=[k for k,v in ids.items() if v in {'water','river','sea','swimming pool'}]
    original_water=np.isin(labels,water_ids)
    # A floor instance may spill across a river bank. Do not open fragments of
    # a connected water surface that BOTH exposures still identify as water.
    # This is local conflicting evidence, not a whole-image floor-area gate.
    agreed_water=(original_water & (confidence>=.5) & np.isin(gray_labels,water_ids)
                  & (gray_confidence>=.8) & (gray_margin>=.2))
    _,components=cv2.connectedComponents(original_water.astype(np.uint8),connectivity=8)
    votes=np.bincount(components[agreed_water],minlength=int(components.max())+1)
    protected=original_water & (votes[components]>=16)
    rejected_water=int((add_ground&protected).sum())
    add_ground &= ~protected
    confirm_barrier=weak & np.isin(gray_labels,barrier_ids)&(gray_confidence>=.65)&(gray_margin>=.2)
    use=add_ground|confirm_barrier
    fused_labels=labels.copy();fused_confidence=confidence.copy();fused_margin=margin.copy()
    fused_labels[use]=gray_labels[use];fused_confidence[use]=gray_confidence[use];fused_margin[use]=gray_margin[use]
    sources=np.zeros(labels.shape,np.uint8);sources[add_ground]=1;sources[confirm_barrier]=2
    diagnostics=dict(method='color_and_sam_with_grayscale_review',raw_maps_retained=True,
        ground_candidate_pixels=int(add_ground.sum()),barrier_pixels=int(confirm_barrier.sum()),
        water_conflicts_resolved=int((add_ground&np.isin(labels,water_ids)).sum()),
        water_candidates_rejected=rejected_water,
        world_geometry_verified=False)
    return fused_labels,fused_confidence,fused_margin,sources,diagnostics
