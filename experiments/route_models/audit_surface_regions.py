"""Dense evaluation of conservative material interiors; no model/control input.

Regions are post-error diagnostic annotations, not an independent test set or
collision ground truth. Images, baseline and reviewed masks stay immutable.
"""
import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,action='append',required=True)
    parser.add_argument('--reviewed',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    report=[]
    for case in json.loads(args.manifest.read_text())['cases']:
        path=Path(case['file'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=case['sha256']:
            raise ValueError('corpus_source_hash_changed')
        original=cv2.imread(str(path));h,w=original.shape[:2]
        filename=case['name']+'-masks.npz'
        baseline=next(folder/filename for folder in args.baseline if (folder/filename).is_file())
        with np.load(baseline,allow_pickle=False) as a,np.load(args.reviewed/filename,allow_pickle=False) as b:
            shape=b['ground'].shape
            if a['ground'].shape!=shape:raise ValueError('surface_audit_shape_mismatch')
            canvas=cv2.resize(original,(shape[1],shape[0]),interpolation=cv2.INTER_AREA)
            records=[]
            for region in case['regions']:
                points=np.rint(np.asarray(region['polygon'])*[shape[1]/w,shape[0]/h]).astype(np.int32)
                mask=np.zeros(shape,np.uint8);cv2.fillPoly(mask,[points],1);mask=mask.astype(bool)
                record=dict(region,region_pixels=int(mask.sum()))
                for tag,observed in [('baseline',a),('reviewed',b)]:
                    counts={k:int((mask&observed[k]).sum()) for k in ('ground','obstacles','water')}
                    record[tag]=dict(counts,expected_fraction=counts[region['expected']]/max(1,mask.sum()))
                records.append(record)
                cv2.polylines(canvas,[points],True,(0,255,255),2)
                cv2.putText(canvas,region['id'],tuple(points[0]),cv2.FONT_HERSHEY_SIMPLEX,.4,(0,255,255),1)
            row=dict(name=case['name'],sha256=case['sha256'],baseline=str(baseline),
                     reviewed=str(args.reviewed/filename),regions=records)
            if 'raw_labels' in b:
                # All original water labels, not just authored sample regions.
                water=np.isin(b['raw_labels'],[21,26,60,109])  # Fixed ADE150 ids; see model manifest.
                row['original_water_pixels_promoted_to_ground']=int((water&b['ground']).sum())
            report.append(row)
            cv2.imwrite(str(args.output/(case['name']+'-regions.png')),canvas)
    result=dict(cases=report,game_inputs_sent=False,scope='post-error visual region audit, not full-image accuracy or physical water boundaries')
    (args.output/'report.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
