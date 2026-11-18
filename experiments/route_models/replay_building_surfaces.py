"""Replay saved captures through the LOCAL surface gate; no game/window access."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.control.building_access import ExteriorPoint
from bot.control.segmented_detour import local_worker,close_local_worker
from bot.perception.scene_scale import SCENE_BOUNDS


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seed-memory',type=Path)
    args=p.parse_args()
    events=[json.loads(line) for line in (args.run/'events.jsonl').read_text().splitlines()]
    # Use all matching historical evidence, never manually supplied map points.
    names={'20261117_193420_camera_after_interact.png':'exterior',
           '20261117_193442_camera_before_ranged.png':'structure',
           '20261117_193520_camera_after_ranged.png':'structure'}
    results=[];points=[];began=time.monotonic()
    for row in events:
        if row['kind']!='quest_camera_evidence':continue
        source=Path(row['data']['image'])
        if source.name not in names:continue
        observations=[x for x in events if x['kind']=='quest_observation'
            and abs(x['ts']-row['ts'])<=1.3
            and x['data']['navigation']['position']==row['data']['position']]
        if not observations:raise ValueError('saved_image_native_pose_missing')
        obs=min(observations,key=lambda x:abs(x['ts']-row['ts']))['data']['navigation']
        if obs['flags'] & (8|16|32|64|128):raise ValueError('saved_surface_not_stationary_ground')
        rgb=cv2.cvtColor(cv2.imread(str(source)),cv2.COLOR_BGR2RGB)
        h,w=rgb.shape[:2];l,t,r,b=SCENE_BOUNDS
        crop=rgb[round(t*h):round(b*h),round(l*w):round(r*w)]
        folder=args.output/source.stem;folder.mkdir(parents=True,exist_ok=True)
        path=folder/'image.png';cv2.imwrite(str(path),cv2.cvtColor(crop,cv2.COLOR_RGB2BGR))
        job=dict(mode='building_scene',frame_id=source.stem,
            models=str(ROOT/'data/segmentation/models.json'),
            checkpoint=str(ROOT/'data/segmentation/sam2.1_hiera_large.pt'),device='cpu')
        result=local_worker().inspect([path],target=job,timeout=30)[0]
        results.append(dict(source=str(source),expected=names[source.name],result=result,
                            native_event=obs['sequence']))
        if result['surface']!=names[source.name]:raise ValueError('surface_regression:'+source.name)
        if result['doorways']:raise ValueError('false_doorway_candidate:'+source.name)
        if result['surface']=='exterior':
            points.append(ExteriorPoint(obs['map_id'],tuple(obs['world_size']),tuple(obs['position']),row['ts'],
                str(path.resolve()),hashlib.sha256(path.read_bytes()).hexdigest()))
    close_local_worker()
    if len(results)!=3:raise ValueError('missing_historical_cases')
    report=dict(kind='saved_images_local_model',game_inputs_sent=False,wall_s=time.monotonic()-began,
                results=results,live_building_entry_verified=False)
    (args.output/'replay.json').write_text(json.dumps(report,indent=2))
    if args.seed_memory:
        args.seed_memory.parent.mkdir(parents=True,exist_ok=True)
        args.seed_memory.write_text(json.dumps(dict(schema=1,points=[asdict(p) for p in points]),indent=2))
    print(json.dumps(dict(cases=len(results),passed=True,wall_s=report['wall_s'],ground_points=len(points))))


if __name__=='__main__':main()
