"""Local stair trials, rendered transitions and task handoff; no live gameplay."""
from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot.control.stair_trial import StairTrial
from bot.decision.stair_probe import observe_stair_trial
from bot.perception.automatic_ground import AutomaticGround
from bot.perception.ground_projection import ProjectionError
from bot.perception.travel_channel import TravelFlags
from experiments.route_models.simulate_staircase import StairScene, frames, trial
from test_segmented_runtime import controller, ready
from test_building_connectors import connector_reply


@pytest.mark.parametrize('public_height', [True, False])
def test_ray_rendered_stairs_reach_new_surface_without_planar_coordinates(public_height):
    result = trial(StairScene(), public_height=public_height)
    assert result['ready'], result
    assert result['final_height'] == 1.5  # Renderer ground truth, not inferred level.
    assert 3 <= result['moved_yards'] <= 5
    assert result['steps'][-1]['kind'] == result['steps'][-2]['kind'] == 'stair_surface_candidate'
    assert all(row['kind'] in {'stair_probe','stair_surface_candidate'} for row in result['steps'])


@pytest.fixture(scope='module')
def scene():
    cal = AutomaticGround()
    rows = list(frames(StairScene(), mask_mode='flat_only'))[:3]
    for frame, rgb, observed, _, _ in rows:
        cal.update(frame, rgb, observed, now=frame.captured_at+.1)
    assert cal.anchor is not None and cal.matrix is None
    return rows, cal


def clone(observed):
    return SimpleNamespace(**{k:v.copy() if isinstance(v,np.ndarray) else v for k,v in vars(observed).items()})


@pytest.mark.parametrize('obstacle', ['wall', 'water', 'ui', 'unknown'])
def test_current_obstacle_or_unknown_can_never_be_filled_by_a_stair_box(scene, obstacle):
    rows, cal = scene
    observed = clone(rows[-1][2])
    x, y = (round(v) for v in cal.anchor)
    # Close all nearby exits, including the current intended stair corridor.
    blocked = np.zeros(observed.ground.shape, np.uint8)
    cv2.circle(blocked, (x, y), 24, 1, 5)
    mask = blocked.astype(bool)
    observed.ground[mask] = False
    observed.semantic_labels[mask] = 0
    if obstacle == 'wall': observed.obstacles[mask] = True
    if obstacle == 'water': observed.water[mask] = True
    if obstacle == 'ui': observed.forbidden[mask] = True
    with pytest.raises(ProjectionError, match='stair_trial_'):
        observe_stair_trial(observed, {1:'floor',2:'stairs',3:'wall'}, cal.anchor.tolist())


def test_occluded_character_anchor_does_not_require_visible_shoes(scene):
    rows, cal = scene
    observed = clone(rows[-1][2])
    x, y = (round(v) for v in cal.anchor)
    observed.ground[y-6:y+7,x-6:x+7] = False
    observed.semantic_labels[y-6:y+7,x-6:x+7] = 0
    observed.occluded[y-6:y+7,x-6:x+7] = True
    result = observe_stair_trial(observed, {1:'floor',2:'stairs',3:'wall'}, cal.anchor.tolist())
    assert result['kind'] == 'stair_probe' and result['metric_route'] is False
    assert result['key'] in {'w','s','q','e'}


def proposal(**overrides):
    result=dict(dict(frame_id='fresh',kind='stair_probe',key='w',hold_s=.04,advance='w',
        anchor=[50.,60.],metric_route=False,requires_new_measurement=True,
        level_id=None,basis='local_stair_corridor_trial'), **overrides)
    result['anchor_validation']=dict(schema=1,current_frame=result['frame_id'],anchor_error_px=3.,
        source='current_independent_motion',fit_movements=2,validation_movements=2)
    return result


@pytest.mark.parametrize('fault', ['uncoupled','view','stale_epoch','paused','strafe','metric'])
def test_trial_rejects_missing_alignment_or_changed_camera_before_emitting_input(tmp_path, monkeypatch, fault):
    _, _, nav, _ = controller(tmp_path, monkeypatch)
    control = StairTrial(1, nav, 'ccw_west', deadline=61)
    result = proposal(); coupled = fault != 'uncoupled'
    if fault == 'view': nav = replace(nav,camera=SimpleNamespace(view=2))
    if fault == 'paused': nav = replace(nav,camera=SimpleNamespace(view=4,paused=True))
    if fault == 'strafe': nav = replace(nav,strafe_verified=False)
    if fault == 'metric': result['metric_route'] = True
    if fault == 'stale_epoch': control.camera = (nav.camera,nav.camera_epoch+1)
    cmd = control.accept(result,1.1,nav,0,coupled=coupled)
    assert cmd.outcome and not cmd.tap and control.pulse is None


@pytest.mark.parametrize('fault', ['collision','drop','sideways','camera','paused'])
def test_stair_feedback_does_not_turn_bad_movement_into_another_step(tmp_path, monkeypatch, fault):
    _, _, nav, _ = controller(tmp_path, monkeypatch)
    control = StairTrial(1, nav, 'ccw_west', deadline=61)
    assert control.accept(proposal(),1.1,nav,0,coupled=True).tap == 'w'
    position = (nav.position[0],nav.position[1]-.00028)
    if fault == 'collision': position = nav.position
    if fault == 'sideways': position = (nav.position[0]+.00028,nav.position[1])
    fresh = replace(nav,sequence=nav.sequence+1,position=position)
    if fault == 'drop': fresh = replace(fresh,height=nav.height-2)
    if fault == 'camera': fresh = replace(fresh,camera_epoch=nav.camera_epoch+1)
    if fault == 'paused': fresh = replace(fresh,camera=SimpleNamespace(view=4,paused=True))
    cmd = control.feedback(1.5,fresh,0)
    if not cmd.outcome:
        cmd = control.feedback(1.6,replace(fresh,sequence=fresh.sequence+1),0)
    assert cmd.outcome not in {None,'stair_step_observed'} and not cmd.tap and control.total == 0


def test_repeated_sequence_cannot_complete_stair_feedback(tmp_path, monkeypatch):
    _, _, nav, _ = controller(tmp_path, monkeypatch)
    control = StairTrial(1,nav,'ccw_west',deadline=61)
    control.accept(proposal(),1.1,nav,0,coupled=True)
    fresh = replace(nav,sequence=nav.sequence+1,position=(nav.position[0],nav.position[1]-.00028))
    for now in (1.5,1.6,1.7):
        assert control.feedback(now,fresh,0).outcome is None
    assert control.pulse is not None and control.total == 0
    cmd = control.feedback(1.8,replace(fresh,sequence=fresh.sequence+1),0)
    assert cmd.outcome == 'stair_step_observed' and control.total == pytest.approx(.28)


def active_trial(tmp_path,monkeypatch):
    obj,ctx,nav,requests = controller(tmp_path,monkeypatch)
    obj.goal_mode = 'building_search'
    nav,heading = ready(obj,ctx,nav)
    connector_reply(requests[0])
    obj.stair = StairTrial(1,nav,'ccw_west',deadline=obj.deadline)
    obj.calibration_coupled = True  # The orbit receipt itself has separate tests.
    requests[0].result.update(proposal(frame_id=requests[0].frame.frame_id))
    return obj,ctx,nav,heading,requests


def test_task_trial_waits_for_two_receipts_then_captures_again(tmp_path,monkeypatch):
    obj,ctx,nav,heading,requests = active_trial(tmp_path,monkeypatch)
    command = obj.step(ctx,1.2,nav,heading)
    assert command.tap == 'w' and command.hold_s == .04
    fresh = replace(nav,sequence=3,position=(nav.position[0]+.00028,nav.position[1]))
    assert not obj.step(ctx,1.55,fresh,heading).tap and obj.total == 0
    fresh = replace(fresh,sequence=4)
    assert not obj.step(ctx,1.65,fresh,heading).outcome
    assert obj.stair.total == pytest.approx(.28) and obj.total == pytest.approx(.28)
    assert obj.stair.pulse is None and obj.anchor is None and obj.pulses == 1
    assert not obj.step(ctx,1.9,fresh,heading).tap and obj.pending is not None
    assert not obj.step(ctx,2,replace(fresh,sequence=5),heading).tap
    assert requests[-1].job['stair_trial']['moved'] == pytest.approx(.28)


@pytest.mark.parametrize('fault',['falling','old_frame','uncoupled','old_plane'])
def test_task_never_substitutes_a_flat_route_or_stale_reply_for_a_stair_trial(tmp_path,monkeypatch,fault):
    obj,ctx,nav,heading,requests = active_trial(tmp_path,monkeypatch)
    if fault=='falling':nav=replace(nav,flags=nav.flags|TravelFlags.FALLING)
    if fault=='old_frame':requests[0].result['frame_id']='obsolete'
    if fault=='uncoupled':obj.calibration_coupled=False
    if fault=='old_plane':requests[0].result['kind']='route'
    cmd=obj.step(ctx,1.2,nav,heading)
    assert cmd.outcome and not cmd.tap and obj.pulses==0


def test_two_new_surface_observations_resume_building_measurement_not_task_success(tmp_path,monkeypatch):
    obj,ctx,nav,heading,requests = active_trial(tmp_path,monkeypatch)
    obj.stair.total=.56;obj.total=.56;obj.stair.pulses=2
    requests[0].result.update(proposal(frame_id=requests[0].frame.frame_id,kind='stair_surface_candidate'))
    deadline,session=obj.deadline,obj.session_id
    assert not obj.step(ctx,1.2,nav,heading).outcome and obj.stair is not None
    nav=replace(nav,sequence=3)
    obj.step(ctx,1.7,nav,heading)
    nav=replace(nav,sequence=4)
    obj.step(ctx,1.8,nav,heading)
    connector_reply(requests[-1])
    requests[-1].result.update(proposal(frame_id=requests[-1].frame.frame_id,kind='stair_surface_candidate'))
    cmd=obj.step(ctx,1.9,nav,heading)
    assert not cmd.outcome and not cmd.tap and obj.stair is None
    assert obj.goal_mode=='building_search' and not obj.passages and obj.crossing_confirmed is None
    assert obj.deadline==deadline and obj.total==.56 and obj.session_id!=session
    assert not obj.step(ctx,2.45,replace(nav,sequence=5),heading).tap and obj.pending is not None


def test_worker_measures_only_stationary_anchor_then_proposes_one_nonmetric_trial(scene,monkeypatch):
    from experiments.route_models import segmentation_worker as worker
    rows,previous=scene
    cal=AutomaticGround()
    job=dict(quest_id=12345,phase='READY',stair_trial={'advance':None,'moved':0},
             building_scene=dict(schema=1,surface='unknown',doorways=[],executable=False))
    # This fixture supplies the preceding approach's measured viewport anchor;
    # it supplies no plane or world endpoint for the stair movement.
    job['stair_trial']['anchor_reference']=dict(point=previous.anchor.tolist(),error_px=previous.anchor_error,
        size=list(rows[0][0].size),camera=list(rows[0][0].camera_signature[:2]),source_frame='prior-floor-motion')
    kinds=[]
    for frame,rgb,observed,_,_ in rows:
        monkeypatch.setattr(worker.time,'monotonic',lambda:frame.captured_at+.1)
        result=worker.stair_job(cal,frame,rgb,observed,job,{1:'floor',2:'stairs',3:'wall'})
        kinds.append(result['kind'])
    assert kinds==['calibration_orbit','calibration_orbit','stair_probe']
    assert cal.matrix is None and not cal.training and not cal.validation
    assert not result['metric_route'] and result['requires_fresh_feedback']


def test_without_previous_motion_anchor_worker_requires_adjacent_floor_measurement(scene):
    from experiments.route_models.segmentation_worker import stair_job
    cal=AutomaticGround()
    job=dict(quest_id=12345,phase='READY',stair_trial={'advance':None,'moved':0},strafe_verified=True,
        building_scene=dict(schema=1,surface='unknown',doorways=[],executable=False))
    for frame,rgb,observed,_,_ in scene[0]:
        result=stair_job(cal,frame,rgb,observed,job,{1:'floor',2:'stairs',3:'wall'},now=frame.captured_at+.1)
    assert result['kind']=='calibration_probe' and not result['metric_route']
    assert cal.stair_reference is None and cal.matrix is None


def test_new_orbit_cannot_replace_measured_anchor_with_a_different_floor(scene):
    from experiments.route_models.segmentation_worker import stair_job
    rows,known=scene;cal=AutomaticGround()
    prior=dict(point=(known.anchor+[0,50]).tolist(),error_px=known.anchor_error,
        size=list(rows[0][0].size),camera=[4,32],source_frame='known-floor')
    job=dict(quest_id=12345,phase='READY',stair_trial={'advance':None,'moved':0,'anchor_reference':prior},
        building_scene=dict(schema=1,surface='unknown',doorways=[],executable=False))
    for frame,rgb,observed,_,_ in rows[:2]:
        result=stair_job(cal,frame,rgb,observed,job,{1:'floor',2:'stairs',3:'wall'},now=frame.captured_at+.1)
        assert result['kind']=='calibration_orbit'
    frame,rgb,observed,_,_=rows[2]
    with pytest.raises(ProjectionError,match='stair_trial_anchor_ambiguous'):
        stair_job(cal,frame,rgb,observed,job,{1:'floor',2:'stairs',3:'wall'},now=frame.captured_at+.1)
    assert cal.stair_reference is None


def test_uncorroborated_anchor_reply_cannot_emit_task_input(tmp_path,monkeypatch):
    obj,ctx,nav,heading,requests=active_trial(tmp_path,monkeypatch)
    requests[0].result['anchor_validation']['validation_movements']=0
    cmd=obj.step(ctx,1.2,nav,heading)
    assert cmd.outcome=='stair_trial_anchor_unconfirmed' and not cmd.tap


def test_approach_reference_contains_only_viewport_anchor_not_old_floor_route():
    cal=AutomaticGround()
    for frame,rgb,observed,_,_ in frames(StairScene(),start=(0,1.5),mask_mode='flat_only'):
        projection,proof=cal.update(frame,rgb,observed,now=frame.captured_at+.1)
    assert projection is not None
    plan=dict(projection_matrix=projection.matrix.tolist(),registration=proof)
    anchor=StairTrial.measured_anchor(plan,frame)
    assert anchor['point']==pytest.approx(cal.anchor.tolist())
    assert anchor['source_frame']==frame.frame_id and 'projection_matrix' not in anchor
    assert StairTrial.measured_anchor(plan,replace(frame,camera_signature=(2,32,1))) is None


@pytest.mark.parametrize('fault',['deadline','pulses','distance','observations'])
def test_stair_trial_budget_does_not_expand_for_a_new_image(tmp_path,monkeypatch,fault):
    _,_,nav,_=controller(tmp_path,monkeypatch)
    control=StairTrial(1,nav,'ccw_west',deadline=61)
    now=control.deadline if fault=='deadline' else 1.1
    if fault=='pulses':control.pulses=24
    if fault=='distance':control.total=8
    if fault=='observations':control.observations=28
    cmd=control.accept(proposal(),now,nav,0,coupled=True)
    assert cmd.outcome=='stair_trial_budget' and not cmd.tap


def test_stair_trial_uses_remaining_parent_budget_and_reserves_remeasurement(tmp_path,monkeypatch):
    _,_,nav,_=controller(tmp_path,monkeypatch)
    control=StairTrial(30,nav,'ccw_west',deadline=180)
    assert control.deadline==172
    # A fresh image later in the SAME bounded run cannot renew the deadline.
    cmd=control.accept(proposal(),100,nav,0,coupled=True)
    assert cmd.tap=='w' and control.deadline==172
    cmd=control.feedback(172,nav,0)
    assert cmd.outcome=='stair_trial_budget' and not cmd.tap


def test_ui_change_preserves_pending_stair_feedback_then_discards_old_trial(tmp_path,monkeypatch):
    obj,ctx,nav,heading,_=active_trial(tmp_path,monkeypatch)
    obj.step(ctx,1.2,nav,heading)
    fresh=replace(nav,sequence=3,position=(nav.position[0]+.00028,nav.position[1]),visual_ui_hidden=1)
    assert not obj.step(ctx,1.55,fresh,heading).tap and obj.stair is not None
    fresh=replace(fresh,sequence=4)
    assert not obj.step(ctx,1.65,fresh,heading).outcome
    assert obj.total==pytest.approx(.28)
    assert not obj.step(ctx,1.7,fresh,heading).tap
    assert obj.stair is None and obj.anchor is None and obj.visual_ui_hidden==1


def test_task_stop_releases_stair_input_and_clears_pending_trial(tmp_path,monkeypatch):
    from test_npc_obstacle import npc
    run,_,_,_,_=npc(tmp_path,monkeypatch)
    obj,ctx,nav,heading,_=active_trial(tmp_path,monkeypatch)
    run.building_access=SimpleNamespace(close=obj.close)
    cmd=obj.step(ctx,1.2,nav,heading)
    run.apply_travel_command(ctx,cmd)
    assert ctx.input.count('key_down','w')==ctx.input.count('key_up','w')==1
    run.stop(ctx,'game_not_frontmost')
    assert obj.closed and obj.stair is None and not ctx.input._held_keys
