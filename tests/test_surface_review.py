"""Surface-review decisions, not live collision or world-geometry acceptance."""
from types import SimpleNamespace
from pathlib import Path
import json

import numpy as np
import pytest

from bot.perception.segmentation import fuse_masks, navigation_evidence
from bot.perception.surface_review import needs_surface_review, reconcile_surfaces
from experiments.route_models.segmentation_worker import review_artifacts


NAMES={0:'water',1:'floor',2:'rug',3:'wall',4:'door',5:'stairs',6:'shelf',7:'plant'}


def sample(original=0,reviewed=2,shape=(60,80)):
    labels=np.full(shape,original,np.uint8)
    confidence=np.full(shape,.96,np.float32)
    margin=np.full(shape,.8,np.float32)
    review=(np.full(shape,reviewed,np.uint8),np.full(shape,.85,np.float32),margin.copy())
    instance=dict(label='wooden floor',detection_score=.6,mask_score=.96,
                  mask=np.ones(shape,bool),box=[0,0,shape[1],shape[0]])
    return labels,confidence,margin,review,[instance]


def resolve(data,excluded=None):
    labels,confidence,margin,review,instances=data
    return reconcile_surfaces(labels,confidence,margin,review,NAMES,instances,excluded)


def test_color_water_conflict_requires_sam_and_review_keeps_actual_confidence():
    data=sample();labels,confidence,margin,review,instances=data
    before=[v.copy() for v in (labels,confidence,margin)]
    assert needs_surface_review(labels,confidence,margin,NAMES,instances)
    fused,conf,gap,sources,details=resolve(data)
    ground,solid,unknown=fuse_masks(fused,conf,gap,NAMES,instances)
    assert ground.all() and not solid.any() and not unknown.any()
    assert np.all(sources==1) and np.all(conf==review[1])
    assert not (conf>=.9).any()  # Cannot borrow RGB water confidence for metric calibration.
    assert details['water_conflicts_resolved']==labels.size
    assert details['world_geometry_verified'] is False
    assert all(np.array_equal(a,b) for a,b in zip(before,(labels,confidence,margin)))


def test_real_water_and_misclassified_edge_stay_water_when_both_views_agree():
    data=sample();data[3][0][:30,:]=0
    fused,conf,gap,sources,details=resolve(data)
    ground,solid,unknown=fuse_masks(fused,conf,gap,NAMES,data[4])
    _,water,_=navigation_evidence(fused,conf,NAMES,data[4],ground,solid,unknown)
    assert not ground.any() and water.all() and not sources.any()
    assert details['water_candidates_rejected']==30*80
    assert details['water_conflicts_resolved']==0


def test_protected_water_component_does_not_block_a_separate_dry_rug():
    data=sample();data[0][:,38:42]=3  # Separating physical bank/wall.
    data[3][0][:,:38]=0
    fused,conf,gap,sources,_=resolve(data)
    ground,solid,_=fuse_masks(fused,conf,gap,NAMES,data[4])
    assert not ground[:,:42].any() and ground[:,42:].all()
    assert solid[:,38:42].all() and not sources[:,:42].any()


@pytest.mark.parametrize('structural',[3,4,5])
def test_gray_floor_cannot_erase_strong_original_wall_door_or_stairs(structural):
    data=sample(original=structural,reviewed=1)
    fused,_,_,sources,_=resolve(data)
    assert np.array_equal(fused,data[0]) and not sources.any()


@pytest.mark.parametrize('change',[
    {'label':'building floor'}, {'label':'water floor'}, {'mask_score':.89},
    {'detection_score':.29}, {'mask_score':float('nan')}, {'mask':np.zeros((60,80),bool)}])
def test_unreliable_or_mixed_detector_support_does_not_release_ground(change):
    data=sample();data[4][0].update(change)
    assert not resolve(data)[3].any()


@pytest.mark.parametrize('fault',['no_instances','confidence','margin','unknown_review'])
def test_single_source_or_uncertain_review_does_not_release_ground(fault):
    data=sample()
    if fault=='no_instances':data[4].clear()
    if fault=='confidence':data[3][1][:]=.79
    if fault=='margin':data[3][2][:]=.19
    if fault=='unknown_review':data[3][0][:]=7
    assert not resolve(data)[3].any()


def test_weak_bookshelf_can_be_confirmed_but_gray_barrier_cannot_erase_good_floor():
    data=sample(original=6,reviewed=6);data[1][:]=.4
    fused,conf,gap,sources,_=resolve(data)
    _,solid,_=fuse_masks(fused,conf,gap,NAMES,[])
    assert solid.all() and np.all(sources==2)
    data[0][:]=1;data[1][:]=.98
    assert not resolve(data)[3].any()


def test_ui_is_never_released_and_small_local_patch_does_not_require_global_fraction():
    data=sample(shape=(900,1600));data[4][0]['mask'][:]=False
    data[4][0]['mask'][400:420,800:820]=True
    excluded=np.zeros(data[0].shape,bool);excluded[400:410,800:820]=True
    assert needs_surface_review(data[0],data[1],data[2],NAMES,data[4],excluded)
    sources=resolve(data,excluded)[3]
    assert not sources[excluded].any() and np.count_nonzero(sources)==200


@pytest.mark.parametrize('fault',['shape','nan'])
def test_corrupt_review_fails_instead_of_synthesizing_a_map(fault):
    data=list(sample());review=list(data[3])
    if fault=='shape':review[0]=review[0][:-1]
    else:review[1][0,0]=np.nan
    data[3]=review
    with pytest.raises(ValueError,match='surface_review_invalid'):resolve(data)


def test_archive_omits_missing_review_instead_of_pickled_none_or_fake_evidence(tmp_path):
    old=SimpleNamespace(semantic_confidence=np.ones((2,2)))
    assert review_artifacts(old)=={}
    old.raw_semantic_labels=np.zeros((2,2),np.uint8);old.review_sources=None
    arrays=review_artifacts(old)
    assert list(arrays)==['raw_labels']
    path=tmp_path/'masks.npz';np.savez_compressed(path,**arrays)
    with np.load(path,allow_pickle=False) as saved:assert np.array_equal(saved['raw_labels'],old.raw_semantic_labels)


def test_normalized_semantic_names_keep_water_forbidden_and_rug_navigable():
    labels=np.array([[0,1],[0,1]],np.uint8);conf=np.full((2,2),.96);gap=np.full((2,2),.8)
    names={'0':' water ','1':' rug '}
    ground,solid,unknown=fuse_masks(labels,conf,gap,names,[])
    _,water,_=navigation_evidence(labels,conf,names,[],ground,solid,unknown)
    assert water[:,0].all() and ground[:,1].all()
    assert not (ground&water).any()


@pytest.mark.parametrize('name',['goldshire-upstairs','stormwind-canal'])
def test_saved_local_semantics_distinguish_rug_conflict_from_corroborated_river(name):
    fixture=Path(__file__).parent/'fixtures/surface-water-review-20261118'
    meta=json.loads(fixture.with_suffix('.json').read_text());names=meta['names']
    with np.load(fixture.with_suffix('.npz'),allow_pickle=False) as a:
        labels=a[name+'_rgb_labels'];confidence=a[name+'_rgb_confidence']
        review=tuple(a[name+'_gray_'+k] for k in ('labels','confidence','margin'))
        # Deliberately overbroad water support: even a SAM floor spill cannot
        # authorize those pixels. This is fault injection, not detector success.
        support=(a[name+'_ground_support'] if name=='goldshire-upstairs'
                 else np.ones(labels.shape,bool))
    margin=np.maximum(0,2*confidence-1)  # Valid lower bound; original margin was not saved.
    instances=[dict(label='floor',mask=support,mask_score=.96,detection_score=.6)]
    fused,conf,gap,sources,details=reconcile_surfaces(labels,confidence,margin,review,names,instances)
    ground,solid,unknown=fuse_masks(fused,conf,gap,names,[])
    _,water,_=navigation_evidence(fused,conf,names,[],ground,solid,unknown)
    if name=='goldshire-upstairs':
        assert ground[344,673] and not water[344,673]
        assert details['water_conflicts_resolved']>0
    else:
        # The first unguarded implementation opened 544 real-river edge pixels
        # in x=50:112,y=611:632. Verify the complete patch, not only two probes.
        assert not ground[611:632,50:112].any()
        assert water[611:632,50:112].all()
        assert details['water_candidates_rejected']>0
