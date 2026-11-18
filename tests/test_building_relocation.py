"""Native-path simulations; no claim of real displacement or improved vision."""
from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

from bot.control.building_relocation import TraceRelocation
from bot.control.ground_trace import GroundTrace
from bot.control.quest_travel import TravelCommand
from bot.perception.travel_channel import TravelFlags as F, LifeState, RecoverySample
from test_segmented_runtime import controller
from test_building_entry_recovery import retry_result,facade_nav


def walked(nav, now=1., offsets=None):
    trace=GroundTrace()
    offsets=offsets or [(0,.002-i*.0005) for i in range(5)]
    for i,(dx,dy) in enumerate(offsets):
        point=replace(nav,position=(nav.position[0]+dx,nav.position[1]+dy),sequence=20+i)
        trace.note_input({'w'})
        trace.observe(point,now-(len(offsets)-1-i)*.1)
    return trace,point


def sample(tmp_path,monkeypatch,**kwargs):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    trace,nav=walked(nav)
    mover=TraceRelocation(1.1,nav,trace,obj.router,obj.convention,deadline=13,
                         blocked_positions=obj.blocked,**kwargs)
    return mover,obj,ctx,nav,trace,requests


def simulate(command,nav,heading,speed=2.5):
    if command.tap in {'left','right'}:
        heading=(heading+(1 if command.tap=='left' else -1)*math.radians(240)*command.hold_s)%math.tau
    if command.tap=='s':
        # Independent kinematic fixture: input duration + native heading,
        # not a teleport to the controller's requested waypoint.
        yards=command.hold_s*speed
        nav=replace(nav,position=(nav.position[0]+math.sin(heading)*yards/nav.world_size[0],
                                  nav.position[1]+math.cos(heading)*yards/nav.world_size[1]))
    return nav,heading


def test_recent_polyline_moves_back_with_released_pulses_then_requires_new_image(tmp_path,monkeypatch):
    mover,obj,ctx,nav,trace,requests=sample(tmp_path,monkeypatch)
    start=nav.position;now=1.2;heading=0;commands=[]
    for _ in range(160):
        nav=replace(nav,sequence=nav.sequence+1)
        cmd=mover.step(ctx,now,nav,heading);commands.append(cmd)
        assert not cmd.keys
        if cmd.outcome:break
        nav,heading=simulate(cmd,nav,heading);now+=.1
    assert cmd.outcome=='building_relocated' and 1.8<mover.total<=2.1
    assert nav.position[1]>start[1] and len([c for c in commands if c.tap=='s'])>=3
    assert all(c.precise and 0<c.hold_s<=.12 for c in commands if c.tap=='s')
    assert not requests  # Reversing a native trace needs no projection/model.


def test_trace_reversal_preserves_corner_instead_of_cutting_across_room(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    trace,nav=walked(nav,offsets=[(-.0015,.0005),(-.001,.0005),(-.0005,.0005),(0,.0005),(0,0)])
    mover=TraceRelocation(1.1,nav,trace,obj.router,obj.convention,deadline=13,blocked_positions=[])
    origin=nav.position;now=1.2;heading=0;positions=[]
    for _ in range(160):
        nav=replace(nav,sequence=nav.sequence+1)
        cmd=mover.step(ctx,now,nav,heading)
        if cmd.outcome:break
        nav,heading=simulate(cmd,nav,heading);positions.append(nav.position);now+=.1
    assert cmd.outcome=='building_relocated'
    # Before the corridor corner, no westward shortcut; after it, stay near its Y.
    assert all(abs(p[0]-origin[0])<.00008 for p in positions if p[1]<origin[1]+.00042)
    assert all(abs(p[1]-origin[1]-.0005)<.00012 for p in positions if p[0]<origin[0]-.00015)


@pytest.mark.parametrize('fault',['stale','unknown_height','other_floor','gap','new_collision','uncommanded','calibration_wiggles'])
def test_invalid_history_does_not_authorize_relocation(tmp_path,monkeypatch,fault):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);trace,nav=walked(nav)
    now=1.1
    if fault=='stale':now=31.1
    if fault=='unknown_height':trace.points=[replace(p,height=None) for p in trace.points]
    if fault=='other_floor':trace.points=[replace(p,height=p.height+2) for p in trace.points]
    if fault=='gap':trace.points=trace.points[:1]
    if fault=='new_collision':obj.blocked.append((nav.position[0],nav.position[1]+.0003))
    if fault=='uncommanded':
        nav=replace(nav,position=(nav.position[0],nav.position[1]-.0005),sequence=99)
        trace.observe(nav,now)
    if fault=='calibration_wiggles':
        trace,nav=walked(nav,offsets=[(0,0),(0,.0002),(0,0),(0,.0002),(0,0)])
    with pytest.raises(ValueError,match='building_recent_ground_trace_missing'):
        TraceRelocation(now,nav,trace,obj.router,obj.convention,deadline=now+12,
                        blocked_positions=obj.blocked)
    assert not ctx.input.count('key_down')


def test_each_movement_requires_two_distinct_new_receipts_and_stops_on_contact(tmp_path,monkeypatch):
    mover,obj,ctx,nav,_,_=sample(tmp_path,monkeypatch)
    assert mover.step(ctx,1.2,nav,0).tap=='s'
    first=replace(nav,sequence=nav.sequence+1)
    assert mover.step(ctx,1.5,first,0)==TravelCommand()
    assert mover.step(ctx,1.6,first,0)==TravelCommand()
    cmd=mover.step(ctx,1.7,replace(first,sequence=first.sequence+1),0)
    assert cmd.outcome=='building_relocation_blocked' and not cmd.tap and not cmd.keys
    assert mover.pulses==1 and obj.blocked


@pytest.mark.parametrize('change',[
    dict(map_id=999),dict(objective_index=9),dict(objective_target=777),dict(height=12),dict(height=None),
    dict(flags=F.KEYS_VERIFIED|F.IN_COMBAT),dict(flags=F.KEYS_VERIFIED|F.FALLING),
    dict(flags=F.KEYS_VERIFIED|F.SWIMMING),dict(recovery=RecoverySample(LifeState.DEAD)),
    dict(camera=SimpleNamespace(paused=True)),
])
def test_relocation_interruptions_never_send_the_next_pulse(tmp_path,monkeypatch,change):
    mover,_,ctx,nav,_,_=sample(tmp_path,monkeypatch)
    assert mover.step(ctx,1.2,nav,0).tap=='s'
    cmd=mover.step(ctx,1.3,replace(nav,sequence=nav.sequence+1,**change),0)
    assert cmd.outcome=='probe_interrupted' and not cmd.keys and not cmd.tap


def test_new_collision_invalidates_previously_walked_route(tmp_path,monkeypatch):
    mover,obj,ctx,nav,_,_=sample(tmp_path,monkeypatch)
    obj.blocked.append((nav.position[0],nav.position[1]+.0003))
    assert mover.step(ctx,1.2,nav,0).outcome=='building_relocation_blocked'
    assert mover.pulses==0


def test_geometry_failure_relocates_before_camera_retry_and_restarts_at_new_position(tmp_path,monkeypatch):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    nav=facade_nav(nav,view=4)
    trace,nav=walked(nav);obj.ground_trace=trace;obj.goal_mode='building_entry'
    obj.step(ctx,1,nav,0);obj.step(ctx,1.1,replace(nav,sequence=nav.sequence+1),0)
    requests[0].result=retry_result(requests[0]);old_session=obj.session_id
    failed_position=nav.position
    obj.step(ctx,1.2,replace(nav,sequence=nav.sequence+2),0)
    assert obj.relocation and obj.building_recovery is None and requests[0].closed
    assert obj.session_id!=old_session and not obj.plan
    assert obj.building_scene is obj.building_measurement is None
    assert failed_position in obj.visited
    deadline,progress_at=obj.deadline,obj.progress_at
    now=1.3;heading=0
    for _ in range(180):
        nav=replace(nav,sequence=nav.sequence+1)
        command=obj.step(ctx,now,nav,heading)
        assert command.outcome is None
        nav,heading=simulate(command,nav,heading)
        if len(requests)==2:break
        now+=.1
    assert len(requests)==2 and obj.relocation is None and obj.relocation_used
    assert obj.deadline==deadline and progress_at<obj.progress_at<=now
    assert obj.total>1.8 and obj.pulses>0
    assert requests[-1].frame.player_position==nav.position
    assert requests[-1].frame.frame_id!=requests[0].frame.frame_id
    assert requests[-1].job['session_id']!=old_session and obj.plan is None
    # A late old-view result cannot authorize a route after the retreat.
    requests[-1].result=requests[0].result
    assert obj.step(ctx,now+.1,replace(nav,sequence=nav.sequence+1),heading).outcome==\
        'segmentation_result_frame_mismatch'


def test_no_trace_keeps_camera_recovery_and_never_invents_backward_motion(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch);obj.goal_mode='building_entry'
    obj.ground_trace=GroundTrace()
    assert obj.recover_building_view(ctx,1.1,nav,'auto_ground_texture_insufficient')==TravelCommand()
    assert obj.relocation is None and obj.building_recovery and not obj.relocation_used


def test_contact_during_relocation_returns_to_observation_without_repeating_retreat(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    trace,nav=walked(nav);obj.ground_trace=trace;obj.goal_mode='building_entry'
    nav=facade_nav(nav,view=4)
    obj.recover_building_view(ctx,1.1,nav,'auto_ground_texture_insufficient')
    assert obj.step(ctx,1.2,nav,0).tap=='s'
    assert obj.step(ctx,1.4,replace(nav,sequence=nav.sequence+1),0)==TravelCommand()
    assert obj.step(ctx,1.6,replace(nav,sequence=nav.sequence+2),0)==TravelCommand()
    assert obj.relocation is None and obj.building_recovery and obj.blocked
    assert obj.pulses==1 and obj.relocation_used
    obj.recover_building_view(ctx,1.7,replace(nav,sequence=nav.sequence+3),'tracking_validation_failed')
    assert obj.relocation is None and obj.building_recoveries==2 and obj.pulses==1


def test_relocation_reserves_observation_and_keeps_total_budget(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    trace,nav=walked(nav);obj.ground_trace=trace;obj.goal_mode='building_entry'
    obj.calls=obj.call_limit-1
    obj.recover_building_view(ctx,1.1,nav,'auto_ground_texture_insufficient')
    assert obj.relocation is None and obj.building_recovery and obj.deadline==61


def test_task_stop_releases_pending_relocation_and_cancels_its_owner(tmp_path,monkeypatch):
    from test_npc_obstacle import npc
    from types import SimpleNamespace
    run,ctx,_,_,_=npc(tmp_path,monkeypatch)
    mover,obj,_,nav,_,_=sample(tmp_path,monkeypatch)
    obj.relocation=mover
    run.building_access=SimpleNamespace(close=obj.close)
    command=mover.step(ctx,1.2,nav,0)
    assert command.tap=='s' and mover.pulse
    run.apply_travel_command(ctx,command)
    run.stop(ctx,'game_not_frontmost')
    assert not ctx.input._held_keys and obj.closed and obj.relocation is None
    assert obj.step(ctx,1.3,replace(nav,sequence=nav.sequence+1),0).outcome=='segmentation_closed'


def test_saved_low_texture_failure_can_choose_native_history_without_claiming_image_geometry(tmp_path,monkeypatch):
    from test_building_entry_recovery import recorded
    from bot.perception.automatic_ground import AutomaticGround
    from bot.perception.building_scene import describe_building_scene
    from experiments.route_models.segmentation_worker import building_job
    meta,data,observed,frame=recorded()
    job=dict(meta['request'],building_scene=describe_building_scene(observed,meta['names'],camera_view=2))
    result=building_job(AutomaticGround(camera_views=(2,4,5)),frame,data['rgb'],observed,job)
    assert result['kind']=='building_reobserve' and result['reason']=='auto_ground_texture_insufficient'
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    trace,nav=walked(nav);obj.ground_trace=trace;obj.goal_mode='building_entry'
    obj.recover_building_view(ctx,1.1,nav,result['reason'])
    assert obj.relocation and obj.plan is None and obj.anchor is None
    assert obj.step(ctx,1.2,nav,0).tap=='s'
    # The failed picture supplied the reason, not any pixel/world conversion.
    assert result['executable'] is False and 'next_segment_world' not in result


def test_task_entry_receives_live_ground_trace(tmp_path,monkeypatch):
    from test_building_access import setup
    from bot.control import segmented_detour as sd
    obj,ctx,nav,events,requests,run=setup(tmp_path,monkeypatch)
    ctx.cfg.segmented_navigation.enabled=True;ctx.cfg.segmented_navigation.building_access=True
    run.require_prepared=True
    captured=[]
    monkeypatch.setattr(sd,'SegmentedDetour',lambda *args,**kwargs:captured.append(kwargs))
    run.process_building_access(ctx,1,nav,0)
    run.building_access.entry_factory(1.1,nav,ctx)
    assert captured[0]['ground_trace'] is run.ground_trace
    assert captured[0]['goal_mode']=='building_entry'
