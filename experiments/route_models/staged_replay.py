"""Repeat production staged planning on saved game frames, without input injection."""
from pathlib import Path
import sys,json,argparse,time,math
from dataclasses import replace
import cv2
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from bot.core.config import load_config
from bot.decision.route_planner import RouteRequest
from bot.decision.grounded_route import npc_identity
from bot.control.navigation import compass_from_delta,heading_error


def main():
 ap=argparse.ArgumentParser();ap.add_argument('--case',choices=['1917','latest'],default='1917');ap.add_argument('--repeat',type=int,default=1);args=ap.parse_args()
 out=ROOT/'data/terrain/deepseek-validation-20261117';out.mkdir(parents=True,exist_ok=True)
 config=replace(load_config().route_planner,staged_grounding=True,request_timeout_s=20)
 if args.case=='1917':
  source=ROOT/'data/terrain/20261116_191810_825d2ec2/right.png';rgb=cv2.cvtColor(cv2.imread(str(source)),cv2.COLOR_BGR2RGB)
  meta=json.loads((source.parent/'report.json').read_text())['metadata'];pose=meta['pose']['right'];run=ROOT/'data/runs/20261116T111736-377cf479/events.jsonl'
 else:
  source=ROOT/'data/screenshots/20261116_215414_camera_after_interact.png';rgb=cv2.cvtColor(cv2.imread(str(source)),cv2.COLOR_BGR2RGB)[78:664,338:1350]
  run=ROOT/'data/runs/20261116T135408-34de5887/events.jsonl';pose=None
 observations=[json.loads(l) for l in run.read_text().splitlines() if json.loads(l)['kind']=='quest_observation']
 nav=observations[0]['data']['navigation']
 if pose is None:pose={'position':nav['position'],'world_size':nav['world_size'],'heading':None}
 dx=(nav['destination'][0]-pose['position'][0])*pose['world_size'][0];dy=(nav['destination'][1]-pose['position'][1])*pose['world_size'][1]
 results=[]
 for i in range(args.repeat):
  facts={'frame_id':args.case+'-staged-'+str(i),'quest_id':90893,'map_id':2451,'phase':'READY',
   'goal_identity':npc_identity(90893,2451,'READY'),'position':pose['position'],'destination':nav['destination'],
   'heading_radians':pose['heading'],'goal_distance_yards':math.hypot(dx,dy),'world_size':pose['world_size'],
   'offline_only':True,'pose_exact_for_frame':args.case=='1917','camera_profile':{'profile':'centered_view_2'},
   'failed_routes':[{'direct_approach_no_displacement_confirmed':True}]}
  if pose['heading'] is not None:facts['goal_relative_degrees']=math.degrees(heading_error(pose['heading'],compass_from_delta(dx,dy)))
  started=time.monotonic();request=RouteRequest(rgb,facts,config)
  result={'case':args.case,'attempt':i,'source':str(source),'evidence':str(request.evidence_dir),'world_route_verified':False}
  try:result['plan']=request.future.result(timeout=23)
  except Exception as exc:result['error']=str(exc)
  finally:request.close()
  result['elapsed_s']=round(time.monotonic()-started,3);results.append(result)
  (out/(args.case+'-production-staged.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2))
  print(json.dumps(result,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
