"""Offline diagnostics on automatically selected door regions; no game input.

Probe annotations are read only after inference. Crop boxes come from current
door candidates, never evaluation points. Outputs cannot authorize movement.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.perception.segmentation import LocalSegmenter,expanded_box
from bot.perception.building_scene import BUILDING_PROMPT,describe_building_scene

DETAIL_PROMPT='floor. rug. carpet. wall. wooden door. closed door. open doorway. bed. sofa. table. stairs. railing. bookcase. barrel.'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    model=LocalSegmenter(json.loads((ROOT/'data/segmentation/models.json').read_text()),
        ROOT/'data/segmentation/sam2.1_hiera_large.pt',variant='large',sam_device='cpu')
    reports=[]
    for case in json.loads(args.manifest.read_text())['cases']:
        path=ROOT/case['file'];assert hashlib.sha256(path.read_bytes()).hexdigest()==case['sha256']
        rgb=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
        h,w=rgb.shape[:2];scale=min(1.,1024/max(h,w))
        rgb=cv2.resize(rgb,(round(w*scale),round(h*scale)),interpolation=cv2.INTER_AREA)
        excluded=np.zeros(rgb.shape[:2],bool)
        for box in case.get('ui_boxes',[]):
            l,t,r,b=[round(v*scale) for v in box];excluded[t:b,l:r]=True
        observed=model.inspect(rgb,prompt=BUILDING_PROMPT,optimize=True,excluded=excluded,review_surfaces=True)
        names=model.semantic.config.id2label
        scene=describe_building_scene(observed,names)
        folder=args.output/case['name'];folder.mkdir(exist_ok=True)
        arrays=dict(labels=observed.semantic_labels,confidence=observed.semantic_confidence,
            ground=observed.ground,solid=observed.obstacles,water=observed.water,
            forbidden=observed.forbidden,raw_labels=observed.raw_semantic_labels,
            raw_confidence=observed.raw_semantic_confidence)
        instances=[]
        for i,item in enumerate(observed.instances):
            arrays[f'instance_{i}']=item['mask'];instances.append({k:v for k,v in item.items() if k!='mask'})
        crops=[]
        for i,door in enumerate(scene['doorways'][:2]):
            l,t,r,b=np.rint(expanded_box(door['box'],rgb.shape[:2],.2)).astype(int)
            local=rgb[t:b,l:r];image=Image.fromarray(local);began=time.monotonic()
            with model.torch.inference_mode():
                for tag,image_view in [('rgb',image),('gray',image.convert('L').convert('RGB'))]:
                    labels,conf,gap=model._semantics(image_view,local.shape[:2])
                    for key,value in [('labels',labels),('confidence',conf),('margin',gap)]:arrays[f'crop_{i}_{tag}_{key}']=value
                inp=model.detector_processor(images=image,text=DETAIL_PROMPT,return_tensors='pt')
                found=model.detector_processor.post_process_grounded_object_detection(
                    model.detector(**inp),inp.input_ids,threshold=.25,text_threshold=.25,
                    target_sizes=[local.shape[:2]])[0]
                model.sam.set_image(local)
                detected=[]
                for j,(label,score,box) in enumerate(zip(found['text_labels'],found['scores'],found['boxes'])):
                    masks,scores,_=model.sam.predict(box=box.numpy(),multimask_output=True)
                    best=int(np.argmax(scores));arrays[f'crop_{i}_instance_{j}']=masks[best].astype(bool)
                    detected.append(dict(label=label,score=float(score),box=box.tolist(),mask_score=float(scores[best])))
            probes=[]
            for p in case['probes']:
                x,y=[round(v*scale) for v in p['point']]
                if not (l<=x<r and t<=y<b):continue
                contributors=[v['label'] for v in observed.instances if v['mask'][y,x]]
                result=dict(id=p['id'],before=[names[int(observed.semantic_labels[y,x])],float(observed.semantic_confidence[y,x])],
                    original_instances=contributors)
                for tag in ['rgb','gray']:
                    result[tag]=[names[int(arrays[f'crop_{i}_{tag}_labels'][y-t,x-l])],
                                 float(arrays[f'crop_{i}_{tag}_confidence'][y-t,x-l])]
                result['local_instances']=[v for j,v in enumerate(detected) if arrays[f'crop_{i}_instance_{j}'][y-t,x-l]]
                probes.append(result)
            crops.append(dict(box=[int(v) for v in (l,t,r,b)],source_door=door,instances=detected,
                              elapsed_s=time.monotonic()-began,probes=probes))
            cv2.imwrite(str(folder/f'crop-{i}.png'),cv2.cvtColor(local,cv2.COLOR_RGB2BGR))
        np.savez_compressed(folder/'observed.npz',**arrays)
        record=dict(name=case['name'],source=case,scale=scale,names=names,scene=scene,
                    instances=instances,crops=crops,game_inputs_sent=False)
        (folder/'report.json').write_text(json.dumps(record,indent=2));reports.append(record)
        print(json.dumps(dict(name=case['name'],doors=len(scene['doorways']),crops=crops)),flush=True)
    (args.output/'report.json').write_text(json.dumps(dict(cases=reports,game_inputs_sent=False,
                runtime_masks_modified=False),indent=2))


if __name__=='__main__':main()
