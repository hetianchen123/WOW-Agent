"""Offline SAM 2 comparison. Review labels score results, never select model masks.

Run in the isolated data/segmentation/.venv environment. No game input or API.
Model-box cases use the uncorrected boxes from saved DeepSeek observations.
The explicitly labelled review-box control is not autonomous perception.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import cv2
import numpy as np
import torch
from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor


def mask_polygon(poly, size):
    mask=np.zeros(size, np.uint8)
    cv2.fillPoly(mask,[np.rint(poly).astype(np.int32)],1)
    return mask.astype(bool)


def metrics(mask, reference):
    intersection=int((mask&reference).sum())
    return {'iou':intersection/max(1,int((mask|reference).sum())),
            'solid_recall':intersection/max(1,int(reference.sum())),
            'precision':intersection/max(1,int(mask.sum()))}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--replays',type=Path,required=True)
    parser.add_argument('--image',type=Path,required=True,help='Scene crop used by model')
    parser.add_argument('--reference',type=Path,default=Path('tests/fixtures/overhead-scene-map.json'))
    parser.add_argument('--checkpoint',type=Path,default=Path('data/segmentation/sam2.1_hiera_tiny.pt'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--crop-context',action='store_true')
    parser.add_argument('--single-mask',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    image=cv2.cvtColor(cv2.imread(str(args.image)),cv2.COLOR_BGR2RGB)
    h,w=image.shape[:2]
    reference=json.loads(args.reference.read_text())
    assert reference['size']==[w,h]
    solid=mask_polygon(reference['obstacles'][0],(h,w))
    cases=[]
    for row in json.loads(args.replays.read_text()):
        report=json.loads((Path(row['evidence'])/'report.json').read_text())
        stage=next((s for s in report['stages'] if s['name']=='barrier'),None)
        if stage is None:continue
        raw=json.loads(stage['raw_output'])
        poly=raw.get('polygon')
        if not isinstance(poly,list) or not all(isinstance(p,list) and len(p)==2 for p in poly):continue
        x,y,_,_=stage['facts']['crop_scene_pixels']
        poly=np.asarray(poly,np.float32)/3+[x,y]
        cases.append({'name':'model-box-'+str(row['attempt']),'source':'uncorrected_deepseek_outline_box',
                      'box':[*poly.min(axis=0),*poly.max(axis=0)],'model_polygon':poly.tolist(),
                      'evidence':row['evidence']})
    poly=np.asarray(reference['obstacles'][0])
    cases.append({'name':'review-box-control','source':'manual_review_box_NOT_automatic',
                  'box':[*poly.min(axis=0),*poly.max(axis=0)]})
    torch.set_num_threads(4)
    began=time.monotonic()
    model=build_sam2('configs/sam2.1/sam2.1_hiera_t.yaml',str(args.checkpoint),
                     device='cpu',apply_postprocessing=False)
    predictor=SAM2ImagePredictor(model)
    load_s=time.monotonic()-began
    with torch.inference_mode():
        began=time.monotonic();predictor.set_image(image);embed_s=time.monotonic()-began
        results=[]
        for case in cases:
            crop=None;crop_embed_s=0
            prompt_box=np.array(case['box'],dtype=np.float32)
            if args.crop_context:
                # Derive local resolution from the model proposal, never review labels.
                x1,y1,x2,y2=prompt_box
                pad=.4*max(x2-x1,y2-y1)
                left,top=max(0,int(x1-pad)),max(0,int(y1-pad))
                right,bottom=min(w,int(x2+pad)+1),min(h,int(y2+pad)+1)
                crop=[left,top,right,bottom]
                began=time.monotonic();predictor.set_image(image[top:bottom,left:right])
                crop_embed_s=time.monotonic()-began
                prompt_box-=np.array([left,top,left,top])
            began=time.monotonic()
            masks,scores,_=predictor.predict(box=prompt_box,
                                           multimask_output=not args.single_mask)
            predict_s=time.monotonic()-began
            if crop:
                left,top,right,bottom=crop
                expanded=np.zeros((len(masks),h,w),bool)
                expanded[:,top:bottom,left:right]=masks
                masks=expanded
            chosen=int(np.argmax(scores))  # Never choose by manual reference score.
            metrics_all=[]
            for i,mask in enumerate(masks):
                binary=mask.astype(np.uint8)
                contours=cv2.findContours(binary,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[0]
                closed=np.zeros_like(binary);cv2.drawContours(closed,contours,-1,1,cv2.FILLED)
                overlay=image.copy()
                overlay[closed>0]=(overlay[closed>0]*.55+np.array([220,40,40])*.45).astype(np.uint8)
                cv2.polylines(overlay,[np.array(reference['obstacles'][0],np.int32)],True,(50,255,80),1)
                box=np.rint(case['box']).astype(int)
                cv2.rectangle(overlay,tuple(box[:2]),tuple(box[2:]),(255,220,0),1)
                title=f"{case['name']} mask {i} / {'SELECTED' if i==chosen else 'alternative'} / IMAGE ONLY"
                cv2.rectangle(overlay,(0,0),(w,30),(24,24,24),-1)
                cv2.putText(overlay,title,(8,21),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1)
                prefix=args.output/f"{case['name']}-{i}"
                cv2.imwrite(str(prefix)+'.png',cv2.cvtColor(overlay,cv2.COLOR_RGB2BGR))
                cv2.imwrite(str(prefix)+'-mask.png',closed*255)
                metrics_all.append({'index':i,'model_score':float(scores[i]),
                    'raw':metrics(binary.astype(bool),solid),'outer_silhouette':metrics(closed.astype(bool),solid),
                    'overlay':str(prefix)+'.png','mask':str(prefix)+'-mask.png'})
            result={**case,'box':list(map(float,case['box'])),'selected_by_model_score':chosen,
                    'predict_s':predict_s,'crop_embedding_s':crop_embed_s,'crop':crop,
                    'candidates':metrics_all,'executable':False}
            if 'model_polygon' in case:
                result['original_model_outline']=metrics(mask_polygon(case['model_polygon'],(h,w)),solid)
            results.append(result)
            print(json.dumps({'case':case['name'],'predict_s':predict_s,'selected':metrics_all[chosen]}),flush=True)
    report={'model':'sam2.1_hiera_tiny','device':'cpu','load_s':load_s,'image_embedding_s':embed_s,
            'checkpoint_sha256':hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
            'source_image_sha256':hashlib.sha256(args.image.read_bytes()).hexdigest(),
            'crop_context':args.crop_context,'single_mask':args.single_mask,
            'reference_used_only_for_scoring_except_explicit_control':True,
            'world_collision_verified':False,'cases':results}
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'load_s':load_s,'image_embedding_s':embed_s}),flush=True)


if __name__=='__main__':main()
