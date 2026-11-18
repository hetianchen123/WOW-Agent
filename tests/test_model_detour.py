import json
import math
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.model_detour import ModelDetour, world_waypoints
from bot.control.pathfinding import GridRouter
from bot.control.quest_terrain import TerrainDetour
from bot.control.quest_travel import QuestTrip
from bot.core.config import BotConfig, RoutePlannerConfig
from bot.decision.route_planner import parse_plan, PlannerError, RouteRequest, credentials, DeepSeekRouteClient
from bot.perception.travel_channel import TravelFlags, TaskFlags
from test_quest_travel import sample


def plan(frame='f', side=4):
    result = {'frame_id':frame,'status':'route','player_feet':[500,750],'goal_feet':[350,650],
            'obstacles':[],'waypoints':[
                {'right_yards':side,'forward_yards':0,'image_point':[600,800],'basis':'near ground'},
                {'right_yards':side,'forward_yards':10,'image_point':[600,600],'basis':'visible end'},
                {'right_yards':0,'forward_yards':10,'image_point':[350,650],'basis':'NPC ground'}],
            'intent':'around observed end','uncertainty':'metric estimates'}
    if side < 0:
        # A left-world candidate must also lie on the left side of the image
        # start->goal line. The previous fixture incorrectly drew both right.
        p,g=result['player_feet'],result['goal_feet']
        dx,dy=g[0]-p[0],g[1]-p[1]
        for wp in result['waypoints'][:-1]:
            x,y=wp['image_point'];t=((x-p[0])*dx+(y-p[1])*dy)/(dx*dx+dy*dy)
            wp['image_point']=[2*(p[0]+t*dx)-x,2*(p[1]+t*dy)-y]
    return result


def fixture(monkeypatch, factory=None):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only-key')
    nav=sample(position=(.2,.21),destination=(.2,.19),flags=TravelFlags.KEYS_VERIFIED,
               task=TaskFlags.PREPARED|TaskFlags.NPC_SELECTED|TaskFlags.NPC_RANGE_KNOWN)
    router=GridRouter(*nav.world_size,cell_size=1)
    events=[]; calls=[]
    ctx=SimpleNamespace(heading_age=lambda:0,capture=None,log_event=lambda n,d:events.append((n,d)))
    def request(rgb,facts,config):
        calls.append(facts)
        future=Future()
        future.set_result(parse_plan(json.dumps(plan(facts['frame_id'],4 if len(calls)==1 else -4)),facts['frame_id']))
        return SimpleNamespace(future=future,close=lambda:None,evidence_dir='test-evidence')
    ctl=ModelDetour(0,nav,0,router,'ccw_west',RoutePlannerConfig(enabled=True),
                    lambda _:np.zeros((586,1012,3),dtype=np.uint8),deadline=60,
                    request_factory=factory or request)
    return ctl,nav,ctx,calls,events


def drive(monkeypatch, collision=False):
    ctl,nav,ctx,calls,events=fixture(monkeypatch)
    heading=0
    for i in range(601):
        at=i*.1
        task=nav.task
        if ctl.router.distance(nav.position,nav.destination)<4.5 and len(calls)>=(2 if collision else 1):
            task|=TaskFlags.NPC_WITHIN_5_YARDS
        nav=replace(nav,sequence=(i+1)%128,task=task)
        command=ctl.step(ctx,at,nav,heading)
        if command.outcome:
            return command,calls,events,ctl
        if command.tap in {'left','right'}:
            heading=(heading+(1 if command.tap=='left' else -1)*math.radians(240)*command.hold_s)%(2*math.pi)
        if command.tap=='w':
            distance=5*command.hold_s
            end=(nav.position[0]-math.sin(heading)*distance/1000,
                 nav.position[1]-math.cos(heading)*distance/500)
            if not (collision and end[0]>.2008 and len(calls)==1):
                nav=replace(nav,position=end)
    pytest.fail('no bounded result')


def test_one_model_plan_follows_multiple_waypoints_without_per_pulse_requests(monkeypatch):
    command,calls,events,_=drive(monkeypatch)
    assert command.outcome=='detoured'
    assert len(calls)==1
    assert sum(n=='quest_model_movement_issued' for n,d in events)>6
    assert any(n=='quest_model_waypoint_reached' for n,d in events)


def test_collision_sends_actual_failure_then_executes_second_model_route(monkeypatch):
    command,calls,events,_=drive(monkeypatch,True)
    assert command.outcome=='detoured' and len(calls)==2
    failure=calls[1]['failed_routes'][0]
    assert failure['reason']=='route_collision' and failure['last_feedback'][-1]['yards']<.15
    assert failure['blocked_target'] is not None


def test_world_conversion_uses_goal_axes_and_map_aspect_not_pixels():
    p=parse_plan(json.dumps(plan()),'f')
    router=GridRouter(1000,500)
    assert world_waypoints(p,(.2,.21),(.2,.19),router)[0]==pytest.approx((.204,.21))
    east=world_waypoints(p,(.2,.2),(.21,.2),router)
    assert east[0]==pytest.approx((.2,.208))
    assert east[-1]==pytest.approx((.21,.2))


@pytest.mark.parametrize('change', ['frame','nonfinite','oversize','crossing','goal','unknown'])
def test_invalid_or_self_contradictory_route_cannot_execute(change):
    p=plan()
    if change=='frame':p['frame_id']='old'
    if change=='nonfinite':p['waypoints'][0]['right_yards']=float('nan')
    if change=='oversize':p['waypoints'][0]['right_yards']=100
    if change=='crossing':p['obstacles']=[{'kind':'wood','polygon':[[530,730],[570,730],[570,830],[530,830]]}]
    if change=='goal':p['goal_feet']=None
    if change=='unknown':p['status']='observe'
    with pytest.raises(PlannerError):parse_plan(json.dumps(p),'f')


def test_pending_result_cancelled_on_camera_change_without_input(monkeypatch):
    closed=[];future=Future()
    def request(*args):return SimpleNamespace(future=future,close=lambda:closed.append(True),evidence_dir='x')
    ctl,nav,ctx,_,_=fixture(monkeypatch,request)
    ctl.step(ctx,.6,nav,0)
    command=ctl.step(ctx,.7,replace(nav,camera_epoch=nav.camera_epoch+1),0)
    assert command.outcome=='probe_pose_changed' and closed and not command.tap


def test_old_position_receipt_does_not_confirm_new_movement(monkeypatch):
    ctl,nav,ctx,_,_=fixture(monkeypatch)
    ctl.step(ctx,.6,nav,0);ctl.step(ctx,.7,nav,0)
    heading=3*math.pi/2
    assert ctl.step(ctx,.8,nav,heading).tap=='w'
    command=ctl.step(ctx,2.5,nav,heading)
    assert command.outcome=='probe_telemetry_stale' and not command.tap


def test_native_range_required_even_after_all_route_points(monkeypatch):
    ctl,nav,ctx,calls,events=fixture(monkeypatch)
    ctl.step(ctx,.6,nav,0);ctl.step(ctx,.7,nav,0)
    ctl.follower.goals=[]
    ctl.step(ctx,.8,nav,0)
    assert ctl.history[-1]['reason']=='route_exhausted_without_range'
    assert not any(n=='quest_model_range_confirmed' for n,d in events)


def test_model_budget_limits_replans(monkeypatch):
    ctl,nav,ctx,_,_=fixture(monkeypatch)
    ctl.calls=3
    assert ctl.step(ctx,1,nav,0).outcome=='escape_model_request_budget'


def test_retreat_finish_enters_cloud_instead_of_fixed_dogleg():
    nav=sample(position=(.2,.208),destination=(.2,.19),flags=TravelFlags.KEYS_VERIFIED)
    trip=QuestTrip(nav.world_size,nav.destination,'ccw_west')
    detour=TerrainDetour(0,nav,0,trip,None,contact_release=True,npc_bypass=True)
    detour.retreat_origin=(.2,.2)
    cfg=BotConfig();cfg.route_planner.enabled=True
    ctx=SimpleNamespace(cfg=cfg,log_event=lambda *_:None)
    detour.finish_retreat(ctx,3,nav,'distance_reached')
    assert detour.phase=='model_route' and detour.bypass is None
    assert detour.model_detour.deadline==60
    assert detour.model_detour.history[0]['direct_approach_no_displacement_confirmed'] is True


def test_cloud_future_cancel_does_not_affect_input_loop(monkeypatch,tmp_path):
    import threading
    from bot.decision import route_planner
    monkeypatch.setattr(route_planner,'DATA_DIR',tmp_path)
    release=threading.Event()
    class Client:
        def plan(self,images,facts,cancelled):
            release.wait(1)
            return {'raw_output':json.dumps(plan(facts['frame_id'])),'finish_reason':'stop'}
    request=RouteRequest(np.zeros((586,1012,3),dtype=np.uint8),{'frame_id':'f'},RoutePlannerConfig(),client=Client())
    request.close();release.set()
    with pytest.raises(PlannerError,match='route_cancelled'):request.future.result(timeout=2)


def test_plaintext_credentials_are_loaded_without_copying_into_config(monkeypatch,tmp_path):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    file=tmp_path/'key.txt';file.write_text('test-only-secret\n')
    config=RoutePlannerConfig(credentials_file=str(file))
    assert credentials(config)=='test-only-secret'
    assert 'test-only-secret' not in repr(config)


@pytest.mark.parametrize('flags',[TravelFlags.IN_COMBAT,TravelFlags.FALLING,TravelFlags.FLYING])
def test_parent_interrupts_cloud_route_and_closes_request_on_unsafe_state(flags):
    nav=sample(position=(.2,.21),destination=(.2,.19),flags=TravelFlags.KEYS_VERIFIED,
               task=TaskFlags.NPC_SELECTED)
    trip=QuestTrip(nav.world_size,nav.destination,'ccw_west')
    detour=TerrainDetour(0,nav,0,trip,None)
    cfg=BotConfig();cfg.route_planner.enabled=True
    ctx=SimpleNamespace(cfg=cfg,log_event=lambda *_:None,heading_age=lambda:0)
    detour.begin_model_route(ctx,0,nav,0,'blocked')
    command=detour.step(ctx,1,replace(nav,flags=nav.flags|flags),0)
    assert command.outcome=='probe_interrupted'
    assert detour.model_detour.closed and not command.tap and not command.keys


def test_api_request_contains_images_and_auth_but_report_never_contains_key(monkeypatch,tmp_path):
    from bot.decision import route_planner
    monkeypatch.setattr(route_planner,'DATA_DIR',tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only-secret-value')
    captured=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):
            return json.dumps({'model':'deepseek-flash','choices':[{'finish_reason':'stop',
                'message':{'content':json.dumps(plan('f'))}}],'usage':{'total_tokens':100}}).encode()
    class Opener:
        def open(self,request,timeout):
            assert request.get_header('Authorization')=='Bearer test-only-secret-value'
            payload=json.loads(request.data)
            assert len([b for b in payload['messages'][0]['content'] if b['type']=='image_url'])==2
            assert payload['max_tokens']==1600
            captured.append(True)
            return Response()
    monkeypatch.setattr(route_planner,'build_opener',lambda *_:Opener())
    request=RouteRequest(np.zeros((586,1012,3),dtype=np.uint8),{'frame_id':'f'},RoutePlannerConfig())
    request.future.result(timeout=2)
    assert captured and 'test-only-secret-value' not in (request.evidence_dir/'report.json').read_text()


def test_http_auth_error_is_reported_without_response_body(monkeypatch):
    from bot.decision import route_planner
    from urllib.error import HTTPError
    from threading import Event
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only-key')
    class Opener:
        def open(self,*args,**kwargs):raise HTTPError('https://api.deepseek.com',401,'sensitive-response',{},None)
    monkeypatch.setattr(route_planner,'build_opener',lambda *_:Opener())
    with pytest.raises(PlannerError,match='^deepseek_http_401$'):
        DeepSeekRouteClient(RoutePlannerConfig()).plan([b'image'],{'frame_id':'f'},Event())
