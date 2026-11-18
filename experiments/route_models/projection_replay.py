"""Fit measured ground correspondence bundle and export candidate world waypoints.

No game input, automatic model-generated anchors or runtime authorization.
JSON bundle: frame (ProjectionFrame fields), training/validation (GroundAnchor
fields), route {frame_id, points}. World positions are map fractions, not pixels.
All anchors' pixels must belong to the SAME reference image ground plane.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bot.perception.ground_projection import GroundProjection,ProjectionFrame,GroundAnchor,ProjectionError
from bot.control.navigation import compass_from_delta


def main():
    p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    data=json.loads(args.bundle.read_text());result={'bundle':str(args.bundle),'executable':False}
    try:
        frame=ProjectionFrame(**data['frame'])
        projection=GroundProjection(frame,[GroundAnchor(**a) for a in data['training']],
                                     [GroundAnchor(**a) for a in data['validation']])
        route=SimpleNamespace(**data['route'])
        converted=projection.project_route(route,frame,now=frame.captured_at)
        result.update(asdict(converted))
        result['segments']=[]
        for a,b in zip(converted.world_points,converted.world_points[1:]):
            dx,dy=[(y-x)*s for x,y,s in zip(a,b,frame.world_size)]
            result['segments'].append({'from':a,'to':b,'yards':(dx*dx+dy*dy)**.5,
                'heading_ccw_west_radians':compass_from_delta(dx,dy,'ccw_west')})
        result['status']='offline_projection_validated_not_executed'
    except (ProjectionError,ValueError,KeyError,TypeError) as exc:
        result.update({'status':'rejected','error':str(exc)})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
