"""Compare observed connectivity around doors. No world scale/player inference.

The starts are explicitly developer-selected floor points on public images.
Pixel clearance is a sensitivity study, not an estimated character footprint.
"""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.perception.building_layout import doorway_floor_box
from bot.decision.image_navigation import ImageNavigator
from bot.decision.building_entry import doorway_approach_distance


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--maps',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    cases=[('blue-carpet-corridor',(800,695)),('tavern-interior-door',(500,812))]
    sources=json.loads((ROOT/'data/terrain/building-conflicts-20261118/evaluation.json').read_text())['cases']
    report=[]
    for name,start_original in cases:
        row=json.loads((args.maps/(name+'.json')).read_text())
        source=next(c for c in sources if c['name']==name)
        start=tuple(round(v*row['scale']) for v in start_original)
        with np.load(args.maps/(name+'-masks.npz')) as data:
            ground=data['ground']&~data['water']&~data['forbidden'];solid=data['obstacles']
        h,w=ground.shape
        goal=np.zeros_like(ground)
        empty=np.zeros_like(ground)
        score=doorway_approach_distance(SimpleNamespace(ground=ground,obstacles=solid,water=empty,forbidden=empty),
                                       row['scene']['doorways'],crossing=True)
        boxes=[]
        for door in row['scene']['doorways']:
            l,t,r,b=doorway_floor_box(door);boxes.append([l,t,r,b]);goal[max(0,t):min(h,b),max(0,l):min(w,r)]=True
        raw=cv2.imread(source['file']);base=cv2.resize(raw,(w,h),interpolation=cv2.INTER_AREA)
        comparisons=[]
        for radius in [0,4,12]:
            nav=ImageNavigator.from_masks(name,ground,solid,~ground,source='public_image_diagnostic',
                                         body_radius_px=radius,uncertainty_px=0)
            _,components=cv2.connectedComponents(nav.clear.astype(np.uint8),connectivity=4)
            x,y=start;reachable=(components==components[y,x])&nav.clear if nav.clear[y,x] else np.zeros_like(ground)
            target=reachable&goal
            if target.any():status='connected_in_image';candidates=target
            else:status='needs_new_observation';candidates=reachable&(score<score[y,x]-8)
            points=[]
            if candidates.any():
                target_y,target_x=np.unravel_index(np.argmin(np.where(candidates,score,np.inf)),score.shape)
                points=nav.plan(start,(int(target_x),int(target_y))).points
            comparison=dict(clearance_px=radius,start_clear=bool(nav.clear[y,x]),status=status,
                            interior_reachable_pixels=int(target.sum()),image_points=points,executable=False)
            comparisons.append(comparison)
            if radius==12:
                canvas=base.copy()
                canvas[solid]=(.65*canvas[solid]+.35*np.array([30,30,240])).astype(np.uint8)
                canvas[ground]=(.8*canvas[ground]+.2*np.array([30,220,30])).astype(np.uint8)
                for connector in row['scene'].get('layout',{}).get('connectors',[]):
                    boundary=np.asarray(connector.get('boundary',[]),np.int32)
                    if boundary.ndim==2 and len(boundary)>=3:
                        cv2.polylines(canvas,[boundary],True,(220,50,220),2)
                for l,t,r,b in boxes:cv2.rectangle(canvas,(l,t),(r,b),(0,255,255),2)
                for a,b in zip(points,points[1:]):cv2.arrowedLine(canvas,a,b,(0,160,255),2,tipLength=.08)
                cv2.circle(canvas,start,7,(255,255,255),2)
                cv2.putText(canvas,'Developer start / pixel route only',(15,25),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,255,255),2)
                cv2.putText(canvas,status,(15,50),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,200,255),2)
                cv2.putText(canvas,'Purple: unmeasured step candidates',(15,75),cv2.FONT_HERSHEY_SIMPLEX,.5,(220,80,220),1)
                cv2.imwrite(str(args.output/(name+'.png')),canvas)
        report.append(dict(name=name,start_source='developer floor point, not a detected avatar',
                           metric_route_verified=False,comparisons=comparisons,
                           connector_candidates=row['scene'].get('layout',{}).get('connectors',[])))
    (args.output/'report.json').write_text(json.dumps(dict(cases=report,game_inputs_sent=False,
        scope='image connectivity and pixel-clearance sensitivity, not calibrated movement'),indent=2))


if __name__=='__main__':main()
