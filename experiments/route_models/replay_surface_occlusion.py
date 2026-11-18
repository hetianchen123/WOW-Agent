"""Re-run local perception on saved landing images; no game or input access."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bot.control.segmented_detour import local_worker, close_local_worker
from experiments.route_models.replay_landing_handoff import saved_cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', type=Path, default=ROOT/'tests/fixtures/building-surface-occlusion-20261117.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads(args.fixture.read_text())['cases']
    results, runs = [], {}
    began = time.monotonic()
    try:
        for fixture in fixtures:
            run = fixture['run']
            if run not in runs:
                runs[run] = {c['result']['frame_id']: c for c in saved_cases(ROOT/'data/runs'/run)}
            case = runs[run][fixture['frame_id']]
            if (case['result']['image_sha256'] != fixture['image_sha256']
                    or case['nav']['camera']['view'] != fixture['camera_view']):
                raise ValueError('saved_surface_fixture_changed')
            folder = args.output/fixture['frame_id']
            folder.mkdir(parents=True, exist_ok=True)
            image = folder/'image.png'
            shutil.copyfile(case['image'], image)
            job = dict(mode='building_scene', frame_id=fixture['frame_id'],
                camera_view=case['nav']['camera']['view'],
                models=str(ROOT/'data/segmentation/models.json'),
                checkpoint=str(ROOT/'data/segmentation/sam2.1_hiera_large.pt'), device='cpu')
            (folder/'request.json').write_text(json.dumps(job, indent=2))
            result = local_worker().inspect([image], target=job, timeout=30)[0]
            if (result.get('frame_id') != job['frame_id'] or
                    result.get('image_sha256') != hashlib.sha256(image.read_bytes()).hexdigest()):
                raise ValueError('replay_result_identity_mismatch')
            passed = (result['surface'] == fixture['expected'] and not result['executable']
                      and result['world_coordinates'] is None and not result['entrance_verified'])
            results.append(dict(frame_id=job['frame_id'], source=case['image'], run=run,
                native_event=case['native_event'], camera_view=job['camera_view'],
                expected=fixture['expected'], previous_surface=fixture['old_surface'],
                passed=passed, result=result))
            print(json.dumps(dict(frame_id=job['frame_id'], surface=result['surface'], passed=passed)), flush=True)
    finally:
        close_local_worker()
        (args.output/'replay.json').write_text(json.dumps(dict(
            kind='real_local_model_saved_images', game_inputs_sent=False,
            live_building_entry_verified=False, world_route_verified=False,
            elapsed_s=time.monotonic()-began, cases=results), indent=2))
    if len(results) != len(fixtures) or not all(r['passed'] for r in results):
        raise ValueError('surface_replay_failed')


if __name__ == '__main__':
    main()
