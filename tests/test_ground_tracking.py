"""Real 16:03 rotation replay and adversarial local-ground correspondence tests."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.perception.automatic_ground import AutomaticGround,match,orbit_anchor,sample
from bot.perception.ground_calibration import transform
from bot.perception.ground_projection import ProjectionError,ProjectionFrame
from bot.perception.ground_tracking import validate_tracking,track_local_ground


def recorded():
    path=Path(__file__).parent/'fixtures/ground-tracking-20261117'
    frames=json.loads(path.with_suffix('.json').read_text())['frames']
    with np.load(path.with_suffix('.npz')) as values:
        for index,v in enumerate(frames):
            for key in ('size','world_size','player_position','camera_signature'):v[key]=tuple(v[key])
            frame=ProjectionFrame(**v)
            for_fast=values[f'fast_ground{index}']
            ground=values[f'ground{index}']
            yield frame,values[f'image{index}'],ground,for_fast


def masks(ground):
    return SimpleNamespace(ground=ground,semantic_confidence=np.ones(ground.shape,np.float32))


def test_real_rotation_pair_and_reshoot_agree_without_inventing_world_scale():
    frames=list(recorded())
    base=sample(frames[0][0],frames[0][1],masks(frames[0][2]))
    anchors=[]
    for frame,rgb,ground,_ in frames[1:]:
        matrix,evidence=match(sample(frame,rgb,masks(ground)),base,require_rotation=True)
        anchors.append(orbit_anchor(matrix,frame.size))
        assert evidence['method']=='local_photometric_tracking'
        assert evidence['matches']>=200
        assert evidence['fit_ratio']>=.9 and evidence['holdout_ratio']>=.9
        assert evidence['holdout_max_px']<=2 and evidence['fit_max_px']<=2
        assert len(evidence['source_matches'])+evidence['rejected_matches']==evidence['matches']
    assert np.linalg.norm(anchors[0]-anchors[1])<2


def test_geometry_only_rotation_then_full_observation_does_not_count_reshoot_twice():
    calibration=AutomaticGround()
    stages=[]
    for frame,rgb,_,ground in recorded():
        projection,evidence=calibration.update(frame,rgb,masks(ground),now=frame.captured_at+.1)
        assert projection is None
        stages.append(evidence.get('reason'))
    assert stages==['automatic_ground_pivot_required','independent_pivot_check','independent_rotation_required']
    assert len(calibration.anchors)==1 and calibration.anchor is None and calibration.matrix is None


def correspondences():
    x,y=np.meshgrid(np.linspace(210,790,20),np.linspace(260,540,12))
    points=np.column_stack([x.ravel(),y.ravel()])
    matrix=np.array([[.99,-.1,50],[.1,.99,-40],[0,0,1.]])
    return points,transform(matrix,points)


def test_few_bad_matches_are_reported_and_never_exported_as_measured_ground():
    source,destination=correspondences()
    destination[[0,17,36]]+=[15,-20]
    _,evidence=validate_tracking(source,destination,(1012,586))
    assert evidence['rejected_matches']==3 and evidence['holdout_rejected']==1
    assert len(evidence['source_matches'])==len(source)-3
    assert evidence['raw_holdout_max_px']>20 and evidence['holdout_max_px']<.01


@pytest.mark.parametrize('fault',['validation','training','mixed_planes','mirror','cluster'])
def test_inconsistent_or_unsupported_geometry_never_authorizes_projection(fault):
    source,destination=correspondences()
    train=np.arange(len(source))%5!=0
    if fault=='validation':destination[~train]+=[15,0]
    if fault=='training':destination[np.flatnonzero(train)[::3]]+=[15,0]
    if fault=='mixed_planes':destination[::2]+=[15,0]
    if fault=='mirror':destination[:,0]=1012-destination[:,0]
    if fault=='cluster':source=source*.05;destination=destination*.05
    with pytest.raises(ProjectionError):validate_tracking(source,destination,(1012,586))


def test_sparse_seed_alone_cannot_make_unrelated_textures_a_ground_plane():
    rng=np.random.default_rng(65)
    a=rng.integers(0,256,(400,600,3),dtype=np.uint8)
    b=rng.integers(0,256,(400,600,3),dtype=np.uint8)
    points,_=correspondences();points*=.5
    mask=np.full((400,600),255,np.uint8)
    with pytest.raises(ProjectionError):
        track_local_ground(SimpleNamespace(image=a,ground_mask=mask),
            SimpleNamespace(image=b,ground_mask=mask),seed=(points,points))


@pytest.mark.parametrize('ready',[False,True])
def test_geometry_only_maps_cannot_issue_a_probe_or_route(ready,monkeypatch):
    from experiments.route_models import segmentation_worker as worker
    frame,rgb,ground,_=next(recorded())
    observed=masks(ground);observed.diagnostics={'geometry_only':True}
    evidence={'stage':'ready' if ready else 'translation','anchor':[500,470]}
    calibration=SimpleNamespace(update=lambda *_,**__:(object() if ready else None,evidence))
    with pytest.raises(ProjectionError,match='geometry_only_cannot_authorize_movement'):
        worker.automatic_job(calibration,frame,rgb,observed,
            {'quest_id':90895,'phase':'READY','objective_index':0})


def test_worker_uses_fast_geometry_only_until_first_rotation_is_confirmed(tmp_path,monkeypatch,capsys):
    import hashlib
    from dataclasses import asdict
    from experiments.route_models import segmentation_worker as worker
    calls=[];model=SimpleNamespace(load_s=0)
    model.inspect_geometry=lambda _:calls.append('geometry') or SimpleNamespace(
        **masks(np.ones((20,20),bool)).__dict__,obstacles=np.zeros((20,20),bool),
        occluded=np.zeros((20,20),bool),water=np.zeros((20,20),bool),forbidden=np.ones((20,20),bool),timings={})
    model.inspect=lambda *_,**__:calls.append('full') or SimpleNamespace(
        **masks(np.ones((20,20),bool)).__dict__,obstacles=np.zeros((20,20),bool),
        occluded=np.zeros((20,20),bool),water=np.zeros((20,20),bool),forbidden=np.zeros((20,20),bool),timings={})
    monkeypatch.setattr(worker,'LocalSegmenter',lambda *_,**__:model)
    def completed(calibration,*_):
        if len(calls)==2:calibration.anchors.append([10,10])
        return {'kind':'calibration_orbit'}
    monkeypatch.setattr(worker,'automatic_job',completed)
    source,_,_,_=next(recorded());requests=[]
    models=tmp_path/'models.json';models.write_text('{}')
    for i in range(3):
        folder=tmp_path/str(i);folder.mkdir();path=folder/'image.png'
        cv2.imwrite(str(path),np.zeros((20,20,3),np.uint8))
        frame=replace(source,size=(20,20),frame_id=str(i),image_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        requests.append(json.dumps({'id':str(i),'images':[str(path)],'target':{
            'automatic':True,'frame':asdict(frame),'models':str(models),'checkpoint':'mock',
            'device':'cpu','session_id':'test'}}))
    monkeypatch.setattr(worker.sys,'stdin',requests)
    worker.main()
    assert calls==['geometry','geometry','full']
    replies=[json.loads(row) for row in capsys.readouterr().out.splitlines()]
    assert not any('error' in row for row in replies)
