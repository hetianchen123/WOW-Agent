"""Controller behavior with native feedback and measured-model-delay simulation."""
from dataclasses import replace
import math

import pytest

from bot.control.route_window import RouteWindow
from bot.perception.travel_channel import TravelFlags,TaskFlags
from test_segmented_runtime import controller,ready


def contract(probing=True):
    return dict(schema=1,requires_probe=probing,cross_track_yards=.15,max_age_s=12,
                max_pulses=4 if probing else 8)


def window_plan(result,origin,*,probing=True):
    length=.75 if probing else 2.
    result.update(approach={'requires_probe':probing},execution_window=contract(probing),
        next_segment_world=[list(origin),[origin[0]+length/1000,origin[1]]],
        movement_budget_yards=length)


def prepared(tmp_path,monkeypatch,probing=True):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);nav,heading=ready(obj,ctx,nav)
    window_plan(requests[0].result,nav.position,probing=probing)
    assert obj.step(ctx,1.2,nav,heading).tap is None
    return obj,ctx,nav,heading,requests


def feedback(obj,ctx,nav,heading,position,start=1.3):
    fresh=replace(nav,sequence=nav.sequence+1,position=position)
    assert obj.step(ctx,start+.3,fresh,heading).tap is None
    return replace(fresh,sequence=fresh.sequence+1),obj.step(
        ctx,start+.4,replace(fresh,sequence=fresh.sequence+1),heading)


def test_two_measured_pulses_finish_one_occluded_prefix_without_second_model_call(tmp_path,monkeypatch):
    obj,ctx,nav,heading,requests=prepared(tmp_path,monkeypatch)
    assert obj.step(ctx,1.3,nav,heading).tap=='w'
    origin=nav.position;nav,receipt=feedback(obj,ctx,nav,heading,(origin[0]+.0005,origin[1]))
    assert receipt.outcome is None and obj.plan is not None and obj.calls==1
    command=obj.step(ctx,1.75,nav,heading)
    assert command.tap=='w' and 0<command.hold_s<.04
    nav,_=feedback(obj,ctx,nav,heading,(origin[0]+.0007,origin[1]),start=1.75)
    assert obj.plan is None and obj.total==pytest.approx(.7) and len(requests)==1
    assert obj.step(ctx,2.2,nav,heading).tap is None and obj.pending is None
    assert obj.step(ctx,2.4,nav,heading).tap is None and obj.pending is not None


def test_one_new_sequence_does_not_confirm_a_window_movement_twice(tmp_path,monkeypatch):
    obj,ctx,nav,heading,_=prepared(tmp_path,monkeypatch)
    obj.step(ctx,1.3,nav,heading)
    fresh=replace(nav,sequence=3,position=(nav.position[0]+.0005,nav.position[1]))
    for now in (1.6,1.7,1.8):assert obj.step(ctx,now,fresh,heading).tap is None
    assert obj.pulse is not None and obj.total==0


@pytest.mark.parametrize('dx,dy,expected',[(0,0,None),(.0005,.0002,'segmentation_route_deviation'),
                                         (.001,0,'segmentation_route_deviation')])
def test_collision_deviation_and_overshoot_never_continue_old_prefix(tmp_path,monkeypatch,dx,dy,expected):
    obj,ctx,nav,heading,_=prepared(tmp_path,monkeypatch);obj.step(ctx,1.3,nav,heading)
    nav,receipt=feedback(obj,ctx,nav,heading,(nav.position[0]+dx,nav.position[1]+dy))
    assert obj.plan is None and obj.window is None and receipt.outcome==expected
    if dx==0:assert obj.blocked


def test_prefix_turn_reaches_corner_before_advancing_next_leg(tmp_path,monkeypatch):
    obj,ctx,nav,heading,requests=prepared(tmp_path,monkeypatch,probing=False)
    x,y=nav.position
    requests[0].result['next_segment_world']=[[x,y],[x+.0005,y],[x+.0005,y-.0005]]
    # Rebuild the window from the updated test geometry, before any action.
    obj.window=RouteWindow(requests[0].result,(1000,1000),1)
    assert obj.step(ctx,1.3,nav,heading).tap=='w'
    nav,_=feedback(obj,ctx,nav,heading,(x+.0005,y))
    command=obj.step(ctx,1.75,nav,heading)
    assert command.tap in {'left','right'} and command.tap!='w'
    assert obj.window.index==2


def test_no_new_pulse_when_feedback_cannot_finish_before_deadline(tmp_path,monkeypatch):
    obj,ctx,nav,heading,_=prepared(tmp_path,monkeypatch);obj.deadline=2
    command=obj.step(ctx,1.3,nav,heading)
    assert command.outcome=='segmentation_feedback_budget_reserved'
    assert not command.tap and not command.keys and obj.pulses==0


def test_global_distance_budget_cannot_issue_or_count_a_phantom_pulse(tmp_path,monkeypatch):
    obj,ctx,nav,heading,_=prepared(tmp_path,monkeypatch);obj.total=7.99
    command=obj.step(ctx,1.3,nav,heading)
    assert command.outcome=='segmentation_budget_exhausted' and not command.tap
    assert obj.pulses==0 and obj.pulse is None


def test_camera_change_during_receipt_cannot_be_hidden_by_updating_window_pose(tmp_path,monkeypatch):
    obj,ctx,nav,heading,_=prepared(tmp_path,monkeypatch);obj.step(ctx,1.3,nav,heading)
    nav=replace(nav,sequence=3,camera_epoch=nav.camera_epoch+1)
    assert obj.step(ctx,1.6,nav,heading).outcome=='probe_pose_changed'


def test_deadline_reports_unconfirmed_last_action_without_extending_or_moving(tmp_path,monkeypatch):
    obj,ctx,nav,heading,_=prepared(tmp_path,monkeypatch);obj.step(ctx,1.3,nav,heading)
    events=[];ctx.log_event=lambda name,data:events.append((name,data))
    command=obj.deadline_result(ctx,61,replace(nav,position=(nav.position[0]+.0005,nav.position[1])))
    assert command.outcome=='segmentation_budget_exhausted' and not command.tap and not command.keys
    assert events[-1][1]['feedback_complete'] is False
    assert events[-1][1]['unconfirmed_displacement_yards']==pytest.approx(.5)


@pytest.mark.parametrize('fault',['probe_length','stale_contract','wrong_probe_flag'])
def test_extended_prefix_requires_matching_bounded_contract(tmp_path,monkeypatch,fault):
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch);nav,heading=ready(obj,ctx,nav)
    r=requests[0].result;window_plan(r,nav.position)
    if fault=='probe_length':r['movement_budget_yards']=2
    if fault=='stale_contract':r['execution_window']['max_age_s']=100
    if fault=='wrong_probe_flag':r['execution_window']['requires_probe']=False
    command=obj.step(ctx,1.2,nav,heading)
    assert command.outcome=='segmentation_window_invalid' and not command.tap


@pytest.mark.parametrize('change',['combat','camera','position','expired'])
def test_reusable_window_keeps_interruption_and_expiry_checks(tmp_path,monkeypatch,change):
    obj,ctx,nav,heading,_=prepared(tmp_path,monkeypatch);now=1.3
    if change=='combat':nav=replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT)
    if change=='camera':nav=replace(nav,camera_epoch=nav.camera_epoch+1)
    if change=='position':nav=replace(nav,position=(nav.position[0]+.001,nav.position[1]))
    if change=='expired':now=14;nav=replace(nav,sequence=3)
    command=obj.step(ctx,now,nav,heading)
    assert command.tap is None and command.keys==frozenset()


def test_full_perception_delay_does_not_repeat_between_confirmed_known_steps(tmp_path,monkeypatch):
    """Synthetic motion with the ~5.5 s measured CPU inference delay, no game I/O."""
    obj,ctx,nav,requests=controller(tmp_path,monkeypatch)
    clock={'now':1.};heading=1.5*math.pi;origin=nav.position;events=[]
    ctx.log_event=lambda name,data:events.append((clock['now'],name,data))
    original_factory=obj.request_factory
    def delayed(image,frame,job):
        request=original_factory(image,frame,job);available=clock['now']+5.5
        window_plan(request.result,frame.player_position,probing=False)
        request.poll=lambda:request.result if clock['now']>=available else None
        return request
    obj.request_factory=delayed
    for tick in range(1200):
        clock['now']=1+tick*.05
        if tick%4==0:nav=replace(nav,sequence=(nav.sequence+1)%128)
        if obj.total>=3.8:
            nav=replace(nav,task=nav.task|TaskFlags.NPC_WITHIN_5_YARDS|TaskFlags.NPC_RANGE_KNOWN)
        command=obj.step(ctx,clock['now'],nav,heading)
        if command.outcome=='detoured':break
        assert command.outcome is None,command
        if command.tap=='w':nav=replace(nav,position=(nav.position[0]+command.hold_s*7.1/1000,nav.position[1]))
    else:pytest.fail('delayed perception exceeded the recovery deadline')
    assert len(requests)==2 and clock['now']<20
    assert obj.total>=3.8 and nav.position[0]>origin[0]
    assert any(name=='quest_segmented_prefix_continued' for _,name,_ in events)
