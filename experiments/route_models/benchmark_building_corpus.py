"""Evaluate authored image probes against LOCAL models, without world geometry.

Probe labels/crops are evaluation data only. They never enter the model prompt,
fused map, navigation masks or task runtime. No game controls or network calls.
"""
import argparse
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
from bot.perception.building_scene import BUILDING_PROMPT,describe_building_scene
from bot.perception.building_layout import flat_navigation_masks,stair_evidence
from experiments.route_models.segmentation_worker import review_artifacts


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--surface-review',action='store_true')
    parser.add_argument('--portal-review',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    manifest=json.loads(args.manifest.read_text())
    model=LocalSegmenter(json.loads((ROOT/'data/segmentation/models.json').read_text()),
        ROOT/'data/segmentation/sam2.1_hiera_large.pt',variant='large',sam_device='cpu')
    report=[];started=time.monotonic()
    for case in manifest['cases']:
        path=ROOT/case['file'];digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=case['sha256']:raise ValueError('corpus_source_hash_changed')
        rgb=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
        height,width=rgb.shape[:2];scale=min(1.,1024/max(width,height))
        rgb=cv2.resize(rgb,(round(width*scale),round(height*scale)),interpolation=cv2.INTER_AREA)
        excluded=np.zeros(rgb.shape[:2],bool)
        for l,t,r,b in case.get('ui_boxes',[]):
            l,t,r,b=[round(x*scale) for x in (l,t,r,b)];excluded[t:b,l:r]=True
        kwargs=dict(review_surfaces=True) if args.surface_review else {}
        if args.portal_review:kwargs['review_portals']=True
        observed=model.inspect(rgb,prompt=BUILDING_PROMPT,optimize=True,excluded=excluded,**kwargs)
        scene=describe_building_scene(observed,model.semantic.config.id2label)
        adapted=flat_navigation_masks(observed,model.semantic.config.id2label)
        stairs=stair_evidence(observed,model.semantic.config.id2label)
        canvas=rgb.copy()
        for mask,color in [(adapted.ground,(20,220,50)),(adapted.obstacles,(235,40,40)),
                           (stairs,(240,160,20)),(excluded,(80,80,180))]:
            canvas[mask]=(.65*canvas[mask]+.35*np.array(color)).astype(np.uint8)
        probes=[]
        for p in case['probes']:
            x,y=(round(v*scale) for v in p['point'])
            label=model.semantic.config.id2label[int(observed.semantic_labels[y,x])]
            result=dict(p,semantic=label,semantic_confidence=float(observed.semantic_confidence[y,x]),
                ground=bool(adapted.ground[y,x]),obstacle=bool(adapted.obstacles[y,x]),
                stairs=bool(stairs[y,x]),water=bool(adapted.water[y,x]),occluded=bool(adapted.occluded[y,x]))
            result['passed']=result[dict(ground='ground',stairs='stairs',obstacle='obstacle',water='water')[p['expected']]]
            probes.append(result)
            cv2.circle(canvas,(x,y),6,(0,255,0) if result['passed'] else (255,0,255),2)
            cv2.putText(canvas,p['id'],(x+7,y-5),cv2.FONT_HERSHEY_SIMPLEX,.38,(255,255,255),1)
        doors=[]
        for door in scene['doorways']:
            l,t,r,b=map(round,door['box']);cv2.rectangle(canvas,(l,t),(r,b),(255,255,0),2)
            doors.append(door)
        cv2.imwrite(str(args.output/(case['name']+'-overlay.png')),cv2.cvtColor(canvas,cv2.COLOR_RGB2BGR))
        np.savez_compressed(args.output/(case['name']+'-masks.npz'),
            labels=observed.semantic_labels,confidence=observed.semantic_confidence,
            ground=adapted.ground,obstacles=adapted.obstacles,forbidden=adapted.forbidden,
            occluded=adapted.occluded,water=adapted.water,**review_artifacts(observed))
        row=dict(name=case['name'],source=case['source_page'],sha256=digest,scale=scale,
            scene=scene,probes=probes,timings=observed.timings,diagnostics=observed.diagnostics,
            detections=[{k:v for k,v in item.items() if k!='mask'} for item in observed.instances],
            native_telemetry_available=False,metric_route_verified=False,executable=False)
        report.append(row)
        (args.output/(case['name']+'.json')).write_text(json.dumps(row,indent=2))
        print(json.dumps(dict(name=case['name'],surface=scene['surface'],doors=len(doors),
            probes_passed=sum(p['passed'] for p in probes),probes=len(probes))),flush=True)
    (args.output/'report.json').write_text(json.dumps(dict(cases=report,elapsed_s=time.monotonic()-started,
        game_inputs_sent=False,scope='sparse visual probes, not dense accuracy or live navigation'),indent=2))


if __name__=='__main__':main()
