"""Historical native image replay and cache corruption/scene-change regressions."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.perception.automatic_ground import AutomaticGround
from bot.perception.calibration_cache import save_cache,load_cache,MAX_AGE_S
from bot.perception.ground_projection import ProjectionFrame,ProjectionError


def recorded():
    path=Path(__file__).parent/'fixtures/native-calibration-20261117-1814'
    meta=json.loads(path.with_suffix('.json').read_text())
    with np.load(path.with_suffix('.npz')) as data:
        for i,v in enumerate(meta['frames']):
            for key in ('size','world_size','player_position','camera_signature'):v[key]=tuple(v[key])
            g=data[f'ground{i}']
            masks=SimpleNamespace(ground=g,semantic_confidence=np.ones(g.shape),instances=[])
            if i==7:
                # Registration uses the stricter confidence subset; routing
                # needs the ORIGINAL fused segmentation, including occlusion.
                masks.ground=data['route_ground7'];masks.semantic_confidence=data['semantic_confidence7']
                for key in ('obstacles','occluded','water','forbidden'):setattr(masks,key,data[key+'7'])
            yield ProjectionFrame(**v),data[f'image{i}'],masks


@pytest.fixture(scope='module')
def measured():
    rows=list(recorded());cal=AutomaticGround()
    for frame,rgb,masks in rows[:7]:
        p,e=cal.update(frame,rgb,masks,now=frame.captured_at+.1)
    assert p is not None and e['validation_movements']==2
    assert e['combined_bound_yards']==pytest.approx(.08020296750929343)
    return cal,rows[7]


def test_native_cache_replays_then_requires_and_passes_a_new_image(measured,tmp_path):
    cal,(frame,rgb,masks)=measured;path=tmp_path/'cache.npz'
    identity=save_cache(path,cal,created_at=1000,provenance='historical_test_fixture')
    # New process clock is simulated. Original evidence is not relabelled.
    frame=replace(frame,captured_at=20_000_000)
    loaded=load_cache(path,frame,now=frame.captured_at,wall_time=1100)
    assert loaded.reference.frame.captured_at==cal.reference.frame.captured_at
    assert loaded.projection.frame.frame_id!=frame.frame_id
    p,e=loaded.update(frame,rgb,masks,now=frame.captured_at+.1)
    assert p is not None and e['current_frame']==frame.frame_id
    assert e['restored_cache']==identity and e['combined_bound_yards']<.35
    assert e['fit_frames']==[list(p.ids) for p in cal.training]


@pytest.mark.parametrize('fault',['map','zoom','height','distance','age','future'])
def test_cache_never_skips_scene_and_age_checks(measured,tmp_path,fault):
    cal,(frame,_,_)=measured;path=tmp_path/'cache.npz'
    save_cache(path,cal,created_at=1000);wall=1100
    if fault=='map':frame=replace(frame,map_id=99)
    if fault=='zoom':frame=replace(frame,camera_signature=(4,24,0))
    if fault=='height':frame=replace(frame,height=frame.height+1)
    if fault=='distance':frame=replace(frame,player_position=(.8,.8))
    if fault=='age':wall=1001+MAX_AGE_S
    if fault=='future':wall=900
    with pytest.raises(ProjectionError):load_cache(path,frame,now=frame.captured_at,wall_time=wall)


@pytest.mark.parametrize('fault',['image','native_holdout','version','reference'])
def test_cache_replays_evidence_instead_of_trusting_a_saved_success_flag(measured,tmp_path,fault):
    cal,(frame,_,_)=measured;path=tmp_path/'cache.npz';save_cache(path,cal,created_at=1000)
    with np.load(path,allow_pickle=False) as z:values={key:z[key].copy() for key in z.files}
    meta=json.loads(str(values['metadata']))
    if fault=='image':values['image3'][0,0,0]^=255
    if fault=='native_holdout':meta['frames'][5]['player_position'][0]+=.01
    if fault=='version':meta['algorithm']='changed'
    if fault=='reference':meta['reference_id']='not-last'
    values['metadata']=np.array(json.dumps(meta));np.savez_compressed(path,**values)
    with pytest.raises(ProjectionError):load_cache(path,frame,now=frame.captured_at,wall_time=1100)


def test_current_unrelated_image_discards_cached_scale(measured,tmp_path):
    cal,(frame,rgb,masks)=measured;path=tmp_path/'cache.npz';save_cache(path,cal,created_at=1000)
    loaded=load_cache(path,frame,now=frame.captured_at,wall_time=1100)
    unrelated=np.random.default_rng(48).integers(0,256,rgb.shape,dtype=np.uint8)
    p,e=loaded.update(frame,unrelated,masks,now=frame.captured_at+.1)
    assert p is None and e['stage']=='orbit' and loaded.cache_id is None


def test_failed_calibration_cannot_be_saved(tmp_path):
    with pytest.raises(ProjectionError,match='unverified'):
        save_cache(tmp_path/'bad.npz',AutomaticGround(),created_at=1000)


@pytest.mark.parametrize('corrupt',[False,True])
def test_worker_uses_cache_only_after_current_frame_validation(measured,tmp_path,monkeypatch,capsys,corrupt):
    from dataclasses import asdict
    import cv2
    from experiments.route_models import segmentation_worker as worker
    cal,(frame,rgb,masks)=measured;path=tmp_path/'cache.npz'
    save_cache(path,cal,created_at=1000)
    if corrupt:path.write_bytes(b'not-a-cache')
    image=tmp_path/'image.png';cv2.imwrite(str(image),cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
    models=tmp_path/'models.json';models.write_text('{}')
    masks.timings={}
    monkeypatch.setattr(worker,'LocalSegmenter',lambda *_,**__:SimpleNamespace(load_s=0,inspect=lambda *_,**__:masks))
    monkeypatch.setattr(worker,'time',SimpleNamespace(monotonic=lambda:frame.captured_at+.1,time=lambda:1100))
    monkeypatch.setattr(worker.sys,'stdin',[json.dumps(dict(id='cached',images=[str(image)],target=dict(
        automatic=True,frame=asdict(frame),models=str(models),checkpoint='test',device='cpu',
        session_id='test',calibration_cache=str(path),quest_id=90895,phase='READY',objective_index=0,
        convention='ccw_west',destination=[.2466003862384665,.37109982595427127],strafe_verified=True)))])
    worker.main();reply=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert 'error' not in reply
    result=reply['cases'][0]
    if corrupt:
        assert result['kind']=='calibration_orbit' and result['calibration_cache_status'].startswith('not_reused:')
    else:
        assert result['kind']=='route' and result['calibration_cache_status']=='reused_with_current_image_validation'
        assert result['registration']['current_frame']==frame.frame_id
        assert result['execution_window']['requires_probe']==result['approach']['requires_probe']
