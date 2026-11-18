"""Saved-image local inference and entry preflight. No game input/window access."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.perception.segmentation import LocalSegmenter
from bot.perception.building_scene import (BUILDING_PROMPT,describe_building_scene,
    building_ui_exclusion,building_ground_measurement)
from bot.perception.automatic_ground import AutomaticGround
from bot.perception.ground_projection import ProjectionFrame
from bot.perception.building_layout import flat_navigation_masks
from experiments.route_models.segmentation_worker import building_job


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run',type=Path,help='Replay the original building surface exposures in this run')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    model=LocalSegmenter(json.loads((ROOT/'data/segmentation/models.json').read_text()),
        ROOT/'data/segmentation/sam2.1_hiera_large.pt',variant='large',sam_device='cpu')
    terrain=ROOT/'data/terrain'
    cases=[('entry',terrain/'segmented-5983fa3a3f52450696c274191a1785b6/image.png',2),
           ('initial',terrain/'building-352dddadb1b548a387fd99cb08ae4e56/image.png',2),
           ('overhead',terrain/'building-534ee8ef2c1747f3910841e4b53e5c68/image.png',4),
           ('latest-entry',terrain/'segmented-63362cb015dd449fbd88878b8cb8627b/image.png',4)]
    cases += [(name,terrain/'building-access-20261117'/name/'scene.npz',2) for name in
        ['20261117_193420_camera_after_interact','20261117_193442_camera_before_ranged',
         '20261117_193520_camera_after_ranged']]
    saved={}
    if args.run:
        from experiments.route_models.replay_landing_handoff import saved_cases
        saved={str(Path(c['image'])):c for c in saved_cases(args.run)}
        cases=[(f'surface-{i}',Path(path),c['nav']['camera']['view']) for i,(path,c) in enumerate(saved.items())]
    results=[];began=time.monotonic()
    for name,path,view in cases:
        if path.suffix=='.npz':
            with np.load(path) as archive:rgb=archive['rgb']
        else:rgb=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
        observed=model.inspect(rgb,prompt=BUILDING_PROMPT,optimize=True,excluded=building_ui_exclusion(rgb.shape[:2]),review_surfaces=True,review_portals=True)
        scene=describe_building_scene(observed,model.semantic.config.id2label,camera_view=view)
        observed=flat_navigation_masks(observed,model.semantic.config.id2label)
        scene['ground_measurement']=building_ground_measurement(rgb,observed)
        folder=args.output/name;folder.mkdir(exist_ok=True)
        arrays=dict(rgb=rgb,ground=observed.ground,obstacles=observed.obstacles,
            occluded=observed.occluded,water=observed.water,forbidden=observed.forbidden,
            labels=observed.semantic_labels,confidence=observed.semantic_confidence,
            raw_labels=observed.raw_semantic_labels,raw_confidence=observed.raw_semantic_confidence,
            review_sources=observed.review_sources if observed.review_sources is not None else np.zeros(rgb.shape[:2],np.uint8))
        instances=[]
        for i,item in enumerate(observed.instances):
            arrays['mask_'+str(i)]=item['mask']
            instances.append({k:v for k,v in item.items() if k!='mask'})
        np.savez_compressed(folder/'observed.npz',**arrays)
        (folder/'observation.json').write_text(json.dumps(dict(source=str(path),
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),names=model.semantic.config.id2label,
            instances=instances,camera_view=view,scene=scene,
            saved_native=saved.get(str(path),{}).get('nav')),indent=2))
        mask=cv2.erode((observed.ground&(observed.semantic_confidence>=.9)).astype(np.uint8)*255,
                       np.ones((7,7),np.uint8))
        points,_=cv2.ORB_create(nfeatures=4000,fastThreshold=12,nlevels=1).detectAndCompute(
            cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY),mask)
        row=dict(name=name,surface=scene['surface'],doorways=scene['doorways'],
            layout=scene['layout'],
            usable_ground_pixels=int((observed.ground&(observed.semantic_confidence>=.9)).sum()),
            ground_features=scene['ground_measurement']['texture_features'],
            high_contrast_features=len(points),measurement=scene['ground_measurement'])
        if name=='entry':
            job=json.loads(path.with_name('request.json').read_text());values=job['frame']
            for k in ('size','world_size','player_position','camera_signature'):values[k]=tuple(values[k])
            # Only exposure time is refreshed for offline algorithm entry. Saved
            # pose is unchanged; no subsequent movement/metric fit is invented.
            frame=replace(ProjectionFrame(**values),captured_at=time.monotonic())
            job['building_scene']=scene
            result=building_job(AutomaticGround(camera_views=(2,4,5)),frame,rgb,observed,job)
            row['result']=result
            assert scene['doorways'] and result['kind'] in {'calibration_orbit','building_reobserve'}
            if result['kind']=='building_reobserve':assert result['reason']=='auto_ground_texture_insufficient'
            assert result['executable'] is False
        if str(path) in saved:
            from experiments.route_models.replay_landing_handoff import native
            nav=native(saved[str(path)]['nav'])
            # Only exercise the first observation: this log did not preserve a
            # synchronized heading or subsequent movement. No metric fit or
            # movement is possible from this single saved image.
            frame=ProjectionFrame(name,hashlib.sha256(path.read_bytes()).hexdigest(),
                (rgb.shape[1],rgb.shape[0]),nav.map_id,'automatic-local-plane',nav.world_size,
                nav.position,None,nav.height,0.,(view,nav.camera.zoom,nav.camera_epoch),time.monotonic())
            job=dict(building_scene=scene,quest_id=nav.quest_id,phase=nav.phase.name,
                     objective_index=nav.objective_index,goal_mode='building_entry')
            result=building_job(AutomaticGround(camera_views=(2,4,5)),frame,rgb,observed,job)
            row['result']=result
            row['scaffolding']='time refreshed, heading placeholder for initial stationary preflight only'
            assert result['kind'] in {'calibration_orbit','building_reobserve'}
            assert result['executable'] is False and 'next_segment_world' not in result
        if '193442' in name or '193520' in name:
            assert scene['surface']=='structure' and not scene['doorways']
        if '193420' in name:assert scene['surface']=='exterior' and not scene['doorways']
        results.append(row)
        print(json.dumps({k:v for k,v in row.items() if k not in {'doorways','result'}}),flush=True)
    (args.output/'replay.json').write_text(json.dumps(dict(results=results,elapsed_s=time.monotonic()-began,
        game_inputs_sent=False,metric_route_verified=False,live_building_entry_verified=False),indent=2))


if __name__=='__main__':main()
