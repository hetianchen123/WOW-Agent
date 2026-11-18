"""Compare local semantic/detection views at automatically detected conflicts.

Read-only offline inference; audit labels never choose a crop or model prompt.
These diagnostics do not authorize navigation or modify runtime masks.
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
from bot.perception.building_scene import BUILDING_PROMPT


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    model=LocalSegmenter(json.loads((ROOT/'data/segmentation/models.json').read_text()),
        ROOT/'data/segmentation/sam2.1_hiera_large.pt',variant='large',sam_device='cpu')
    names=model.semantic.config.id2label
    records=[]
    for case in json.loads(args.manifest.read_text())['cases']:
        path=ROOT/case['file'];assert hashlib.sha256(path.read_bytes()).hexdigest()==case['sha256']
        rgb=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
        h,w=rgb.shape[:2];scale=min(1.,1024/max(h,w))
        rgb=cv2.resize(rgb,(round(w*scale),round(h*scale)),interpolation=cv2.INTER_AREA)
        excluded=np.zeros(rgb.shape[:2],bool)
        for box in case.get('ui_boxes',[]):
            l,t,r,b=[round(v*scale) for v in box];excluded[t:b,l:r]=True
        observed=model.inspect(rgb,prompt=BUILDING_PROMPT,optimize=True,excluded=excluded)
        folder=args.output/case['name'];folder.mkdir(exist_ok=True)
        arrays=dict(labels=observed.semantic_labels,confidence=observed.semantic_confidence,
            ground=observed.ground,solid=observed.obstacles,unknown=observed.unknown,
            water=observed.water,forbidden=observed.forbidden,ground_evidence=observed.ground_evidence)
        rows=[];crops=[]
        for i,item in enumerate(observed.instances):
            arrays[f'mask_{i}']=item['mask'];rows.append({k:v for k,v in item.items() if k!='mask'})
            if (item['label'] not in {'rug','carpet','floor','floor road','closed door'}
                    or item['mask_score']<.9 or item['mask'].sum()<.005*rgb.shape[0]*rgb.shape[1]
                    or len(crops)>=5):continue
            l,t,r,b=np.rint(expanded_box(item['box'],rgb.shape[:2],.2)).astype(int)
            crop=Image.fromarray(rgb[t:b,l:r]);began=time.monotonic()
            with model.torch.inference_mode():
                labels,confidence,margin=model._semantics(crop,(b-t,r-l))
                inp=model.detector_processor(images=crop,text='rug. carpet. wooden floor. stone floor. water. pool. bookcase. closed door. table.',return_tensors='pt')
                detections=model.detector_processor.post_process_grounded_object_detection(
                    model.detector(**inp),inp.input_ids,threshold=.25,text_threshold=.25,target_sizes=[(b-t,r-l)])[0]
            arrays[f'crop_{i}_labels']=labels;arrays[f'crop_{i}_confidence']=confidence
            arrays[f'crop_{i}_margin']=margin
            local_mask=item['mask'][t:b,l:r]
            shares=sorted([(str(names[int(k)]),int(((labels==k)&local_mask).sum()))
                           for k in np.unique(labels[local_mask])],key=lambda x:x[1],reverse=True)[:6]
            result=dict(instance=i,label=item['label'],box=[int(v) for v in (l,t,r,b)],
                top_semantics=shares,elapsed_s=time.monotonic()-began,
                detections=[dict(label=label,score=float(score),box=[float(v) for v in box])
                    for label,score,box in zip(detections['text_labels'],detections['scores'],detections['boxes'])])
            probes=[]
            for p in case['probes']:
                x,y=[round(v*scale) for v in p['point']]
                if l<=x<r and t<=y<b and local_mask[y-t,x-l]:
                    probes.append(dict(id=p['id'],before=str(names[int(observed.semantic_labels[y,x])]),
                        after=str(names[int(labels[y-t,x-l])]),confidence=float(confidence[y-t,x-l])))
            result['audit_probes']=probes;crops.append(result)
            print(json.dumps(dict(name=case['name'],label=item['label'],probes=probes,top_semantics=shares,
                detections=[(d['label'],round(d['score'],3)) for d in result['detections']])),flush=True)
        np.savez_compressed(folder/'masks.npz',**arrays)
        record=dict(name=case['name'],image=str(path),source_sha256=case['sha256'],scale=scale,
            names=names,instances=rows,crops=crops,game_inputs_sent=False)
        (folder/'audit.json').write_text(json.dumps(record,indent=2));records.append(record)
    (args.output/'report.json').write_text(json.dumps(dict(cases=records,game_inputs_sent=False,
        runtime_masks_modified=False),indent=2))


if __name__=='__main__':main()
