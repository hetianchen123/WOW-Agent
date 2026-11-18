"""Local doorway material conflicts; no claim of live movement/physical clearance."""
from dataclasses import replace
from pathlib import Path
import json

import numpy as np
import pytest

from bot.perception.segmentation import SegmentedMap,fuse_masks,navigation_evidence
from bot.perception.portal_review import context_box,portal_detail_candidate,merge_portal_detail


NAMES={0:'unknown',1:'floor',2:'rug',3:'wall',4:'door',5:'stairs',6:'bed',7:'water',8:'roof'}


def mask():
    value=np.zeros((120,180),bool);value[40:80,60:110]=True
    return value


def item(label,area=None,score=.6):
    return dict(label=label,mask=mask() if area is None else area,
                box=[55,35,115,85],detection_score=score,mask_score=.97)


def scene():
    labels=np.ones((120,180),np.uint8);conf=np.full(labels.shape,.98,np.float32)
    conf[mask()]=.6
    instances=[item('bed'),item('floor',np.ones(labels.shape,bool),score=.26)]
    ground,solid,unknown=fuse_masks(labels,conf,np.maximum(0,2*conf-1),NAMES,instances)
    empty=np.zeros(labels.shape,bool)
    occluded,water,forbidden=navigation_evidence(labels,conf,NAMES,instances,ground,solid,unknown)
    return SegmentedMap(ground,solid,unknown,instances,labels,conf,{}, {},empty,occluded,water,forbidden,
                        labels.copy(),conf.copy())


def review(observed,local=None,excluded=None):
    shape=observed.ground.shape
    rgb=(np.ones(shape,np.uint8),np.full(shape,.91,np.float32),np.full(shape,.6,np.float32))
    gray=(np.full(shape,2,np.uint8),np.full(shape,.85,np.float32),np.full(shape,.6,np.float32))
    result,details=merge_portal_detail(observed,NAMES,(0,0,shape[1],shape[0]),rgb,gray,
                                     [item('carpet')] if local is None else local,excluded)
    return result,details


def test_rug_misnamed_bed_requires_local_detection_and_both_semantic_views():
    observed=scene();before=observed.instances[0]['mask'].copy()
    updated,details=review(observed)
    assert updated.ground[mask()].all() and not updated.obstacles[mask()].any()
    assert np.allclose(updated.semantic_confidence[mask()],.85)
    assert not (updated.semantic_confidence[mask()]>=.9).any()
    assert details['furniture_pixels_revised']==int(mask().sum())
    assert details['geometry_verified'] is False
    assert np.array_equal(observed.instances[0]['mask'],before)
    assert np.array_equal(updated.raw_semantic_labels,observed.raw_semantic_labels)
    assert np.all(updated.review_sources[mask()]==3)


def test_semantic_confirmation_alone_does_not_erase_a_furniture_instance():
    updated,details=review(scene(),local=[])
    assert updated.obstacles[mask()].all() and not updated.ground[mask()].any()
    assert details['furniture_pixels_revised']==0


@pytest.mark.parametrize('semantic',[3,4,5,6,8])
def test_strong_original_wall_panel_stairs_furniture_and_roof_survive_crop_floor(semantic):
    observed=scene();observed.semantic_labels[mask()]=semantic
    observed.semantic_confidence[mask()]=.95
    updated,details=review(observed)
    assert not updated.ground[mask()].any() and details['furniture_pixels_revised']==0


@pytest.mark.parametrize('label',['wall','roof','wooden fence','closed door','stairs','railing'])
def test_existing_non_furniture_mask_is_not_cleared_by_crop_ground(label):
    observed=scene();observed.instances.append(item(label))
    updated,_=review(observed)
    assert not updated.ground[mask()].any()


@pytest.mark.parametrize('label',['wooden door closed door','stairs','bed','wall','barrel'])
def test_local_structural_conflict_prevents_release(label):
    updated,_=review(scene(),local=[item('carpet'),item(label)])
    assert not updated.ground[mask()].any()


@pytest.mark.parametrize('field',['water','forbidden','excluded'])
def test_water_and_ui_remain_excluded_even_if_local_ground_agrees(field):
    observed=scene();excluded=None
    if field=='excluded':excluded=mask()
    else:setattr(observed,field,mask())
    updated,_=review(observed,excluded=excluded)
    assert not updated.ground[mask()].any()


def test_new_semantics_cannot_discard_an_unrelated_existing_obstacle():
    observed=scene();observed.obstacles[5:15,5:15]=True;observed.ground[5:15,5:15]=False
    updated,_=review(observed)
    assert updated.obstacles[5:15,5:15].all() and not updated.ground[5:15,5:15].any()


@pytest.mark.parametrize('semantic,label',[(3,'wall'),(4,'wooden door closed door'),(5,'stairs')])
def test_new_corroborated_local_structure_overrides_old_false_floor(semantic,label):
    observed=scene();observed.instances=[];observed.ground[:]=True;observed.obstacles[:]=False
    labels=np.ones(observed.ground.shape,np.uint8);labels[mask()]=semantic
    view=(labels,np.full(labels.shape,.92),np.full(labels.shape,.7))
    after,details=merge_portal_detail(observed,NAMES,(0,0,180,120),view,view,[item(label)])
    assert not after.ground[mask()].any() and after.obstacles[mask()].all()
    assert np.all(after.review_sources[mask()]==4)
    assert details['corroborated_barrier_pixels']==mask().sum()
    assert after.ground[0,0] and not after.obstacles[0,0]


def test_context_includes_approaching_floor_and_stays_inside_image():
    for box in ([450,300,500,360],[1,1,45,60],[950,800,999,899]):
        l,t,r,b=context_box(box,(900,1000))
        assert 0<=l<r<=1000 and 0<=t<b<=900
        assert r-l>=384 and b-t>=384
        assert l<=box[0] and t<=box[1] and r>=box[2] and b>=box[3]


def test_detail_is_not_requested_without_a_current_opening_or_unresolved_patch():
    observed=scene();assert portal_detail_candidate(observed,NAMES) is None
    opening=item('open doorway');opening['box']=[20,10,150,100]
    observed.instances.append(opening)
    proposal=portal_detail_candidate(observed,NAMES)
    assert proposal is not None and proposal['source_box']==opening['box']
    observed.ground[:]=True
    assert portal_detail_candidate(observed,NAMES) is None


def test_closed_panel_does_not_start_an_opening_material_review():
    observed=scene();observed.semantic_labels[mask()]=4;observed.semantic_confidence[mask()]=.98
    observed.instances=[item('closed door')]
    assert portal_detail_candidate(observed,NAMES) is None


def test_invalid_crop_result_never_resizes_into_a_route_map():
    observed=scene();shape=observed.ground.shape
    bad=(np.ones((1,1),np.uint8),np.ones(shape),np.ones(shape))
    with pytest.raises(ValueError,match='portal_review_invalid'):
        merge_portal_detail(observed,NAMES,(0,0,180,120),bad,bad,[])


@pytest.mark.parametrize('index',[0,1])
def test_recorded_portal_models_recover_floor_without_opening_panel_or_wall(index):
    path=Path(__file__).parent/'fixtures/portal-material-review-20261118'
    meta=json.loads(path.with_suffix('.json').read_text())['cases'][index]
    with np.load(path.with_suffix('.npz'),allow_pickle=False) as a:
        fields={key:a[f'{index}_before_{key}'] for key in ('labels','confidence','ground','solid','water','forbidden','raw_labels','raw_confidence')}
        original=[dict(item,mask=a[f'{index}_before_instance_{i}']) for i,item in enumerate(meta['before_instances'])]
        local=[dict(item,mask=a[f'{index}_portal_instance_{i}']) for i,item in enumerate(meta['portal']['detections'])]
        rgb=tuple(a[f'{index}_portal_rgb_{key}'] for key in ('labels','confidence','margin'))
        gray=tuple(a[f'{index}_portal_gray_{key}'] for key in ('labels','confidence','margin'))
        crop=tuple(int(v) for v in a[f'{index}_portal_box'])
    # No occlusion-box refinement participates in these saved building samples.
    assert not any(any(k in i['label'] for k in ('person','tree','bush')) for i in original)
    empty=np.zeros(fields['ground'].shape,bool)
    before=SegmentedMap(fields['ground'],fields['solid'],~(fields['ground']|fields['solid']),original,
        fields['labels'],fields['confidence'],{}, {},empty,empty,fields['water'],fields['forbidden'],
        fields['raw_labels'],fields['raw_confidence'])
    after,details=merge_portal_detail(before,meta['names'],crop,rgb,gray,local)
    for probe in meta['probes']:
        x,y=[round(v*meta['scale']) for v in probe['point']]
        if probe['expected']=='ground':assert after.ground[y,x],probe['id']
        if probe['expected']=='obstacle':assert after.obstacles[y,x] and not after.ground[y,x],probe['id']
    assert details['ground_candidates']>0
    if index==1:assert details['furniture_pixels_revised']>0
    assert np.array_equal(before.semantic_labels,fields['labels'])
    if index==1:
        from bot.perception.building_layout import building_layout,flat_navigation_masks
        steps=[i for i in after.instances if i.get('observation_source')=='portal_detail']
        assert details['transferred_stair_instances']==len(steps)==1
        assert steps[0]['label']=='stairs' and steps[0]['mask'].any()
        # The real local detector saw the wooden sill as steps. Preserve that
        # observation for navigation instead of discarding it after RGB fusion.
        flat=flat_navigation_masks(after,meta['names'])
        assert not flat.ground[steps[0]['mask']].any()
        assert flat.forbidden[steps[0]['mask']].all()
        layout=building_layout(after,meta['names'])
        assert any(c.get('boundary') for c in layout['connectors'])
