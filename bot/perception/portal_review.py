"""Bounded local material review around a detected opening.

Cropping changes observation resolution, never world scale. Strong structural
semantics and non-furniture instances stay protected. Any revised floor remains
a visual candidate requiring the normal geometry and movement verification.
"""
from dataclasses import replace
import time

import cv2
import numpy as np

from .building_layout import door_state, STAIR_LABELS

DETAIL_PROMPT=('floor. rug. carpet. wall. wooden door. closed door. open doorway. '
               'bed. sofa. table. stairs. railing. bookcase. barrel.')
FURNITURE={'bed','sofa','table','desk','chest'}
FLOOR={'floor','rug','carpet','road','grass','earth','sidewalk','path','field','land'}


def context_box(box,shape):
    """Retain approaching ground/context even for a small distant doorway."""
    h,w=shape;l,t,r,b=map(float,box)
    pad=.2*max(r-l,b-t)
    width=min(w,max(384,r-l+2*pad));height=min(h,max(384,b-t+2*pad))
    x=np.clip((l+r-width)/2,0,w-width);y=np.clip((t+b-height)/2,0,h-height)
    return tuple(int(round(v)) for v in (x,y,x+width,y+height))


def portal_detail_candidate(observed,names):
    """At most one current-frame crop; no point annotations or screen-area gate."""
    ids={int(k):str(v).strip().lower() for k,v in names.items()}
    possible=np.isin(observed.semantic_labels,[k for k,v in ids.items() if v in FLOOR])
    possible |= (np.isin(observed.semantic_labels,[k for k,v in ids.items() if v=='wall'])
                 & (observed.semantic_confidence<.65))
    possible &= ~observed.ground & ~observed.water & ~observed.forbidden
    h,w=possible.shape
    for item in sorted(observed.instances,key=lambda x:x.get('detection_score',0),reverse=True):
        if (item.get('detection_score',0)<.3 or item.get('mask_score',0)<.85
                or door_state(item,observed.semantic_labels,observed.semantic_confidence,names)!='opening'):
            continue
        l,t,r,b=item['box'];l,r=max(0,round(l)),min(w,round(r))
        t,b=max(0,round(t+(b-t)*.5)),min(h,round(b+12))
        if np.count_nonzero(possible[t:b,l:r])>=16:
            return dict(box=context_box(item['box'],(h,w)),source_box=list(item['box']))
    return None


def _ground_instances(instances,shape,*,weak=False):
    mask=np.zeros(shape,bool)
    words=FLOOR|{'wooden','stone'}
    for item in instances:
        label=set(str(item['label']).strip().lower().split())
        if (label and label<=words and label&FLOOR and item['mask_score']>=(.95 if weak else .9)
                and item['detection_score']>=(.25 if weak else .3)):
            mask |= item['mask']
    return mask


def _local_stair_instances(instances,crop,shape,excluded):
    """Carry newly observed steps into the full-frame surface inventory.

    Local semantics often call the treads floor/wall. A precise stair instance
    still reserves a separate surface; it never establishes slope or scale.
    The crop transform is image-to-image, not a world-coordinate projection.
    """
    l,t,r,b=crop;result=[]
    for item in instances:
        if (item['label'].strip().lower() not in STAIR_LABELS
                or item.get('detection_score',0)<.25 or item.get('mask_score',0)<.85):
            continue
        mask=item.get('mask');box=np.asarray(item.get('box'),float)
        if (not isinstance(mask,np.ndarray) or mask.dtype!=bool or mask.shape!=(b-t,r-l)
                or box.shape!=(4,) or not np.isfinite(box).all()
                or box[0]>=box[2] or box[1]>=box[3]):
            raise ValueError('portal_review_instance_invalid')
        full=np.zeros(shape,bool);full[t:b,l:r]=mask
        if excluded is not None:full &= ~excluded
        if not full.any():continue
        box=np.clip(box,[0,0,0,0],[r-l,b-t,r-l,b-t])+[l,t,l,t]
        if box[0]>=box[2] or box[1]>=box[3]:continue
        result.append(dict(item,box=box.tolist(),mask=full,
                           observation_source='portal_detail',observation_box=list(crop)))
    return result


def merge_portal_detail(observed,names,crop,local_rgb,local_gray,instances,excluded=None):
    """Correct corroborated material conflicts, not arbitrary obstacle erasure."""
    from .segmentation import SOLID_LABELS,fuse_masks,navigation_evidence
    l,t,r,b=crop;shape=(b-t,r-l)
    arrays=(*local_rgb,*local_gray)
    if (any(not isinstance(a,np.ndarray) or a.shape!=shape for a in arrays)
            or not all(np.isfinite(a).all() for a in arrays)):
        raise ValueError('portal_review_invalid')
    ids={int(k):str(v).strip().lower() for k,v in names.items()}
    ground_ids=[k for k,v in ids.items() if v in FLOOR]
    barrier_ids=[k for k,v in ids.items() if v in SOLID_LABELS|STAIR_LABELS|{'door','screen door'}]
    rgb_labels,rgb_conf,rgb_gap=local_rgb;gray_labels,gray_conf,gray_gap=local_gray
    rgb_ground=np.isin(rgb_labels,ground_ids)&(rgb_conf>=.8)&(rgb_gap>=.2)
    gray_ground=np.isin(gray_labels,ground_ids)&(gray_conf>=.8)&(gray_gap>=.2)
    local_support=_ground_instances(instances,shape)
    global_support=_ground_instances(observed.instances,observed.ground.shape,weak=True)[t:b,l:r]
    # One local semantic view needs a strong local ground instance. Two agreeing
    # views may corroborate an existing weaker text detection with precise SAM.
    candidate=(local_support&(rgb_ground|gray_ground)) | (global_support&rgb_ground&gray_ground)
    labels=observed.semantic_labels[t:b,l:r];confidence=observed.semantic_confidence[t:b,l:r]
    protected=(np.isin(labels,barrier_ids)&(confidence>=.65)) | observed.forbidden[t:b,l:r] | observed.water[t:b,l:r]
    # Existing hard surfaces and ambiguous instances are never removed here.
    # Furniture is eligible only where the original semantics already saw floor.
    for item in observed.instances:
        label=item['label'].strip().lower()
        if label in FURNITURE:
            protected |= item['mask'][t:b,l:r] & ~(np.isin(labels,ground_ids)&(confidence>=.5))
        elif any(word in label.split() for word in ('closed','locked')):
            protected |= item['mask'][t:b,l:r]
        elif (door_state(item,observed.semantic_labels,observed.semantic_confidence,names) is None
              and not (set(label.split())<=FLOOR|{'ground','wooden','stone','building','house'})):
            protected |= item['mask'][t:b,l:r]
    # Conflicting local furniture/door/stair evidence wins. A panel detected
    # beside an opening is still physical even if the whole doorway is open.
    local_barrier_support=np.zeros(shape,bool)
    for item in instances:
        words=set(item['label'].strip().lower().split())
        barrier=words&({'wall','door','bed','sofa','table','stairs','railing','bookcase','barrel'})
        if (barrier and 'open' not in words and item['mask_score']>=.85
                and item['detection_score']>=(.25 if 'stairs' in words else .3)):
            protected |= item['mask']
            local_barrier_support |= item['mask']
    confirmed_barrier=(local_barrier_support & np.isin(rgb_labels,barrier_ids)
                       & np.isin(gray_labels,barrier_ids) & (rgb_conf>=.65) & (gray_conf>=.65)
                       & (rgb_gap>=.2) & (gray_gap>=.2))
    if excluded is not None:protected |= excluded[t:b,l:r]
    if excluded is not None:confirmed_barrier &= ~excluded[t:b,l:r]
    candidate &= ~protected
    # Never use unrelated crop changes outside actual new evidence.
    candidate &= ~observed.ground[t:b,l:r]
    fused_labels=observed.semantic_labels.copy();fused_conf=observed.semantic_confidence.copy()
    # Without a saved margin, 2*p_top-1 is a lower bound on the top-two gap.
    margin=np.maximum(0,2*fused_conf-1)
    use_rgb=rgb_ground & (~gray_ground | (rgb_conf<=gray_conf))
    selected_labels=np.where(use_rgb,rgb_labels,gray_labels)
    selected_conf=np.where(use_rgb,rgb_conf,gray_conf)
    selected_gap=np.where(use_rgb,rgb_gap,gray_gap)
    fused_labels[t:b,l:r][candidate]=selected_labels[candidate]
    fused_conf[t:b,l:r][candidate]=selected_conf[candidate]
    margin[t:b,l:r][candidate]=selected_gap[candidate]
    # A closer view can reveal a real panel that the full image called floor.
    # Corroborated structural evidence must also remove previously free pixels.
    fused_labels[t:b,l:r][confirmed_barrier]=rgb_labels[confirmed_barrier]
    fused_conf[t:b,l:r][confirmed_barrier]=np.minimum(rgb_conf,gray_conf)[confirmed_barrier]
    margin[t:b,l:r][confirmed_barrier]=np.minimum(rgb_gap,gray_gap)[confirmed_barrier]
    full=np.zeros(observed.ground.shape,bool);full[t:b,l:r]=candidate
    revised=[];revised_furniture=np.zeros_like(full)
    for item in observed.instances:
        new=dict(item)
        if item['label'].strip().lower() in FURNITURE:
            # Strong local ground detection is additionally required to resolve
            # an explicit conflicting furniture instance (e.g. rug called bed).
            corrected=np.zeros_like(full);corrected[t:b,l:r]=candidate&local_support&rgb_ground&gray_ground
            remove=item['mask']&corrected
            revised_furniture |= remove
            new['mask']=item['mask']&~remove
        revised.append(new)
    transferred=_local_stair_instances(instances,crop,observed.ground.shape,excluded)
    revised.extend(transferred)
    ground,solid,unknown=fuse_masks(fused_labels,fused_conf,margin,names,revised,
        verified_ground=observed.ground_evidence,excluded=excluded)
    solid |= observed.obstacles & ~revised_furniture
    solid[t:b,l:r] |= confirmed_barrier
    ground &= ~solid
    # Re-fusion must preserve previously usable floor, including existing gaps
    # with a true (but unsaved) top-two margin above its conservative bound.
    ground |= observed.ground & ~solid & ~observed.forbidden & ~observed.water
    unknown=~(ground|solid)
    occluded,water,forbidden=navigation_evidence(fused_labels,fused_conf,names,revised,ground,solid,unknown,excluded)
    water |= observed.water;forbidden |= observed.forbidden
    ground &= ~water & ~forbidden;occluded &= ~water & ~forbidden
    unknown=~(ground|solid)
    sources=(observed.review_sources.copy() if observed.review_sources is not None else np.zeros_like(full,np.uint8))
    sources[full]=3
    sources[t:b,l:r][confirmed_barrier]=4
    result=replace(observed,semantic_labels=fused_labels,semantic_confidence=fused_conf,
        ground=ground,obstacles=solid,unknown=unknown,instances=revised,
        occluded=occluded,water=water,forbidden=forbidden,review_sources=sources)
    return result,dict(ground_candidates=int(full.sum()),furniture_pixels_revised=int(revised_furniture.sum()),
                       corroborated_barrier_pixels=int(confirmed_barrier.sum()),
                       transferred_stair_instances=len(transferred),
                       geometry_verified=False)


def inspect_portal_detail(model,rgb,observed,excluded=None):
    from PIL import Image
    names=model.semantic.config.id2label
    candidate=portal_detail_candidate(observed,names)
    diagnostics=dict(observed.diagnostics,portal_review=dict(triggered=False))
    if candidate is None:return replace(observed,diagnostics=diagnostics)
    began=time.monotonic();l,t,r,b=candidate['box'];local=rgb[t:b,l:r]
    image=Image.fromarray(local)
    with model.torch.inference_mode():
        local_rgb=model._semantics(image,local.shape[:2])
        local_gray=model._semantics(image.convert('L').convert('RGB'),local.shape[:2])
        inp=model.detector_processor(images=image,text=DETAIL_PROMPT,return_tensors='pt')
        found=model.detector_processor.post_process_grounded_object_detection(
            model.detector(**inp),inp.input_ids,threshold=.25,text_threshold=.25,target_sizes=[local.shape[:2]])[0]
        # The caller already embedded this full exposure. Local detector boxes
        # map back into that same image; do not spend another embedding per door.
        instances=[]
        order=model.torch.argsort(found['scores'],descending=True).tolist()[:24]
        for i in order:
            box=found['boxes'][i].numpy()
            masks,scores,_=model.sam.predict(box=box+np.array([l,t,l,t]),multimask_output=True)
            best=int(np.argmax(scores))
            instances.append(dict(label=found['text_labels'][i],box=box.tolist(),detection_score=float(found['scores'][i]),
                                  mask_score=float(scores[best]),mask=masks[best][t:b,l:r].astype(bool)))
        model._sync()
    result,details=merge_portal_detail(observed,names,candidate['box'],local_rgb,local_gray,instances,excluded)
    arrays={'portal_schema':np.array([1],np.uint8),'portal_box':np.asarray(candidate['box'],np.int32)}
    for key in ('semantic_labels','semantic_confidence','ground','obstacles','water','forbidden'):
        arrays['portal_before_'+key]=getattr(observed,key)[t:b,l:r].copy()
    prior=[]
    for i,item in enumerate(observed.instances):
        if item['mask'][t:b,l:r].any():
            arrays[f'portal_prior_instance_{i}']=item['mask'][t:b,l:r].copy()
            prior.append(dict(index=i,**{k:v for k,v in item.items() if k!='mask'}))
    for tag,values in [('rgb',local_rgb),('gray',local_gray)]:
        for key,value in zip(('labels','confidence','margin'),values):arrays[f'portal_{tag}_{key}']=value
    for i,item in enumerate(instances):arrays[f'portal_instance_{i}']=item['mask']
    diagnostics['portal_review']=dict(triggered=True,**candidate,**details,
        full_frame_sam_reused=True,
        prior_instances=prior,
        detections=[{k:v for k,v in item.items() if k!='mask'} for item in instances])
    return replace(result,diagnostics=diagnostics,portal_evidence=arrays,
                   timings=dict(observed.timings,portal_review_s=time.monotonic()-began))
