"""Read-only ground calibration: capture, review, build. Never sends game input."""
import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import time
from uuid import uuid4

import cv2
import numpy as np

from .perception.ground_projection import ProjectionFrame, ProjectionError
from .perception.ground_calibration import CalibrationSample, build_calibration


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def capture_sample(folder, floor_id, delay):
    from .core.config import load_config
    from .perception.capture import ScreenCapture, frontmost_game_bounds
    from .perception.scene_scale import SCENE_BOUNDS, overhead_profile
    from .terrain_cli import ground_pose
    from .control.navigation import heading_error
    cfg = load_config()
    if not cfg.channel.calibrated:
        raise ProjectionError('calibration_channel_not_calibrated')
    if folder.exists():
        raise ProjectionError('calibration_output_already_exists')
    time.sleep(delay)
    capture = ScreenCapture(cfg.capture)
    def guard():
        frontmost_game_bounds()
    try:
        before, heading = ground_pose(cfg, capture, guard)
        if not overhead_profile(before.camera) or before.height is None:
            raise ProjectionError('calibration_overhead_receipt_missing')
        bounds = frontmost_game_bounds()
        x, y, w, h = bounds
        l, t, r, b = SCENE_BOUNDS
        rect = (round(x+w*l), round(y+h*t), round(w*r)-round(w*l), round(h*b)-round(h*t))
        started = time.monotonic()
        rgb = capture.grab(*rect)
        captured_at = (started+time.monotonic())/2
        after, after_heading = ground_pose(cfg, capture, guard)
        telemetry_at = time.monotonic()
        displacement = math.hypot(*[(a-b)*s for a, b, s in zip(after.position, before.position, before.world_size)])
        if (before.sequence == after.sequence or before.map_id != after.map_id or before.world_size != after.world_size
                or before.camera != after.camera or before.camera_epoch != after.camera_epoch
                or after.height is None or abs(before.height-after.height) > .1 or displacement > .08
                or abs(heading_error(heading, after_heading)) > math.radians(1)
                or telemetry_at-captured_at > .25 or bounds != frontmost_game_bounds()):
            raise ProjectionError('calibration_capture_not_stationary_or_synchronized')
        folder.mkdir(parents=True)
        path = folder/'image.png'
        if not cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)):
            raise ProjectionError('calibration_image_write_failed')
        frame = ProjectionFrame(uuid4().hex, hashlib.sha256(path.read_bytes()).hexdigest(),
            (rgb.shape[1], rgb.shape[0]), after.map_id, floor_id, after.world_size, after.position,
            None, after.height, after_heading, (after.camera.view, after.camera.zoom, after.camera_epoch), captured_at)
        dump(folder/'sample.json', {'frame': asdict(frame), 'sequence': after.sequence, 'telemetry_at': telemetry_at,
            'source': 'native_position_ground_correspondence', 'reviewed': False, 'capture_rect': rect,
            'before_sequence': before.sequence, 'capture_displacement_yards': displacement})
    finally:
        capture.close()


def review_sample(folder, confirmed):
    """Offline GUI; the user reviews feet and static planar ground, not gameplay."""
    if not confirmed:
        raise ProjectionError('review_requires_confirm_same_ground')
    data = json.loads((folder/'sample.json').read_text())
    source = cv2.imread(str(folder/'image.png'))
    if source is None or hashlib.sha256((folder/'image.png').read_bytes()).hexdigest() != data['frame']['image_sha256']:
        raise ProjectionError('calibration_image_hash_mismatch')
    mask = np.zeros(source.shape[:2], np.uint8)
    feet, points, mode = None, [], 'feet'
    window = 'Ground calibration review (offline)'
    def click(event, x, y, flags, param):
        nonlocal feet
        if event == cv2.EVENT_LBUTTONDOWN:
            if mode == 'feet': feet = (x, y)
            else: points.append((x, y))
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window, click)
    try:
        while True:
            canvas = source.copy()
            canvas[mask > 0] = (canvas[mask > 0]*.6+np.array([0, 255, 0])*.4).astype(np.uint8)
            if feet: cv2.circle(canvas, feet, 5, (255, 0, 255), 2)
            if points: cv2.polylines(canvas, [np.array(points)], False, (0, 255, 255), 2)
            cv2.putText(canvas, f'{mode}: F feet, G ground, X exclude, Enter polygon, R reset, S save, Esc cancel',
                        (8, 20), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)
            cv2.imshow(window, canvas)
            key = cv2.waitKey(30) & 255
            if key == 27 or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                raise ProjectionError('calibration_review_cancelled')
            if key in (ord('f'), ord('g'), ord('x')):
                mode = {ord('f'): 'feet', ord('g'): 'ground', ord('x'): 'exclude'}[key]; points = []
            if key in (10, 13) and len(points) >= 3 and mode != 'feet':
                cv2.fillPoly(mask, [np.array(points)], 255 if mode == 'ground' else 0); points = []
            if key == ord('r'): mask[:] = 0; feet = None; points = []
            if key == ord('s') and feet and np.count_nonzero(mask) >= 1000 and not points:
                # Avatar animation cannot be a ground feature. Other characters,
                # shadows, foliage, posts and UI must be excluded in the review.
                cv2.circle(mask, feet, 24, 0, -1)
                mask_path = folder/'ground.png'
                if not cv2.imwrite(str(mask_path), mask):
                    raise ProjectionError('calibration_mask_write_failed')
                data['frame']['player_feet'] = feet
                data.update(reviewed=True, mask_sha256=hashlib.sha256(mask_path.read_bytes()).hexdigest())
                dump(folder/'sample.json', data)
                break
    finally:
        cv2.destroyWindow(window)


def record_session(folder, floor_id, delay, duration=180):
    """Collect ten distinct stationary positions while the user moves manually."""
    import shutil
    if folder.exists():
        raise ProjectionError('calibration_output_already_exists')
    folder.mkdir(parents=True)
    time.sleep(delay)
    deadline, positions, count = time.monotonic()+duration, [], 0
    while count < 10 and time.monotonic() < deadline:
        sample = folder/f'{count:02d}'
        try:
            capture_sample(sample, floor_id, 0)
            data = json.loads((sample/'sample.json').read_text())
            frame = data['frame']
            # A standing player must not fill the calibration with duplicates.
            if any(math.hypot(*[(a-b)*s for a, b, s in zip(frame['player_position'], old, frame['world_size'])]) < .5
                   for old in positions):
                shutil.rmtree(sample)  # Only this function's just-created sample.
                print('Waiting for a distinct stationary position.', flush=True)
            else:
                positions.append(frame['player_position']); count += 1
                print(json.dumps({'sample': str(sample), 'collected': count, 'required': 10}), flush=True)
        except (ValueError, OSError) as exc:
            print(json.dumps({'waiting': str(exc)}), flush=True)
            # A partially saved record is retained as evidence; do not overwrite it.
            if sample.exists(): raise
        time.sleep(2)
    if count != 10:
        raise ProjectionError('calibration_collection_incomplete')
    dump(folder/'session.json', {'reference': '09', 'training': [f'{i:02d}' for i in range(6)],
                                 'validation': [f'{i:02d}' for i in range(6, 9)]})


def read_sample(folder):
    data = json.loads((folder/'sample.json').read_text())
    for name, expected in [('image.png', data['frame']['image_sha256']), ('ground.png', data.get('mask_sha256'))]:
        if not expected or hashlib.sha256((folder/name).read_bytes()).hexdigest() != expected:
            raise ProjectionError('calibration_image_or_mask_hash_mismatch')
    frame = data['frame']
    for field in ['size', 'world_size', 'player_position', 'player_feet', 'camera_signature']:
        if frame[field] is not None: frame[field] = tuple(frame[field])
    image = cv2.cvtColor(cv2.imread(str(folder/'image.png')), cv2.COLOR_BGR2RGB)
    return CalibrationSample(ProjectionFrame(**frame), image, cv2.imread(str(folder/'ground.png'), 0),
                             data['sequence'], data['telemetry_at'], data['source'], data['reviewed'])


def build_bundle(manifest, output):
    data = json.loads(manifest.read_text())
    reference = read_sample(manifest.parent/data['reference'])
    training = [read_sample(manifest.parent/p) for p in data['training']]
    validation = [read_sample(manifest.parent/p) for p in data['validation']]
    projection, train, check, records = build_calibration(reference, training, validation)
    output.mkdir(parents=True, exist_ok=True)
    bundle = {'frame': asdict(reference.frame), 'training': list(map(asdict, train)),
              'validation': list(map(asdict, check)), 'executable': False,
              'registration': records, 'validation_max_error_yards': projection.validation_error}
    # Portable reference evidence is needed to register FUTURE exposures. An old
    # matrix alone must never be relabelled as a fresh calibration.
    import shutil
    shutil.copyfile(manifest.parent/data['reference']/'image.png', output/'reference.png')
    shutil.copyfile(manifest.parent/data['reference']/'ground.png', output/'reference-ground.png')
    bundle['reference_image'] = 'reference.png'
    bundle['reference_mask'] = 'reference-ground.png'
    bundle['reference_mask_sha256'] = hashlib.sha256((output/'reference-ground.png').read_bytes()).hexdigest()
    bundle['source'] = reference.source
    if 'route' in data: bundle['route'] = data['route']
    dump(output/'bundle.json', bundle)
    canvas = cv2.cvtColor(reference.image, cv2.COLOR_RGB2BGR)
    for group, color in [(train, (255, 180, 0)), (check, (0, 255, 255))]:
        for i, anchor in enumerate(group):
            point = tuple(round(v) for v in anchor.pixel)
            cv2.circle(canvas, point, 5, color, 2)
            cv2.putText(canvas, str(i), point, cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1)
    cv2.imwrite(str(output/'anchors.png'), canvas)
    return bundle


def plan_bundle(bundle_path, map_path, output):
    from .perception.ground_projection import GroundProjection, GroundAnchor
    from .decision.image_navigation import SceneMap
    from .control.projected_route import plan_projected_route, plan_segmented_approach, interaction_region
    bundle, scene = json.loads(bundle_path.read_text()), json.loads(map_path.read_text())
    fields = bundle['frame']
    for name in ['size', 'world_size', 'player_position', 'player_feet', 'camera_signature']:
        fields[name] = tuple(fields[name])
    frame = ProjectionFrame(**fields)
    if scene.get('image_sha256') != frame.image_sha256:
        raise ProjectionError('projection_map_image_mismatch')
    projection = GroundProjection(frame, [GroundAnchor(**a) for a in bundle['training']],
                                  [GroundAnchor(**a) for a in bundle['validation']])
    if 'segmentation_file' in scene:
        from types import SimpleNamespace
        masks_path = map_path.parent/scene['segmentation_file']
        if hashlib.sha256(masks_path.read_bytes()).hexdigest() != scene.get('segmentation_sha256'):
            raise ProjectionError('projection_mask_hash_mismatch')
        with np.load(masks_path, allow_pickle=False) as data:
            masks = SimpleNamespace(**{k:data[k].copy() for k in ('ground','obstacles','occluded','water','forbidden')})
        target = scene.get('target', {})
        if target.get('source') not in {'native_target_position','measured_target_position','synthetic_regression'}:
            raise ProjectionError('approach_target_position_unverified')
        region = interaction_region(projection, target['world_position'], target['approach_radius_yards'],
                                    position_error_yards=target.get('position_error_yards',0))
        result = plan_segmented_approach(projection,masks,region,frame,now=frame.captured_at,
            frame_id=scene['frame_id'],image_sha256=scene['image_sha256'])
        result['mode'] = 'historical_replay_not_live'
        output.parent.mkdir(parents=True,exist_ok=True)
        dump(output,result)
        return result
    observed = SceneMap(scene['frame_id'], tuple(scene['size']), tuple(scene['ground']),
                        tuple(scene['obstacles']), tuple(scene.get('unknown', [])), scene['source'])
    # Historical replay time is explicit. This function cannot authorize current
    # movement based on an old saved image or manufacture a fresh timestamp.
    result = plan_projected_route(projection, observed, scene['goal'], frame, now=frame.captured_at)
    result['mode'] = 'historical_replay_not_live'
    output.parent.mkdir(parents=True, exist_ok=True)
    dump(output, result)
    return result


def calibration_status(bundle_path):
    from .perception.live_projection import read_bundle
    projection,_,_,_=read_bundle(bundle_path)
    data=json.loads(bundle_path.read_text())
    return dict(native_measurements=True,map_id=projection.frame.map_id,floor_id=projection.frame.floor_id,
        validation_max_error_yards=projection.validation_error,targets=sorted(data.get('targets',{})),
        reference_frame_id=projection.frame.frame_id,requires_current_image_registration=True,
        static_prerequisites_ready=bool(data.get('targets')),runtime_movement_authorized=False,game_input_sent=False)


def bind_approach_region(bundle_path,pixel,quest_id,phase,objective_index,radius,error,confirmed):
    """Register a reviewed nearby standing region, not the NPC's hidden feet.

    This is preparation data for a static task target; selecting it is not proof
    of interaction range. Native range/identity receipts still finish the approach.
    """
    from .perception.live_projection import read_bundle
    from .control.projected_route import interaction_region
    if not confirmed:raise ProjectionError('calibration_static_target_review_required')
    projection,_,_,_=read_bundle(bundle_path)
    if not projection.contains(pixel):raise ProjectionError('projection_outside_calibrated_ground')
    if quest_id<=0 or objective_index<0:raise ProjectionError('calibration_target_identity_invalid')
    world=tuple(float(o+d/s) for o,d,s in zip(projection.frame.player_position,
               projection.delta(pixel),projection.frame.world_size))
    interaction_region(projection,world,radius,position_error_yards=error)
    data=json.loads(bundle_path.read_text())
    key=f'{quest_id}:{phase}:{objective_index}'
    data.setdefault('targets',{})[key]=dict(source='reviewed_static_approach_region',
        world_position=world,radius_yards=radius,position_error_yards=error,
        map_id=projection.frame.map_id,floor_id=projection.frame.floor_id,
        bundle_image_sha256=projection.frame.image_sha256,pixel=list(pixel))
    dump(bundle_path,data)
    return dict(target_key=key,world_position=world,interaction_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture'); capture.add_argument('--output', type=Path, required=True)
    capture.add_argument('--floor-id', required=True); capture.add_argument('--delay', type=float, default=3)
    record = sub.add_parser('record'); record.add_argument('--output', type=Path, required=True)
    record.add_argument('--floor-id', required=True); record.add_argument('--delay', type=float, default=3)
    review = sub.add_parser('review'); review.add_argument('--sample', type=Path, required=True)
    review.add_argument('--confirm-same-ground', action='store_true')
    build = sub.add_parser('build'); build.add_argument('--manifest', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    plan = sub.add_parser('plan'); plan.add_argument('--bundle', type=Path, required=True)
    plan.add_argument('--map', type=Path, required=True); plan.add_argument('--output', type=Path, required=True)
    status=sub.add_parser('status');status.add_argument('--bundle',type=Path,required=True)
    target=sub.add_parser('bind-target');target.add_argument('--bundle',type=Path,required=True)
    target.add_argument('--pixel',type=float,nargs=2,required=True)
    target.add_argument('--quest-id',type=int,required=True)
    target.add_argument('--phase',choices=['READY','PENDING','ACTIVE'],required=True)
    target.add_argument('--objective-index',type=int,default=0)
    target.add_argument('--radius-yards',type=float,default=.8)
    target.add_argument('--position-error-yards',type=float,default=.25)
    target.add_argument('--confirm-static-region',action='store_true')
    args = parser.parse_args()
    try:
        if args.command in {'capture', 'record'}:
            if not 0 <= args.delay <= 30 or args.floor_id in {'unknown', 'unverified'}:
                raise ProjectionError('calibration_capture_arguments_invalid')
            if args.command == 'capture': capture_sample(args.output, args.floor_id, args.delay)
            else: record_session(args.output, args.floor_id, args.delay)
        elif args.command == 'review': review_sample(args.sample, args.confirm_same_ground)
        elif args.command == 'build': build_bundle(args.manifest, args.output)
        elif args.command=='plan': plan_bundle(args.bundle, args.map, args.output)
        elif args.command=='status': print(json.dumps(calibration_status(args.bundle),ensure_ascii=False))
        elif args.command=='bind-target':
            print(json.dumps(bind_approach_region(args.bundle,args.pixel,args.quest_id,args.phase,
                args.objective_index,args.radius_yards,args.position_error_yards,args.confirm_static_region)))
        print(json.dumps({'status': 'saved', 'command': args.command, 'game_input_sent': False}))
    except (ValueError, OSError, TypeError, KeyError, cv2.error) as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc), 'game_input_sent': False}))
        raise SystemExit(1) from None


if __name__ == '__main__': main()
