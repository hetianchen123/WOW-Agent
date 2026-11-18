"""Replay a labelled scene map, without requesting a model or controlling the game."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import cv2

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bot.decision.image_navigation import SceneMap, ImageNavigator
from bot.decision.route_render import save_route


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--map',type=Path,required=True)
    parser.add_argument('--image',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--start',nargs=2,type=float,help='Synthetic changed start; never game movement')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    data=json.loads(args.map.read_text())
    image=cv2.cvtColor(cv2.imread(str(args.image)),cv2.COLOR_BGR2RGB)
    w,h=data['size']
    if image.shape[:2]!=(h,w):raise ValueError('scene image and map size differ')
    scene=SceneMap(data['frame_id'],(w,h),tuple(data['ground']),tuple(data['obstacles']),
                   tuple(data.get('unknown',[])),data['source'])
    route=ImageNavigator(scene).plan(args.start or data['start'],data['goal'])
    norm=lambda p:[p[0]*1000/w,p[1]*1000/h]
    plan={'player_feet':norm(route.points[0]),'goal_feet':norm(route.points[-1]),
          'path':[norm(p) for p in route.points],
          'obstacles':[{'polygon':[norm(p) for p in poly]} for poly in scene.obstacles],
          'ground_polygons':[[norm(p) for p in poly] for poly in scene.ground],
          'unknown_polygons':[[norm(p) for p in poly] for poly in scene.unknown]}
    save_route(image,plan,args.output/'route.png',
               title='LOCAL SEARCH / LABELLED MAP / IMAGE ONLY'+(' / SYNTHETIC START' if args.start else ''))
    result={**asdict(route),'map_file':str(args.map),'synthetic_start':args.start is not None,
            'ground_label':data.get('ground_label'),'world_collision_verified':False}
    (args.output/'report.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':main()
