"""Replay a dead-end return and preserve its simulated actions/receipts.

The model and game feedback are simulated. Fresh-frame geometric planning is
checked separately against synthetic floor, furniture, and doorway masks.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from test_building_return import simulate_branch_return, return_scene
from bot.decision.building_navigation import building_navigation_region
from bot.control.projected_route import plan_segmented_approach


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    with pytest.MonkeyPatch.context() as patch:
        report=simulate_branch_return(args.output,patch)
    with pytest.MonkeyPatch.context() as patch:
        frame,projection,_,observed,description,memory=return_scene(patch)
        region,goal=building_navigation_region(projection,observed,None,(.9,.9),description,
            searching=True,passages=memory.incident(),navigation_context=memory.snapshot())
        plan=plan_segmented_approach(projection,observed,region,frame,now=11,
            frame_id=frame.frame_id,image_sha256=frame.image_sha256)
        report['geometry']=dict(source='synthetic_geometry',goal=goal,plan=plan,
            model_recognition_verified=False,calibration_source='synthetic_regression')
    (args.output/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in {'events','passages','geometry'}}))


if __name__=='__main__':main()
