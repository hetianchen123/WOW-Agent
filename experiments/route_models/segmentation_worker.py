"""Persistent LOCAL SAM worker. Only saved images and measured bundles; no input APIs."""
import json
from pathlib import Path
import sys
import hashlib
import time
from dataclasses import replace
from zipfile import BadZipFile

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.perception.segmentation import LocalSegmenter
from bot.perception.ground_projection import ProjectionFrame, ProjectionError
from bot.perception.live_projection import read_bundle,rebase_projection
from bot.control.projected_route import interaction_region,plan_segmented_approach
from bot.perception.automatic_ground import AutomaticGround
from bot.perception.calibration_cache import load_cache,save_cache
from bot.decision.automatic_approach import calibration_probe,npc_search_region
from bot.perception.building_scene import (BUILDING_PROMPT, BUILDING_REOBSERVE,
    describe_building_scene, building_ui_exclusion, building_ground_measurement, turnaway_view)
from bot.decision.building_navigation import building_navigation_region
from bot.perception.building_layout import flat_navigation_masks
from bot.decision.image_navigation import ImageRouteError
from bot.decision.stair_probe import observe_stair_trial
from bot.control.navigation import heading_error
import math


def review_artifacts(observed):
    """Optional evidence must not become object arrays or invented review maps."""
    fields={'raw_labels':'raw_semantic_labels',
            'raw_confidence':'raw_semantic_confidence','review_sources':'review_sources',
            'fused_labels':'semantic_labels'}
    arrays={key:value for key,attribute in fields.items()
            if isinstance(value:=getattr(observed,attribute,None),np.ndarray)}
    arrays.update({key:value for key,value in (getattr(observed,'portal_evidence',None) or {}).items()
                   if key.startswith('portal_') and isinstance(value,np.ndarray) and value.dtype!=object})
    return arrays


def automatic_job(calibrator,frame,rgb,observed,job):
    projection,evidence=calibrator.update(frame,rgb,observed,now=time.monotonic())
    common=dict(frame_id=frame.frame_id,executable=False,requires_fresh_feedback=True,
                calibration=evidence,target_key=f"{job['quest_id']}:{job['phase']}:{job.get('objective_index') or 0}")
    if projection is None:
        if evidence['stage']=='orbit':
            return dict(common,kind='calibration_orbit',yaw_pixels=evidence['yaw_pixels'])
        if evidence['stage']=='observe':
            return dict(common,kind='calibration_observe',wait_s=evidence['wait_s'])
        if getattr(observed,'diagnostics',{}).get('geometry_only'):
            raise ProjectionError('geometry_only_cannot_authorize_movement')
        probe=calibration_probe(observed,evidence['anchor'],job.get('motion_history',[]),
                                strafe_verified=job.get('strafe_verified') is True,
                                anchor_error=calibrator.anchor_error,blocked_keys=job.get('blocked_probe_keys',()))
        return dict(common,**probe)
    if getattr(observed,'diagnostics',{}).get('geometry_only'):
        raise ProjectionError('geometry_only_cannot_authorize_movement')
    for cell in job.get('blocked',[]):
        pixel=np.linalg.solve(projection.matrix,[*projection.metric_delta(cell),1])
        if abs(pixel[2])<1e-8:continue
        point=tuple(int(round(v)) for v in pixel[:2]/pixel[2])
        mask=observed.forbidden.astype(np.uint8)
        cv2.circle(mask,point,16,1,-1)
        observed.forbidden=mask.astype(bool)
    if job.get('goal_mode') in {'building_entry','building_cross','building_search'}:
        region,goal=building_navigation_region(projection,observed,rgb,job.get('destination'),job['building_scene'],
            crossing=job['goal_mode']=='building_cross',searching=job['goal_mode']=='building_search',
            visited=job.get('visited',[]),passages=job.get('passages',[]),crossing_origin=job.get('crossing_origin'),
            navigation_context=job.get('navigation_context'),returning=job.get('return_passage_id'))
    else:
        region,goal=npc_search_region(projection,observed,rgb,job.get('destination'),
                                     visited=job.get('visited',[]))
    result=plan_segmented_approach(projection,observed,region,projection.frame,
        now=time.monotonic(),frame_id=frame.frame_id,image_sha256=frame.image_sha256,
        convention=job['convention'],max_step_yards=2.)
    result.update(common,kind='route',registration=evidence,calibration_source=projection.source,
                  automatic_goal=goal,projection_matrix=projection.matrix.tolist())
    result['search_goal_world']=result['world_route']['world_points'][-1]
    probing=result['approach']['requires_probe']
    result['execution_window']=dict(schema=1,requires_probe=probing,cross_track_yards=.15,
                                    max_age_s=12,max_pulses=4 if probing else 8)
    return result


def building_job(calibrator,frame,rgb,observed,job):
    """Local measurement and entrance recognition are independent prerequisites.

    Measurement can continue when a doorway leaves the view during an orbit.
    Only a current-frame doorway can select an approach route. Calibrating the
    ground never treats an old screen box or the task pin as an entry target.
    """
    scene=job['building_scene']
    if scene['surface']=='structure':raise ProjectionError('building_not_ground_surface')
    measurement=building_ground_measurement(rgb,observed,full_window=job.get('full_window') is True,
                                            hidden_ui_flags=job.get('visual_ui_hidden'))
    try:
        # Area is diagnostic only. An ongoing measurement uses real matched
        # points/holdouts; do not discard it because an effect hid other ground.
        measurement['minimum_features']=16 if calibrator.base is not None else 40
        if measurement['texture_features']<measurement['minimum_features']:
            raise ProjectionError('auto_ground_texture_insufficient')
        result=automatic_job(calibrator,frame,rgb,observed,job)
    except (ProjectionError,ImageRouteError) as exc:
        if str(exc) not in BUILDING_REOBSERVE:raise
        result=dict(kind='building_reobserve',reason=str(exc),executable=False,
            requires_fresh_feedback=True,target_key=f"{job['quest_id']}:{job['phase']}:{job.get('objective_index') or 0}")
        if str(exc).startswith('building_return_'):
            context=job.get('navigation_context') or {}
            result['return_passage_id']=(context.get('return_edge') or {}).get('passage_id')
    return dict(result,building_scene=scene,ground_measurement=measurement,frame_id=frame.frame_id)


def building_rotation_only(calibrator,frame,job):
    # The base frame already has the full obstacle and surface review. Only the
    # FIRST overhead rotation can never authorize a probe, route or stair step.
    return (frame.camera_signature[0]==4 and job.get('stair_trial') is None
            and calibrator.base is not None and not calibrator.anchors
            and calibrator.reference is None and not calibrator.observation_retries)


def stationary_building_job(calibrator,frame,rgb,observed,job):
    try:
        result=automatic_job(calibrator,frame,rgb,observed,job)
    except ProjectionError as exc:
        if str(exc) not in BUILDING_REOBSERVE:raise
        # Recheck uncertain geometry once on a fresh full segmentation. Its
        # door/ground evidence is needed to choose a relocation or alternate view.
        calibrator.observation_retries=1
        result=dict(kind='calibration_observe',wait_s=.7,frame_id=frame.frame_id,
            executable=False,requires_fresh_feedback=True,
            target_key=f"{job['quest_id']}:{job['phase']}:{job.get('objective_index') or 0}",
            calibration=dict(stage='observe',reason=str(exc),wait_s=.7))
    if result['kind'] not in {'calibration_orbit','calibration_observe'}:
        raise ProjectionError('geometry_only_cannot_authorize_movement')
    return dict(result,stationary_only=True)


def stair_job(calibrator,frame,rgb,observed,job,names,*,now=None):
    """A separately bounded trial after reaching a measured connector approach.

    Recheck the preceding measured viewport anchor with stationary turns. If
    that view has no measured anchor, first measure the adjacent floor with
    the ordinary guarded probes. No floor homography is extended onto treads.
    """
    now=time.monotonic() if now is None else now
    if frame.camera_signature[0]!=4 or not 0<=now-frame.captured_at<=15:
        raise ProjectionError('stair_trial_observation_invalid')
    common=dict(frame_id=frame.frame_id,executable=False,requires_fresh_feedback=True,
        target_key=f"{job['quest_id']}:{job['phase']}:{job.get('objective_index') or 0}",
        building_scene=job['building_scene'])
    state=job['stair_trial']
    prior=state.get('anchor_reference')
    usable_prior=(isinstance(prior,dict) and prior.get('size')==list(frame.size)
        and prior.get('camera')==list(frame.camera_signature[:2])
        and isinstance(prior.get('source_frame'),str) and bool(prior['source_frame'])
        and isinstance(prior.get('point'),list) and len(prior['point'])==2
        and type(prior.get('error_px')) in (int,float) and 0<=prior['error_px']<=5)
    if calibrator.stair_reference is None:
        flat=flat_navigation_masks(observed,names)
        projection,evidence=calibrator.update(frame,rgb,flat,now=now)
        if evidence['stage']=='orbit':
            return dict(common,kind='calibration_orbit',calibration=evidence,yaw_pixels=evidence['yaw_pixels'])
        if evidence['stage']=='observe':
            return dict(common,kind='calibration_observe',calibration=evidence,wait_s=evidence['wait_s'])
        if usable_prior:
            if (not np.isfinite(prior['point']).all()
                    or np.linalg.norm(calibrator.anchor-np.asarray(prior['point']))>prior['error_px']+calibrator.anchor_error):
                raise ProjectionError('stair_trial_anchor_ambiguous')
        elif projection is None:
            probe=calibration_probe(flat,evidence['anchor'],job.get('motion_history',[]),
                strafe_verified=job.get('strafe_verified') is True,anchor_error=calibrator.anchor_error)
            return dict(common,calibration=evidence,**probe)
        calibrator.stair_reference=frame
    reference=calibrator.stair_reference
    if (frame.map_id!=reference.map_id or frame.world_size!=reference.world_size
            or frame.size!=reference.size or frame.camera_signature!=reference.camera_signature
            or abs(heading_error(reference.heading,frame.heading))>math.radians(1)
            or not 0<=frame.captured_at-reference.captured_at<=180):
        raise ProjectionError('stair_trial_pose_changed')
    result=observe_stair_trial(observed,names,calibrator.anchor.tolist(),
        advance=state.get('advance'),moved=state.get('moved',0.),anchor_error=calibrator.anchor_error)
    anchor_validation=dict(schema=1,current_frame=frame.frame_id,anchor_error_px=calibrator.anchor_error,
        source='prior_motion_anchor_and_orbits' if usable_prior else 'current_independent_motion',
        prior_frame=prior.get('source_frame') if usable_prior else None,
        fit_movements=len(calibrator.training),validation_movements=len(calibrator.validation))
    return dict(common,anchor_validation=anchor_validation,**result)


def main():
    engines={}
    calibrator=AutomaticGround()
    session=None
    cache_attempted=set()
    saved_references=set()
    print(json.dumps({'protocol':1,'ready':True}),flush=True)
    for line in sys.stdin:
        request={}
        path=None
        try:
            request=json.loads(line); job=request['target']
            path=Path(request['images'][0]); rgb=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
            if job.get('mode') in {'building_scene','building_turnaway_view'}:
                settings=(job['models'],job['checkpoint'],job['device'])
                reused=settings in engines
                if not reused:
                    engines.clear()
                    engines[settings]=LocalSegmenter(json.loads(Path(job['models']).read_text()),
                        job['checkpoint'],variant='large',sam_device=job['device'])
                model=engines[settings]
                if job['mode']=='building_turnaway_view':
                    observed=model.inspect_geometry(rgb)
                    result=turnaway_view(observed,model.semantic.config.id2label,job.get('visual_ui_hidden'))
                    result.update(frame_id=job['frame_id'],image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                  model_timings=observed.timings)
                    (path.parent/'scene.json').write_text(json.dumps(result,indent=2))
                    print(json.dumps({'id':request['id'],'cases':[result]}),flush=True)
                    continue
                observed=model.inspect(rgb,prompt=BUILDING_PROMPT,optimize=True,
                                       excluded=building_ui_exclusion(rgb.shape[:2],full_window=job.get('full_window') is True,
                                           hidden_ui_flags=job.get('visual_ui_hidden')),
                                       review_surfaces=True,review_portals=True)
                result=describe_building_scene(observed,model.semantic.config.id2label,
                    camera_view=job.get('camera_view'),native_outdoors=job.get('native_outdoors'),
                    full_window=job.get('full_window') is True)
                result.update(frame_id=job['frame_id'], image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              ground_measurement=building_ground_measurement(rgb,observed,
                                  full_window=job.get('full_window') is True,hidden_ui_flags=job.get('visual_ui_hidden')),
                              visual_ui_hidden=job.get('visual_ui_hidden'),
                              model_timings=dict(observed.timings,engine_reused=reused,load_s=model.load_s),
                              model_diagnostics=getattr(observed,'diagnostics',{}))
                np.savez_compressed(path.parent/'masks.npz',labels=observed.semantic_labels,
                    confidence=observed.semantic_confidence,ground=observed.ground,obstacles=observed.obstacles,
                    water=observed.water,**review_artifacts(observed))
                (path.parent/'scene.json').write_text(json.dumps(result,indent=2))
                print(json.dumps({'id':request['id'],'cases':[result]}),flush=True)
                continue
            values=job['frame']
            for name in ('size','world_size','player_position','camera_signature'):
                values[name]=tuple(values[name])
            frame=ProjectionFrame(**values)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=frame.image_sha256:
                raise ProjectionError('segmentation_frame_hash_mismatch')
            automatic=job.get('automatic') is True
            if not automatic:
                # Historical/offline compatibility only. The task controller
                # always requests automatic mode and never supplies a bundle.
                bundle=read_bundle(job['bundle'])
                data=json.loads(Path(job['bundle']).read_text())
                key=f"{job['quest_id']}:{job['phase']}:{job.get('objective_index') or 0}"
                target=data.get('targets',{}).get(key)
                if not target or target.get('source')!='reviewed_static_approach_region':
                    raise ProjectionError('segmentation_target_region_required')
                if target.get('map_id')!=frame.map_id or target.get('floor_id')!=frame.floor_id:
                    raise ProjectionError('segmentation_target_scene_mismatch')
                if target.get('bundle_image_sha256')!=bundle[0].frame.image_sha256:
                    raise ProjectionError('segmentation_target_reference_mismatch')
            settings=(job['models'],job['checkpoint'],job['device'])
            reused=settings in engines
            if settings not in engines:
                engines.clear()
                engines[settings]=LocalSegmenter(json.loads(Path(job['models']).read_text()),
                    job['checkpoint'],variant='large',sam_device=job['device'])
            model=engines[settings]
            building=job.get('goal_mode') in {'building_entry','building_cross','building_search'}
            camera_views=(2,4,5) if building else (4,)
            if calibrator.camera_views!=camera_views:
                calibrator=AutomaticGround(camera_views=camera_views)
            if automatic:
                if session!=job['session_id'] and (building or calibrator.reference is None):calibrator.reset()
                session=job['session_id']
            cache_status='disabled'
            cache_path=job.get('calibration_cache') if automatic and not building else None
            if cache_path and session not in cache_attempted:
                cache_attempted.add(session)
                if calibrator.reference is None:
                    try:
                        calibrator=load_cache(cache_path,frame,now=time.monotonic(),wall_time=time.time())
                        cache_status='restored_pending_current_image'
                    except (OSError,ValueError,KeyError,TypeError,IndexError,AttributeError,BadZipFile,cv2.error) as exc:
                        cache_status='not_reused:'+str(exc)
            # The initial frame and first rotation only measure texture. Before
            # the second rotation can lead to ANY movement, run full detection/SAM.
            geometry_only=(automatic and calibrator.reference is None and not calibrator.anchors
                           and hasattr(model,'inspect_geometry'))
            if building:
                geometry_only=geometry_only and building_rotation_only(calibrator,frame,job)
            if building and not geometry_only:
                observed=model.inspect(rgb,prompt=BUILDING_PROMPT,optimize=True,
                                       excluded=building_ui_exclusion(rgb.shape[:2],full_window=job.get('full_window') is True,
                                           hidden_ui_flags=job.get('visual_ui_hidden')),
                                       review_surfaces=True,review_portals=True)
                job['building_scene']=describe_building_scene(observed,model.semantic.config.id2label,
                    camera_view=frame.camera_signature[0],native_outdoors=job.get('native_outdoors'),
                    full_window=job.get('full_window') is True)
                (path.parent/'building-scene.json').write_text(json.dumps(job['building_scene'],indent=2))
                if job['building_scene']['surface']=='structure':
                    raise ProjectionError('building_not_ground_surface')
                if job.get('stair_trial') is None:
                    observed=flat_navigation_masks(observed,model.semantic.config.id2label)
            else:
                observed=model.inspect_geometry(rgb) if geometry_only else model.inspect(rgb,optimize=True)
                if building:
                    observed.ground &= ~building_ui_exclusion(rgb.shape[:2],full_window=job.get('full_window') is True,
                                                              hidden_ui_flags=job.get('visual_ui_hidden'))
            # Preserve the actual segmentation even when subsequent geometry fails.
            np.savez_compressed(path.parent/'masks.npz',ground=observed.ground,obstacles=observed.obstacles,
                occluded=observed.occluded,water=observed.water,forbidden=observed.forbidden,
                semantic_confidence=observed.semantic_confidence,
                **review_artifacts(observed))
            if automatic:
                try:
                    result=(stationary_building_job(calibrator,frame,rgb,observed,job) if building and geometry_only else
                            stair_job(calibrator,frame,rgb,observed,job,model.semantic.config.id2label)
                            if building and job.get('stair_trial') is not None else
                            (building_job if building else automatic_job)(calibrator,frame,rgb,observed,job))
                finally:
                    reference=getattr(calibrator,'reference',None)
                    if (cache_path and reference is not None and not calibrator.cache_id
                            and reference.frame.frame_id not in saved_references):
                        try:
                            save_cache(cache_path,calibrator,created_at=time.time())
                            saved_references.add(reference.frame.frame_id)
                            cache_status='saved_verified_measurements'
                        except (OSError,ValueError) as exc:
                            cache_status='save_failed:'+str(exc)
                if calibrator.cache_id:cache_status='reused_with_current_image_validation'
                elif cache_status=='restored_pending_current_image':cache_status='current_image_rejected_cache'
                result['calibration_cache_status']=cache_status
            else:
                projection,evidence=rebase_projection(bundle,frame,rgb,
                    observed.ground & (observed.semantic_confidence>=.9),now=time.monotonic())
                region=interaction_region(projection,target['world_position'],target['radius_yards'],
                                          position_error_yards=target['position_error_yards'])
                result=plan_segmented_approach(projection,observed,region,projection.frame,
                    now=time.monotonic(),frame_id=frame.frame_id,image_sha256=frame.image_sha256,
                    convention=job['convention'],max_step_yards=.75)
                result['approach']['goal_kind']='reviewed_approach_region_not_interaction_receipt'
                result['registration']=evidence
                result['calibration_source']=bundle[0].source
                result['target_key']=key
            result['model_timings']=dict(observed.timings,load_s=model.load_s,engine_reused=reused,
                                        geometry_only=geometry_only)
            result['model_diagnostics']=getattr(observed,'diagnostics',{})
            result['frame_id']=frame.frame_id
            (path.parent/'plan.json').write_text(json.dumps(result,indent=2))
            print(json.dumps({'id':request['id'],'cases':[result]}),flush=True)
        except Exception as exc:
            # The controller still receives a failure; the on-disk record explains
            # which observation failed instead of disappearing from the run bundle.
            if path is not None and path.parent.is_dir():
                try:
                    (path.parent/'failure.json').write_text(json.dumps({
                        'request_id':request.get('id'),'error':str(exc),
                        'details':getattr(exc,'evidence',None)},indent=2))
                except OSError:
                    pass
            print(json.dumps({'id':request.get('id'),'error':str(exc)}),flush=True)


if __name__=='__main__':main()
