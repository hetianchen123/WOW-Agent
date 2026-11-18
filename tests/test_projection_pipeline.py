from dataclasses import asdict, replace
import hashlib
import json
import math
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bot import projection_cli
from bot.control.projected_route import plan_projected_route
from bot.decision.image_navigation import SceneMap
from bot.perception.ground_projection import ProjectionError, GroundProjection
from test_ground_calibration import scene
from test_ground_projection import fixture


def test_obstacle_route_to_world_points_preserves_every_bend_and_heading():
    frame, training, checks, world = fixture()
    projection = GroundProjection(frame, training, checks)
    geometry = SceneMap('f', (800, 600), ([[240, 160], [560, 160], [560, 440], [240, 440]],),
                        ([[435, 270], [460, 270], [460, 335], [435, 335]],), source='synthetic_regression')
    result = plan_projected_route(projection, geometry, (520, 300), frame, now=11)
    pixels = result['image_route']['points']
    assert len(pixels) > 2  # End-to-end path genuinely goes around the wall.
    assert np.allclose(result['world_route']['world_points'], [world(p) for p in pixels], atol=1e-7)
    for segment in result['segments']:
        heading, yards = segment['heading_radians'], segment['yards']
        dx = (segment['to'][0]-segment['from'][0])*frame.world_size[0]
        dy = (segment['to'][1]-segment['from'][1])*frame.world_size[1]
        assert abs(-math.sin(heading)*yards-dx) < 1e-8
        assert abs(-math.cos(heading)*yards-dy) < 1e-8
    assert result['executable'] is False and result['world_collision_verified'] is False
    with pytest.raises(ProjectionError, match='expired'):
        plan_projected_route(projection, geometry, (520, 300), frame, now=100)


def test_capture_review_files_build_and_export_without_a_model(tmp_path):
    ref, train, check, _ = scene()
    for i, sample in enumerate([ref, *train, *check]):
        folder = tmp_path/str(i); folder.mkdir()
        image, mask = folder/'image.png', folder/'ground.png'
        cv2.imwrite(str(image), cv2.cvtColor(sample.image, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(mask), sample.ground_mask)
        frame = replace(sample.frame, image_sha256=hashlib.sha256(image.read_bytes()).hexdigest())
        projection_cli.dump(folder/'sample.json', {'frame': asdict(frame), 'sequence': sample.sequence,
            'telemetry_at': sample.telemetry_at, 'source': sample.source, 'reviewed': True,
            'mask_sha256': hashlib.sha256(mask.read_bytes()).hexdigest()})
    manifest = tmp_path/'session.json'
    projection_cli.dump(manifest, {'reference': '0', 'training': [str(i) for i in range(1, 7)],
                                  'validation': [str(i) for i in range(7, 10)]})
    bundle = projection_cli.build_bundle(manifest, tmp_path/'built')
    assert bundle['validation_max_error_yards'] < .01
    assert (tmp_path/'built/anchors.png').exists()
    geometry = {'frame_id': ref.frame.frame_id, 'image_sha256': bundle['frame']['image_sha256'],
        'size': [800, 600], 'ground': [[[285, 205], [515, 205], [515, 395], [285, 395]]],
        'obstacles': [[[435, 270], [455, 270], [455, 325], [435, 325]]],
        'goal': [490, 300], 'source': 'synthetic_regression'}
    projection_cli.dump(tmp_path/'map.json', geometry)
    result = projection_cli.plan_bundle(tmp_path/'built/bundle.json', tmp_path/'map.json', tmp_path/'world.json')
    assert len(result['segments']) >= 3
    assert result['mode'] == 'historical_replay_not_live' and not result['executable']
    geometry['image_sha256'] = '0'*64
    projection_cli.dump(tmp_path/'map.json', geometry)
    with pytest.raises(ProjectionError, match='map_image_mismatch'):
        projection_cli.plan_bundle(tmp_path/'built/bundle.json', tmp_path/'map.json', tmp_path/'world.json')
    (tmp_path/'1/ground.png').write_bytes(b'tampered')
    with pytest.raises(ProjectionError, match='hash_mismatch'): projection_cli.read_sample(tmp_path/'1')


@pytest.mark.parametrize('moving', [False, True])
def test_read_only_capture_rejects_motion_and_always_closes_device(tmp_path, monkeypatch, moving):
    from bot.perception import capture as capture_module
    from bot.core import config
    from bot import terrain_cli
    from bot.perception.travel_channel import CameraSample, CameraScene, CameraResult
    closed = []
    monkeypatch.setattr(config, 'load_config', lambda: SimpleNamespace(channel=SimpleNamespace(calibrated=True), capture=None))
    monkeypatch.setattr(capture_module, 'frontmost_game_bounds', lambda: (0, 0, 1000, 800))
    monkeypatch.setattr(capture_module, 'ScreenCapture', lambda _: SimpleNamespace(
        grab=lambda x, y, w, h: np.zeros((h, w, 3), np.uint8), close=lambda: closed.append(True)))
    camera = CameraSample(32, 4, CameraScene.OVERHEAD, CameraScene.OVERHEAD, 1, CameraResult.DISTANCE_VERIFIED)
    values = [SimpleNamespace(position=(.25, .36), world_size=(1000, 500), sequence=1, map_id=2451,
                              height=100, camera=camera, camera_epoch=7),
              SimpleNamespace(position=(.252 if moving else .25, .36), world_size=(1000, 500), sequence=2,
                              map_id=2451, height=100, camera=camera, camera_epoch=7)]
    monkeypatch.setattr(terrain_cli, 'ground_pose', lambda *_: (values.pop(0), 0))
    monkeypatch.setattr(projection_cli.time, 'monotonic', lambda: 100)
    if moving:
        with pytest.raises(ProjectionError, match='not_stationary'):
            projection_cli.capture_sample(tmp_path/'sample', 'verified-local-plane', 0)
        assert not (tmp_path/'sample/sample.json').exists()
    else:
        projection_cli.capture_sample(tmp_path/'sample', 'verified-local-plane', 0)
        result = json.loads((tmp_path/'sample/sample.json').read_text())
        assert result['frame']['player_feet'] is None and result['reviewed'] is False
        assert result['frame']['camera_signature'] == [4, 32, 7]
    assert closed == [True]


def test_passive_session_does_not_count_duplicate_standing_samples(tmp_path, monkeypatch):
    positions = iter([(.25, .36), (.25, .36), *[(.25+i*.001, .36) for i in range(1, 10)]])
    def capture(folder, floor, delay):
        folder.mkdir()
        projection_cli.dump(folder/'sample.json', {'frame': {'player_position': next(positions),
                                                            'world_size': (1000, 500)}})
    monkeypatch.setattr(projection_cli, 'capture_sample', capture)
    monkeypatch.setattr(projection_cli.time, 'sleep', lambda _: None)
    monkeypatch.setattr(projection_cli.time, 'monotonic', lambda: 0)
    projection_cli.record_session(tmp_path/'session', 'ground', 0)
    data = json.loads((tmp_path/'session/session.json').read_text())
    assert data == {'reference': '09', 'training': ['00', '01', '02', '03', '04', '05'],
                    'validation': ['06', '07', '08']}
    assert len(list((tmp_path/'session').glob('*/sample.json'))) == 10


def test_segmented_bundle_uses_native_anchor_and_target_region_with_hash_binding(tmp_path):
    frame,training,checks,world=fixture()
    bundle={'frame':asdict(frame),'training':[asdict(a) for a in training],
            'validation':[asdict(a) for a in checks]}
    projection_cli.dump(tmp_path/'bundle.json',bundle)
    ground=np.ones((600,800),bool);soft=np.zeros_like(ground)
    soft[270:330,370:430]=True;ground[soft]=False
    empty=np.zeros_like(ground)
    mask_path=tmp_path/'masks.npz'
    np.savez_compressed(mask_path,ground=ground,obstacles=empty,occluded=soft,water=empty,forbidden=empty)
    spec={'frame_id':'f','image_sha256':frame.image_sha256,'segmentation_file':'masks.npz',
        'segmentation_sha256':hashlib.sha256(mask_path.read_bytes()).hexdigest(),
        'target':{'source':'synthetic_regression','world_position':world((520,300)),
                  'approach_radius_yards':.6}}
    projection_cli.dump(tmp_path/'map.json',spec)
    result=projection_cli.plan_bundle(tmp_path/'bundle.json',tmp_path/'map.json',tmp_path/'out.json')
    assert result['mode']=='historical_replay_not_live' and not result['executable']
    assert result['next_step']=='probe_then_reobserve' and result['next_segments']
    spec['target']['source']='quest_map_pin'
    projection_cli.dump(tmp_path/'map.json',spec)
    with pytest.raises(ProjectionError,match='target_position_unverified'):
        projection_cli.plan_bundle(tmp_path/'bundle.json',tmp_path/'map.json',tmp_path/'out.json')
    mask_path.write_bytes(b'different frame')
    with pytest.raises(ProjectionError,match='mask_hash_mismatch'):
        projection_cli.plan_bundle(tmp_path/'bundle.json',tmp_path/'map.json',tmp_path/'out.json')
