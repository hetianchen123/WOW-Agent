"""A blocked entrance, rear visibility and native motion. All inputs are simulated."""
from dataclasses import replace
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control.building_relocation import TurnawayRelocation
from bot.control.navigation import heading_error
from bot.control.pathfinding import GridRouter
from bot.perception.building_scene import turnaway_view
from bot.perception.travel_channel import TravelFlags
from experiments.route_models.replay_landing_handoff import native
from test_building_entry_recovery import facade_nav
from test_building_relocation import simulate
from test_segmented_runtime import controller


def test_rear_view_uses_local_road_and_vetoes_a_wall_or_water():
    shape=(300,500)
    ground=np.zeros(shape,bool)
    ground[118:169,225:275]=True
    obs=SimpleNamespace(ground=ground,obstacles=np.zeros(shape,bool),
        semantic_labels=np.zeros(shape,np.uint8),semantic_confidence=np.ones(shape))
    assert turnaway_view(obs,{0:'grass'},0)['lanes']==dict(center='open',left='unknown',right='unknown')
    obs.obstacles[136:149,225:275]=True
    assert turnaway_view(obs,{0:'grass'},0)['lanes']['center']=='blocked'
    obs.obstacles[:]=False
    obs.semantic_labels[136:149,225:275]=1
    assert turnaway_view(obs,{0:'grass',1:'river'},0)['lanes']['center']=='blocked'


@pytest.mark.parametrize('rear,contact,age',[('open',False,2),('open',True,16),
                                             ('blocked',False,16),('unknown',False,2)])
def test_turn_faces_reverse_walks_beyond_two_yards_and_recaptures_front(tmp_path,monkeypatch,rear,contact,age):
    obj,ctx,_,routes=controller(tmp_path,monkeypatch)
    sample=json.loads((Path(__file__).parent/'fixtures/building-90896-turnaway.json').read_text())
    nav=native(sample['native'])
    obj.router=GridRouter(*nav.world_size)
    obj.identity=(nav.quest_id,nav.map_id,nav.phase,nav.objective_index,nav.objective_target,
                  nav.world_size,nav.destination,nav.destination_map)
    obj.visual_ui_hidden=nav.visual_ui_hidden
    obj.explored=[nav.position]
    obj.deadline=181
    obj.goal_mode='building_entry'
    obj.config.building_access=True
    heading=initial=sample['heading']
    origin=nav.position
    rear_views=[];events=[];moves=[];yaw_at_collision=None
    ctx.log_event=lambda k,d:events.append((k,d))
    obj.overview_capture=lambda _:np.zeros((200,320,3),np.uint8)
    class Request:
        def __init__(self,image,config,view,outdoors,**opts):
            assert opts['mode']=='building_turnaway_view'
            self.frame_id=f'back-{len(rear_views)}'
            self.center=rear if not rear_views else 'open'
            rear_views.append((heading,nav.position))
        def poll(self):
            return dict(schema=1,frame_id=self.frame_id,executable=False,
                        lanes=dict(center=self.center,left='open',right='unknown'))
        def close(self):pass
    obj.overview_request_factory=Request
    scene=sample['scene']
    obj.seed_building_observation(scene,(nav,heading,1,scene['frame_id'],tuple(sample['frame_size'])))
    if age==16:age=sample['latency_s']
    for i in range(650):
        now=age+1+i*.1
        nav=replace(nav,sequence=(nav.sequence+1)%128)
        cmd=obj.step(ctx,now,nav,heading)
        assert cmd.outcome is None and cmd.tap!='s'
        nav,heading=simulate(cmd,nav,heading)
        if cmd.tap=='w':
            assert rear_views and now<obj.deadline and cmd.hold_s<=.12
            error=abs(heading_error(initial+math.pi,heading))
            assert error<=math.radians(41)
            if not moves and rear=='blocked':assert error>=math.radians(29)
            if contact and not moves:
                yaw_at_collision=heading
            else:
                if yaw_at_collision is not None:
                    assert abs(heading_error(yaw_at_collision,heading))>=math.radians(29)
                yards=6.8*cmd.hold_s
                nav=replace(nav,position=(nav.position[0]-math.sin(heading)*yards/nav.world_size[0],
                                          nav.position[1]-math.cos(heading)*yards/nav.world_size[1]))
            moves.append(cmd.tap)
        if routes:break
    assert routes and not routes[0].job['full_window'] and routes[0].frame.camera_signature[0]==2
    assert len(rear_views)>=2 and moves and 7.2<=obj.router.distance(origin,nav.position)<=8.1
    delta=[(a-b)*size for a,b,size in zip(origin,nav.position,nav.world_size)]
    assert abs(heading_error(math.atan2(-delta[0],-delta[1]),heading))<=math.radians(5)
    assert not obj.passages and obj.progress_at>age
    assert any(k=='quest_building_facade_reacquisition' for k,_ in events)


def test_combat_aborts_reverse_while_waiting_for_image(tmp_path,monkeypatch):
    obj,ctx,nav,_=controller(tmp_path,monkeypatch)
    nav=facade_nav(nav)
    closed=[]
    request=SimpleNamespace(frame_id='rear',poll=lambda:None,close=lambda:closed.append(True))
    obj.goal_mode='building_entry'
    obj.relocation=TurnawayRelocation(1,nav,0,obj.router,obj.convention,deadline=40,
        blocked_positions=[],max_distance=8,max_pulses=40,observe=lambda *_:request)
    obj.relocation.turn=None
    assert not obj.step(ctx,2,nav,math.pi).tap and obj.relocation.request is request
    cmd=obj.step(ctx,2.1,replace(nav,flags=nav.flags|TravelFlags.IN_COMBAT),math.pi)
    assert cmd.outcome=='probe_interrupted' and not cmd.keys and not cmd.tap
    obj.close()
    assert closed==[True]


def obscured_sample():
    path = Path(__file__).parent / 'fixtures/building-90896-obscured-facade.json'
    return json.loads(path.read_text())


@pytest.mark.parametrize('needs_overhead', [False, True])
def test_quest_entry_relocates_when_no_door_is_visible(tmp_path, monkeypatch, needs_overhead):
    """Historical front views through the production task wiring; rear view and physics simulated."""
    from bot.control import building_access, quest_terrain, segmented_detour as sd
    from bot.control.quest_travel import TravelCommand
    from bot.modules import quest
    from bot.perception.travel_channel import CameraSample
    from test_building_access import setup

    sample = obscured_sample()
    front, overhead, _ = sample['frames']
    nav = native(sample['native'])
    initial = heading = front['heading']
    origin = nav.position
    _, ctx, _, events, _, run = setup(tmp_path, monkeypatch)
    run.require_prepared = True
    ctx.cfg.segmented_navigation.enabled = ctx.cfg.segmented_navigation.building_access = True
    monkeypatch.setattr(quest, 'DATA_DIR', tmp_path)
    image = np.zeros((586, 1012, 3), np.uint8)
    monkeypatch.setattr(quest, 'capture_scene', lambda _: image)
    monkeypatch.setattr(quest_terrain, 'capture_building_overview', lambda _: image)
    monkeypatch.setattr(sd, 'preflight', lambda *_: 'test-plane')
    # If the first view has no local evidence, only the overhead view should
    # transfer control. Preserve the recorded latency in both branches.
    if needs_overhead:
        front['scene']['layout']['floor_regions'] = []
        front['scene']['layout']['exterior_regions'] = []
    facades = iter([front, overhead] if needs_overhead else [front])
    rear_views, routes = [], []

    def start_review(request, rgb, config, view, outdoors, **kw):
        assert outdoors
        if kw.get('mode') == 'building_turnaway_view':
            assert kw['full_window']
            request.frame_id = f'rear-{len(rear_views)}'
            rear_views.append((nav.position, heading))
            request.result = dict(schema=1, executable=False, frame_id=request.frame_id,
                                  lanes=dict(center='open', left='unknown', right='unknown'))
            request.ready = now + .4
        else:
            row = next(facades)
            assert view == row['camera']['view']
            request.result = row['scene']
            request.frame_id = request.result['frame_id']
            request.ready = now + row['latency_s']

    monkeypatch.setattr(building_access.BuildingSceneRequest, '__init__', start_review)
    monkeypatch.setattr(building_access.BuildingSceneRequest, 'poll',
                        lambda request: request.result if now >= request.ready else None)
    monkeypatch.setattr(building_access.BuildingSceneRequest, 'close', lambda _: None)
    monkeypatch.setattr(sd.LocalRouteRequest, '__init__',
                        lambda self, img, frame, job: routes.append((frame, job)))
    for tick in range(900):
        now = tick / 10
        nav = replace(nav, sequence=(nav.sequence + 1) % 128)
        start = len(events)
        assert run.process_building_access(ctx, now, nav, heading)
        assert run.OUTCOME is None
        for kind, data in events[start:]:
            if kind != 'quest_movement_input':
                continue
            if 'f6' in data.get('pressed', []):
                nav = replace(nav, camera=CameraSample(**overhead['camera']))
            if 'f8' in data.get('pressed', []):
                nav = replace(nav, camera=replace(CameraSample(**front['camera']),
                              request=(nav.camera.request + 1) % 8))
            if data.get('tap') in {'left', 'right'}:
                nav, heading = simulate(TravelCommand(tap=data['tap'], hold_s=data['hold_s']), nav, heading)
            if data.get('tap') == 'w':
                assert rear_views and abs(heading_error(initial + math.pi, heading)) < math.radians(5)
                yards = 6.8 * data['hold_s']
                nav = replace(nav, position=tuple(a - delta * yards / width for a, delta, width in
                              zip(nav.position, (math.sin(heading), math.cos(heading)), nav.world_size)))
        if routes:
            break
    assert routes and len(rear_views) >= 2
    assert ctx.input.count('key_down', 'w') == ctx.input.count('key_up', 'w') > 0
    assert not ctx.input._held_keys and not run.arrived and not run.building_access.entry.passages
    assert 7.2 <= GridRouter(*nav.world_size).distance(origin, nav.position) <= 8.1
    assert abs(heading_error(initial, heading)) < math.radians(5)
    assert routes[0][0].player_position == nav.position and routes[0][1]['goal_mode'] == 'building_entry'
    receipt = next(data for kind, data in events if kind == 'quest_building_relocation_started')
    assert receipt['reason'] == 'building_facade_obstructed'
    assert receipt['evidence']['source_frame'] == (overhead if needs_overhead else front)['scene']['frame_id']


@pytest.mark.parametrize('case', ['landing', 'after_dismount', 'no_building', 'no_local_ground'])
def test_obscured_facade_requires_local_on_foot_evidence(tmp_path, monkeypatch, case):
    from bot.control.quest_travel import TravelCommand
    from test_building_access import setup
    obj, ctx, _, events, requests, _ = setup(tmp_path, monkeypatch, 'unknown')
    obj.config.building_access = True
    obj.capture = lambda _: np.zeros((586, 1012, 3), np.uint8)
    sample = obscured_sample()
    nav = native(sample['native'])
    obj.identity = (nav.quest_id, nav.map_id, nav.phase, nav.objective_index, nav.destination)
    obj._review(0, case if case in {'landing', 'after_dismount'} else 'initial')
    obj.surface_view = SimpleNamespace(step=lambda *_: None)
    obj.surface_view_used = True
    obj.state = 'surface_view'
    # This review follows a view change. Landing provenance must survive it.
    scene = sample['frames'][1]['scene']
    if case == 'no_local_ground':
        scene['layout']['floor_regions'] = scene['layout']['exterior_regions'] = []
    if case == 'no_building':
        scene['building_visible'] = False
    # This fixture's mock request accepts positional arguments only.
    nav = replace(nav, visual_ui_hidden=None)
    for at in (0., .1, .7, .8):
        nav = replace(nav, sequence=(nav.sequence + 1) % 128)
        if requests:
            requests[0].result = dict(scene, frame_id=requests[0].frame_id)
        assert obj.step(ctx, at, nav, 0.) == TravelCommand()
    assert requests and requests[0].closed and obj.entry_observation is None
    assert obj.state == 'review' and not obj.ground_only
    assert not any(k == 'quest_building_local_observation' for k, _ in events)
