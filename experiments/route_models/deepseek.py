"""Exercise the production DeepSeek request with saved images; never move WoW."""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import time

import cv2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.core.config import load_config
from bot.decision.route_planner import RouteRequest
from experiments.route_models.compare import prepare, overlay


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,default=ROOT/'data/terrain/model-route-comparison-20261116')
    parser.add_argument('--case',choices=['1917','2137'])
    parser.add_argument('--thinking',action='store_true')
    parser.add_argument('--retry-rejected',action='store_true')
    args=parser.parse_args()
    config=replace(load_config().route_planner,request_timeout_s=20,thinking=args.thinking,
                   max_tokens=2400 if args.thinking else 1600)
    results=[]
    for record in prepare(args.out):
        if args.case and record['case']!=args.case: continue
        rgb=cv2.cvtColor(cv2.imread(record['input']),cv2.COLOR_BGR2RGB)
        facts={'frame_id':'offline-'+record['case'],'quest_id':90893,'map_id':2451,
               'goal_description':'Large armored quest NPC under leftmost yellow question mark in nearby central group.',
               'player_description':'Upright red-armored humanoid near center; molten quadruped is pet.',
               'position':None,'heading_radians':None,'goal_relative_degrees':None,
               'strafe_verified':True,'history':[], 'failed_movements':[],
               'offline_only':True,'unknown_pose_not_fabricated':True}
        if record['case']=='1917':
            meta=json.loads((ROOT/'data/terrain/20261116_191810_825d2ec2/report.json').read_text())['metadata']
            pose=meta['pose']['right']
            events=ROOT/'data/runs/20261116T111736-377cf479/events.jsonl'
            navigation=next(r['data']['navigation'] for r in map(json.loads,events.read_text().splitlines())
                            if r['kind']=='quest_observation')
            from bot.control.navigation import compass_from_delta,heading_error
            dx=(navigation['destination'][0]-pose['position'][0])*pose['world_size'][0]
            dy=(navigation['destination'][1]-pose['position'][1])*pose['world_size'][1]
            facts.update(position=pose['position'],destination=navigation['destination'],
                world_size=pose['world_size'],heading_radians=pose['heading'],
                goal_distance_yards=math.hypot(dx,dy),
                goal_relative_degrees=math.degrees(heading_error(pose['heading'],compass_from_delta(dx,dy))),
                pose_source='exact saved scene report; goal from same run navigation')
        if args.retry_rejected:
            prior=json.loads((ROOT/'data/terrain/deepseek_20261116_222307_1247c6c3/report.json').read_text())
            facts['failed_routes']=[{'reason':'route_crosses_obstacle_or_degenerate_segment_0',
                                     'rejected_plan':json.loads(prior['raw_output'])}]
        started=time.monotonic()
        request=RouteRequest(rgb,facts,config)
        result=dict(record,model=config.model,world_route_verified=False,evidence=str(request.evidence_dir))
        try:
            plan=request.future.result(timeout=23)
            result['plan']=plan
            path=args.out/('deepseek-batch-'+('retry-' if args.retry_rejected else 'thinking-' if args.thinking else '')+record['case']+'.png')
            overlay(record['input'],plan,path)
            result['overlay']=str(path)
        except Exception as exc:
            result['error']=str(exc)
        finally:
            request.close()
        result['elapsed_s']=round(time.monotonic()-started,3)
        results.append(result)
        (args.out/('deepseek-batch-retry.json' if args.retry_rejected else 'deepseek-batch-thinking.json' if args.thinking else 'deepseek-batch.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
