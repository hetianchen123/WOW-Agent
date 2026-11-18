from dataclasses import asdict,replace
import hashlib
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.control import segmented_detour as sd
from bot.control.pathfinding import GridRouter
from bot.core.config import SegmentedNavigationConfig
from bot.perception.travel_channel import TaskFlags,TravelFlags
from bot.perception.live_projection import read_bundle,rebase_projection
from bot.perception.ground_projection import ProjectionError
from bot.projection_cli import build_bundle,bind_approach_region
from test_ground_calibration import scene
from test_npc_obstacle import npc


def measured_bundle(tmp_path):
    ref,training,validation,_=scene()
    # Test-only injected numeric/texture data. Native-source flags here exercise
    # native gates; they do NOT constitute actual game calibration.
    for i,s in enumerate([ref,*training,*validation]):
        folder=tmp_path/str(i);folder.mkdir()
        cv2.imwrite(str(folder/'image.png'),cv2.cvtColor(s.image,cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(folder/'ground.png'),s.ground_mask)
        frame=replace(s.frame,image_sha256=hashlib.sha256((folder/'image.png').read_bytes()).hexdigest())
        data=dict(frame=asdict(frame),sequence=s.sequence,telemetry_at=s.telemetry_at,reviewed=True,
            source='native_position_ground_correspondence',
            mask_sha256=hashlib.sha256((folder/'ground.png').read_bytes()).hexdigest())
        (folder/'sample.json').write_text(json.dumps(data))
    manifest=tmp_path/'session.json'
    manifest.write_text(json.dumps(dict(reference='0',training=[str(i) for i in range(1,7)],
                                       validation=[str(i) for i in range(7,10)])))
    build_bundle(manifest,tmp_path/'built')
    return tmp_path/'built/bundle.json'


def test_native_bundle_portable_reference_and_real_frame_rebase_contract(tmp_path):
    path=measured_bundle(tmp_path);bundle=read_bundle(path)
    old,ref,_,_=bundle
    current=replace(ref.frame,frame_id='new',captured_at=1000,camera_signature=(4,32,55),player_feet=None)
    projection,evidence=rebase_projection(bundle,current,ref.image,ref.ground_mask.astype(bool),now=1001)
    assert projection.frame.frame_id=='new'
    assert np.linalg.norm(projection.delta(projection.frame.player_feet))<.001
    assert evidence['combined_bound_yards']<.01
    assert evidence['reference_frame']!=evidence['current_frame']
    with pytest.raises(ProjectionError,match='local_scene_changed'):
        rebase_projection(bundle,replace(current,height=current.height+2),ref.image,ref.ground_mask.astype(bool),now=1001)
    with pytest.raises(ProjectionError,match='expired'):
        rebase_projection(bundle,current,ref.image,ref.ground_mask.astype(bool),now=1020)
    (path.parent/'reference-ground.png').write_bytes(b'tampered')
    with pytest.raises(ProjectionError,match='hash_mismatch'):read_bundle(path)


def test_static_region_preparation_is_explicit_and_bound_to_reference(tmp_path):
    path=measured_bundle(tmp_path)
    with pytest.raises(ProjectionError,match='review_required'):
        bind_approach_region(path,(400,300),12345,'READY',0,.8,.25,False)
    result=bind_approach_region(path,(400,300),12345,'READY',0,.8,.25,True)
    data=json.loads(path.read_text())
    assert result['target_key']=='12345:READY:0' and not result['interaction_verified']
    assert data['targets'][result['target_key']]['bundle_image_sha256']==data['frame']['image_sha256']


def controller(tmp_path,monkeypatch):
    _,ctx,_,nav,_=npc(tmp_path,monkeypatch)
    nav=replace(nav,camera=SimpleNamespace(view=4,zoom=32,request=0),height=10,strafe_verified=True,
                calibration_orbit_available=True)
    monkeypatch.setattr(sd,'preflight',lambda *_:'test-ground')
    created=[]
    class Request:
        def __init__(self,image,frame,job):
            self.frame,self.job,self.closed=frame,job,False
            self.result=dict(frame_id=frame.frame_id,executable=False,requires_fresh_feedback=True,
                kind='route',target_key='12345:READY:0',calibration_source='automatic_native_motion',
                automatic_goal=dict(goal_kind='automatic_npc_search_region'),
                registration=dict(current_frame=frame.frame_id,combined_bound_yards=.1,
                                  fit_movements=2,validation_movements=2),
                movement_budget_yards=.75,next_segment_world=[list(nav.position),[nav.position[0]+.0005,nav.position[1]]])
            created.append(self)
        def poll(self):return self.result
        def close(self):self.closed=True
    obj=sd.SegmentedDetour(1,nav,GridRouter(1000,1000),'ccw_west',SegmentedNavigationConfig(),
        lambda _:np.zeros((100,100,3),np.uint8),deadline=61,request_factory=Request)
    obj.overhead=SimpleNamespace(step=lambda *_:None,used=True)
    return obj,ctx,nav,created


def ready(obj,ctx,nav):
    heading=1.5*np.pi
    assert obj.step(ctx,1,nav,heading).tap is None
    nav=replace(nav,sequence=2)
    assert obj.step(ctx,1.1,nav,heading).tap is None
    return nav,heading


def test_task_controller_uses_fresh_capture_one_pulse_and_reobserves(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    assert obj.step(ctx,1.2,nav,heading).tap is None
    command=obj.step(ctx,1.3,nav,heading)
    assert command.tap=='w' and 0<command.hold_s<=.07 and command.precise
    assert obj.step(ctx,1.4,nav,heading).tap is None
    fresh=replace(nav,sequence=3,position=(nav.position[0]+.00045,nav.position[1]))
    assert obj.step(ctx,1.9,fresh,heading).tap is None
    assert obj.plan is None and obj.total==pytest.approx(.45)
    assert obj.step(ctx,2,fresh,heading).tap is None
    assert obj.pending is not None  # Does not continue the old route.


def test_worker_frame_clock_is_monotonic_even_when_task_clock_is_wall_time(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    monkeypatch.setattr(sd.time,'monotonic',lambda:1234.5)
    nav,heading=ready(obj,ctx,nav)
    assert requests[0].frame.captured_at==1234.5


def test_current_image_translation_reanchors_world_coordinates(tmp_path):
    path=measured_bundle(tmp_path);bundle=read_bundle(path)
    old,ref,_,_=bundle
    dx,dy=12,-8
    matrix=np.float32([[1,0,dx],[0,1,dy]])
    image=cv2.warpAffine(ref.image,matrix,ref.frame.size)
    mask=cv2.warpAffine(ref.ground_mask,matrix,ref.frame.size).astype(bool)
    # World player has moved under a following camera; its screen anchor stays fixed.
    delta=old.delta((400-dx,300-dy))
    position=tuple(float(o+d/s) for o,d,s in zip(old.frame.player_position,delta,old.frame.world_size))
    current=replace(ref.frame,frame_id='moved',captured_at=1000,player_position=position,player_feet=None)
    projection,_=rebase_projection(bundle,current,image,mask,now=1001)
    assert np.allclose(projection.frame.player_feet,(400,300),atol=.1)
    # A fixed ground landmark shifts in the image but retains its world location.
    old_world=np.array(old.frame.player_position)+old.delta((450,320))/np.array(old.frame.world_size)
    new_world=np.array(position)+projection.delta((450+dx,320+dy))/np.array(old.frame.world_size)
    assert np.allclose(old_world,new_world,atol=1e-5)


@pytest.mark.parametrize('bad',['stale_frame','nan','over_budget','unmeasured','bad_registration'])
def test_bad_or_old_plan_never_reaches_input(tmp_path,monkeypatch,bad):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    if bad=='stale_frame':requests[0].result['frame_id']='old'
    if bad=='nan':requests[0].result['next_segment_world'][1][0]=float('nan')
    if bad=='over_budget':requests[0].result['movement_budget_yards']=2
    if bad=='unmeasured':requests[0].result['calibration_source']='synthetic_regression'
    if bad=='bad_registration':requests[0].result['registration']['combined_bound_yards']=1
    result=obj.step(ctx,1.2,nav,heading)
    assert result.outcome and not result.tap and not result.keys


def test_interruption_and_native_range_cancel_pending_local_request(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav,heading=ready(obj,ctx,nav)
    assert obj.step(ctx,1.2,replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT),heading).outcome=='probe_interrupted'
    receipt=replace(nav,task=nav.task|TaskFlags.NPC_WITHIN_5_YARDS)
    assert obj.step(ctx,1.3,receipt,heading).outcome=='detoured'
    assert requests[0].closed and obj.closed


def test_preflight_missing_local_models_does_not_fallback_to_cloud(tmp_path):
    config=SegmentedNavigationConfig(enabled=True,models=str(tmp_path/'missing.json'))
    with pytest.raises(ValueError,match='models_missing'):sd.preflight(config)


def test_npc_task_routes_to_local_segmentation_without_cloud_or_qwen(tmp_path,monkeypatch):
    from bot.modules import quest
    run,ctx,_,nav,_=npc(tmp_path,monkeypatch)
    ctx.cfg.segmented_navigation=SegmentedNavigationConfig(enabled=True)
    monkeypatch.setattr(quest,'local_vision_available',lambda:False)
    monkeypatch.setattr(quest,'cloud_route_available',lambda _:False)
    monkeypatch.setattr(sd,'preflight',lambda *_:'test-plane')
    constructed=[]
    class Local:
        def __init__(self,*args,**kw):self.closed=False;constructed.append(self)
        def close(self):self.closed=True
    monkeypatch.setattr(sd,'SegmentedDetour',Local)
    run.begin_npc_detour(ctx,1,nav,'range_not_reached')
    assert run.npc_detouring and constructed
    assert run.terrain.phase=='model_route' and run.terrain.model_detour is constructed[0]
    run.terrain.close()
    assert constructed[0].closed


def test_task_preflight_failure_releases_inputs_without_trying_a_model(tmp_path,monkeypatch):
    from bot.modules import quest
    run,ctx,_,nav,events=npc(tmp_path,monkeypatch)
    ctx.cfg.segmented_navigation=SegmentedNavigationConfig(enabled=True,models=str(tmp_path/'missing'))
    monkeypatch.setattr(quest,'cloud_route_available',lambda _:False)
    run.set_travel_keys(ctx,frozenset({'w'}))
    run.begin_npc_detour(ctx,1,nav,'range_not_reached')
    assert not ctx.input._held_keys and run.terrain is None
    assert any(name=='quest_segmented_preflight_failed' for name,_ in events)


def test_worker_connects_local_masks_to_registered_world_prefix_and_reuses_engine(tmp_path,monkeypatch,capsys):
    import io
    from experiments.route_models import segmentation_worker as worker
    path=measured_bundle(tmp_path)
    bind_approach_region(path,(460,300),12345,'READY',0,.8,.25,True)
    _,reference,_,_=read_bundle(path)
    frame=replace(reference.frame,frame_id='worker-current',captured_at=1000,player_feet=None)
    loaded=[]
    class Segmenter:
        # Synthetic segmentation fixture: tests pipeline integration, not model accuracy.
        def __init__(self,*args,**kwargs):loaded.append(self);self.load_s=0
        def inspect(self,rgb,**kwargs):
            shape=rgb.shape[:2]
            return SimpleNamespace(ground=np.ones(shape,bool),semantic_confidence=np.ones(shape),
                obstacles=np.zeros(shape,bool),occluded=np.zeros(shape,bool),water=np.zeros(shape,bool),
                forbidden=np.zeros(shape,bool),timings={})
    models=tmp_path/'models.json';models.write_text('{}')
    job=dict(frame=asdict(frame),bundle=str(path),quest_id=12345,phase='READY',objective_index=0,
        models=str(models),checkpoint='synthetic-test-only',device='cpu',convention='ccw_west')
    requests=[dict(id=str(i),images=[str(path.parent/'reference.png')],target=job) for i in range(2)]
    monkeypatch.setattr(worker,'LocalSegmenter',Segmenter)
    monkeypatch.setattr(worker.time,'monotonic',lambda:1001)
    monkeypatch.setattr(worker.sys,'stdin',io.StringIO('\n'.join(json.dumps(r) for r in requests)))
    worker.main()
    replies=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert replies[0]['ready'] and len(loaded)==1
    for i,reply in enumerate(replies[1:]):
        result=reply['cases'][0]
        assert result['model_timings']['engine_reused']==bool(i)
        assert result['registration']['current_frame']==frame.frame_id
        assert result['target_key']=='12345:READY:0'
        assert not result['executable'] and len(result['next_segment_world'])>=2
        assert result['movement_budget_yards']<=.75
        assert np.allclose(result['next_segment_world'][0],frame.player_position,atol=1e-6)
