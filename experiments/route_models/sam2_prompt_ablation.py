"""Offline SAM prompt ablation using saved AUTOMATIC detector boxes only.

No reference annotations are read. Outputs are evaluated separately afterwards.
"""
import argparse
import json
from pathlib import Path
import time

import cv2
import numpy as np
import torch
from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor


def run(args):
    torch.set_num_threads(4)
    report = json.loads(args.report.read_text())
    rgb = cv2.cvtColor(cv2.imread(report['image']), cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    model = SAM2ImagePredictor(build_sam2('configs/sam2.1/sam2.1_hiera_l.yaml',
        'data/segmentation/sam2.1_hiera_large.pt', device='cpu', apply_postprocessing=False))
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    with torch.inference_mode():
        model.set_image(rgb)
        for i, item in enumerate(report['instances']):
            if not any(t in item['label'] for t in ('fence', 'post', 'spike', 'rock')):
                continue
            box = np.array(item['box'])
            masks, scores, logits = model.predict(box=box, multimask_output=True)
            best = int(scores.argmax()); original = masks[best].astype(bool)
            cv2.imwrite(str(args.output/f'{i}-original.png'), original.astype('uint8')*255)
            for fraction in (.15, .3):
                pad = fraction*max(box[2]-box[0], box[3]-box[1])
                expanded = np.clip(box+[-pad,-pad,pad,pad], [0,0,0,0], [w,h,w,h])
                for mode in ('box', 'logits'):
                    began = time.monotonic()
                    candidates, quality, _ = model.predict(box=expanded, multimask_output=True,
                        mask_input=logits[best:best+1] if mode == 'logits' else None)
                    selected = int(quality.argmax()); candidate = candidates[selected].astype(bool)
                    name = f'{i}-{fraction}-{mode}.png'
                    cv2.imwrite(str(args.output/name),candidate.astype('uint8')*255)
                    records.append(dict(instance=i,label=item['label'],mode=mode,padding=fraction,
                        score=float(quality[selected]),base_score=float(scores[best]),mask_file=name,
                        retained=float((original & candidate).sum()/max(1,original.sum())),
                        growth=float(candidate.sum()/max(1,original.sum())),s=time.monotonic()-began))
    (args.output/'results.json').write_text(json.dumps(records,indent=2))
    print(json.dumps(records,indent=2))


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())
