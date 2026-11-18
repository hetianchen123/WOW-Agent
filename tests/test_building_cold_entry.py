"""Recorded 90896 facade observations, mock inputs and simulated native motion."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.control import segmented_detour as sd
from bot.control.quest_travel import TravelCommand
from bot.control.pathfinding import GridRouter
from bot.decision.automatic_approach import calibration_probe
from experiments.route_models.replay_landing_handoff import native
from test_building_access import setup
from test_building_relocation import simulate


def frames():
    path = Path(__file__).parent / 'fixtures/building-90896-cold-entry.json'
    return json.loads(path.read_text())['frames']


@pytest.mark.parametrize('contact', [False, True])
def test_real_facade_cold_start_moves_then_measures_again(tmp_path, monkeypatch, contact):
    facade, overhead = frames()
    access, ctx, _, events, _, run = setup(tmp_path, monkeypatch)
    nav = native(facade['nav'])
    heading = facade['heading']
    original_position = nav.position
    run.building_access = access
    run.require_prepared = True
    monkeypatch.setattr(run, 'adjust_scene_camera',
        lambda *_a, **_kw: pytest.fail('ordinary travel camera interrupted doorway handoff'))
    ctx.cfg.segmented_navigation.enabled = ctx.cfg.segmented_navigation.building_access = True
    image = np.zeros((586, 1012, 3), np.uint8)
    access.capture = lambda _: image
    clock = [0.]
    pending = iter([facade, overhead])
    measurements = []

    class SavedReview:
        def __init__(self, rgb, config, view, outdoors, **kwargs):
            row = next(pending)  # A third same-position review was the live failure.
            assert view == row['nav']['camera']['view']
            self.scene = row['scene']
            self.frame_id = self.scene['frame_id']
            self.ready = clock[0] + row['latency_s']

        def poll(self):
            return self.scene if clock[0] >= self.ready else None

        def close(self):
            pass

    class Measurement:
        def __init__(self, rgb, frame, job):
            measurements.append((frame, job))
            self.result = None

        def poll(self):
            return self.result

        def close(self):
            pass

    access.request_factory = SavedReview
    monkeypatch.setattr(sd, 'preflight', lambda *_: 'test-plane')
    access.entry_factory = lambda at, nav, ctx: sd.SegmentedDetour(
        at, nav, GridRouter(*nav.world_size), 'ccw_west', access.config, access.capture,
        deadline=at+180, goal_mode='building_entry', ground_trace=run.ground_trace,
        request_factory=Measurement)

    for tick in range(500):
        clock[0] = tick / 10
        nav = replace(nav, sequence=(nav.sequence+1) % 128)
        event_count = len(events)
        assert not run.restore_camera(ctx,clock[0],nav)
        assert run.process_building_access(ctx, clock[0], nav, heading)
        for kind, event in events[event_count:]:
            if kind == 'quest_movement_input' and 'f6' in event.get('pressed', []):
                nav = native(overhead['nav'])
            if kind == 'quest_movement_input' and event.get('tap') == 's' and not contact:
                nav, _ = simulate(TravelCommand(tap='s', hold_s=event['hold_s']), nav, heading)
                nav = replace(nav, position=tuple(round(v*65535)/65535 for v in nav.position))
        if measurements:
            break

    assert run.OUTCOME is None and run.building_pending and not run.arrived
    assert not access.points and not access.intents and run.trip is None
    assert len(measurements) == 1 and measurements[0][0].player_position == nav.position
    assert measurements[0][0].frame_id != overhead['scene']['frame_id']
    assert measurements[0][1]['visited'] == [original_position]
    assert ctx.input.count('key_down', 's') == ctx.input.count('key_up', 's') > 0
    assert not ctx.input._held_keys
    assert not any(kind in {'quest_building_failed', 'quest_building_entered'} for kind, _ in events)
    evidence = next(v['evidence'] for k,v in events if k == 'quest_building_relocation_started')
    assert evidence['source_frame'] == overhead['scene']['frame_id']
    assert evidence['local_surface_box'] == overhead['scene']['layout']['exterior_regions'][0]['box']
    pulses = [v for k,v in events if k == 'quest_building_relocation_pulse']
    feedback = [v for k,v in events if k == 'quest_building_relocation_feedback']
    assert len(pulses) == len(feedback) and all(len(v['after_sequences']) >= 2 for v in feedback)
    result = next(v for k,v in events if k == 'quest_building_relocation_result')
    if contact:
        assert len(pulses) == 1 and result['reason'] == 'building_relocation_blocked'
        assert nav.position == original_position and measurements[0][1]['blocked_probe_keys'] == ['s']
        # The next independently observed corridor can produce a different input.
        ground = np.ones((200,200),bool)
        masks = SimpleNamespace(ground=ground, obstacles=~ground, water=~ground,
                                occluded=~ground, forbidden=~ground)
        choice = calibration_probe(masks,(100,100),[],strafe_verified=True,
            blocked_keys=measurements[0][1]['blocked_probe_keys'])
        assert choice['key'] != 's'
        # Inject a *new*, synthetically measured clear corridor after contact.
        # Exercise result validation and the same task input backend again.
        frame,job = measurements[0]
        access.entry.request.result = dict(choice,frame_id=frame.frame_id,executable=False,
            requires_fresh_feedback=True,calibration={'stage':'translation'},
            target_key=f"{nav.quest_id}:{nav.phase.name}:{nav.objective_index}",
            building_scene=dict(schema=1,surface='unknown',executable=False,doorways=[]))
        for _ in range(2):
            clock[0] += .1
            nav = replace(nav, sequence=(nav.sequence+1)%128)
            assert run.process_building_access(ctx,clock[0],nav,heading)
        assert ctx.input.count('key_down',choice['key']) == ctx.input.count('key_up',choice['key']) == 1
        assert ctx.input.count('key_down','s') == 1
    else:
        assert result['reason'] == 'building_relocated'
        assert .8 < access.entry.router.distance(original_position,nav.position) <= 1.1
        assert not access.entry.passages


@pytest.mark.parametrize('change', ['camera','position','age','ui','height','heading','objective'])
def test_stale_facade_handoff_cannot_authorize_retreat(tmp_path,monkeypatch,change):
    from test_segmented_runtime import controller
    obj,ctx,_,_ = controller(tmp_path,monkeypatch)
    overhead = frames()[1]
    nav,heading = native(overhead['nav']),overhead['heading']
    obj.goal_mode = 'building_entry'
    obj.identity = (nav.quest_id,nav.map_id,nav.phase,nav.objective_index,nav.objective_target,
                    nav.world_size,nav.destination,nav.destination_map)
    obj.visual_ui_hidden = nav.visual_ui_hidden
    scene = overhead['scene']
    obj.seed_building_observation(scene,(nav,heading,1.,scene['frame_id'],(1012,586)))
    now = 2.
    if change == 'camera': nav = replace(nav,camera_epoch=nav.camera_epoch+1)
    if change == 'position': nav = replace(nav,position=(nav.position[0]+.001,nav.position[1]))
    if change == 'age': now = 17.
    if change == 'ui': nav = replace(nav,visual_ui_hidden=0)
    if change == 'height': nav = replace(nav,height=nav.height+1)
    if change == 'heading': heading += .3
    if change == 'objective':
        old,angle,stamp,fid,size = obj.building_observation
        obj.building_observation = replace(old,objective_target=999),angle,stamp,fid,size
    assert obj.step(ctx,now,nav,heading) == TravelCommand()
    assert obj.relocation is None and obj.building_observation is None


def test_navigation_pulse_limits_apply_before_pressing_any_key(tmp_path,monkeypatch):
    _,ctx,_,_,_,_ = setup(tmp_path,monkeypatch)
    for key,duration in [('s',.121),('left',.121),('f6',.12),('w',0),('w',float('nan'))]:
        with pytest.raises(ValueError,match='precise_pulse_duration_invalid'):
            ctx.input.tap_precise(key,duration)
    assert ctx.input.count('key_down') == 0


@pytest.mark.parametrize('case',['clear_threshold','no_door','no_local_ground','roof','water'])
def test_cold_start_only_repositions_for_an_obscured_local_entrance(tmp_path,monkeypatch,case):
    access,ctx,nav,_,requests,_ = setup(tmp_path,monkeypatch)
    scene = frames()[1]['scene']
    access.capture = lambda _: np.zeros((586,1012,3),np.uint8)
    access.step(ctx,1.,nav,0.)
    access.step(ctx,1.6,replace(nav,sequence=2),0.)
    scene['frame_id'] = requests[0].frame_id
    if case=='clear_threshold':
        scene['doorways'][0]['threshold_observed'] = True
    if case=='no_door':
        scene['doorways'] = []
    if case=='no_local_ground':
        scene['layout']['exterior_regions'] = scene['layout']['floor_regions'] = []
    if case=='roof':
        scene['surface'] = 'structure'
    if case=='water':
        scene['surface'] = 'water'
    requests[0].result = scene
    cmd = access.step(ctx,1.7,replace(nav,sequence=3),0.)
    assert access.entry_observation is None and not cmd.tap and not cmd.keys
    assert (access.state=='ground_travel') == (case=='clear_threshold')
