"""Repeatable building geometry benchmark and route diagrams; no game input.

Uses the same synthetic layouts as the behavior tests. These diagrams test
planning, not whether SAM recognizes a game screenshot.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_building_navigation import scene,TARGETS
from test_ground_projection import fixture
from test_building_connectors import stair_scene
from bot.decision.building_navigation import building_navigation_region
from bot.perception.ground_projection import GroundProjection
from bot.control.projected_route import plan_segmented_approach
from bot.decision.image_navigation import ImageNavigator


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    rows=[];panels=[]
    cases=[(kind,scene(kind)) for kind in ('gate','room','corridor','indoor_door','courtyard','stairs','attic')]
    cases += [(name,stair_scene(box)) for name,box in [
        ('steps_above_anchor',(360,190,445,250)),('steps_below_anchor',(360,350,445,410)),
        ('steps_left_of_anchor',(270,265,330,335))]]
    for kind,(observed,description) in cases:
        frame,training,validation,_=fixture()
        projection=GroundProjection(frame,training,validation)
        region,goal=building_navigation_region(projection,observed,None,(.1,.1),description)
        plan=plan_segmented_approach(projection,observed,region,frame,now=11,
            frame_id=frame.frame_id,image_sha256=frame.image_sha256)
        rgb=np.full((600,800,3),24,np.uint8)
        rgb[observed.ground]=(89,126,86)
        rgb[observed.obstacles]=(126,126,138)
        rgb[observed.forbidden]=(210,130,60)
        rgb[observed.water]=(50,95,200)
        full_route=None
        if kind in TARGETS:
            geometry=ImageNavigator.from_masks(kind,observed.ground,observed.obstacles,
                ~observed.ground|observed.forbidden|observed.water,source='synthetic_regression')
            full_route=geometry.plan((400,300),TARGETS[kind])
            geometry.validate(full_route.points)
            for a,b in zip(full_route.points,full_route.points[1:]):
                cv2.line(rgb,tuple(round(v) for v in a),tuple(round(v) for v in b),(250,190,50),3)
        points=[tuple(round(v) for v in p) for p in plan['approach']['route']['points']]
        for a,b in zip(points,points[1:]):
            cv2.line(rgb,a,b,(40,240,240),3)
        cv2.circle(rgb,points[0],7,(255,255,255),-1)
        cv2.circle(rgb,points[-1],7,(250,210,50),-1)
        cv2.putText(rgb,kind+' | synthetic geometry',(20,35),cv2.FONT_HERSHEY_SIMPLEX,.7,(240,240,240),2)
        cv2.putText(rgb,'Not model recognition or live validation',(20,570),cv2.FONT_HERSHEY_SIMPLEX,.6,(230,230,230),1)
        path=args.output/(kind+'.png')
        cv2.imwrite(str(path),cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
        panels.append(cv2.resize(rgb,(400,300)))
        row=dict(scenario=kind,layout=description['layout'],goal=goal,plan=plan,
            full_local_route=[list(p) for p in full_route.points] if full_route else None,
            requires_separate_stair_surface=kind=='stairs' or kind.startswith('steps_'),
            figure=str(path),figure_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            source='synthetic_geometry',model_recognition_verified=False,live_passed=False)
        (args.output/(kind+'.json')).write_text(json.dumps(row,indent=2))
        rows.append(row)
    if len(panels)%2:panels.append(np.zeros_like(panels[0]))
    grid=np.concatenate([np.concatenate(panels[i:i+2],axis=1) for i in range(0,len(panels),2)])
    cv2.imwrite(str(args.output/'overview.png'),cv2.cvtColor(grid,cv2.COLOR_RGB2BGR))
    (args.output/'summary.json').write_text(json.dumps(dict(scenarios=rows,game_inputs_sent=False,
        calibration_source='synthetic_regression',real_world_route_verified=False),indent=2))
    print(json.dumps(dict(scenarios=len(rows),output=str(args.output),game_inputs_sent=False)))


if __name__=='__main__':main()
