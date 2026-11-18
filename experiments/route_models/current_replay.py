"""Read a saved current frame; no game input and no fabricated world pose."""
import json,time,sys,argparse
from pathlib import Path
from dataclasses import replace
import cv2
root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root))
from bot.core.config import load_config
from bot.decision.route_planner import RouteRequest
from bot.decision.grounded_route import npc_identity
from bot.perception.scene_scale import SCENE_BOUNDS
parser=argparse.ArgumentParser();parser.add_argument('--model',default='deepseek-flash');parser.add_argument('--repeat',type=int,default=3);parser.add_argument('--view-mode',choices=['low','overhead'],default='low');parser.add_argument('--image',type=Path);parser.add_argument('--output',type=Path);args=parser.parse_args()
out=args.output or root/'data/terrain/current-route-20261117';out.mkdir(parents=True,exist_ok=True)
source=args.image or out/'current-window.png'
image=cv2.cvtColor(cv2.imread(str(source)),cv2.COLOR_BGR2RGB);h,w=image.shape[:2]
l,t,r,b=[round(v*n) for v,n in zip(SCENE_BOUNDS,(w,h,w,h))];rgb=image[t:b,l:r]
cv2.imwrite(str(out/'scene.png'),cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
results=[]
for i in range(args.repeat):
 facts={'frame_id':out.name+'-'+str(i),'goal_identity':npc_identity(90893,2451,'READY'),
  'goal_identity_source':'previous_user_task_target_not_live_selected_receipt',
  'view_mode':args.view_mode,'offline_only':True,'pose_exact_for_frame':False,'goal_distance_yards':None,
  'camera_profile':None,'position':None,'destination':None,'failed_routes':[],
  'capture_metadata':json.loads((out/'capture.json').read_text()) if (out/'capture.json').exists() else {'source':str(source),'provided_by_user':True,'live_pose_unavailable':True}}
 request=RouteRequest(rgb,facts,replace(load_config().route_planner,staged_grounding=True,request_timeout_s=20,model=args.model))
 row={'attempt':i,'evidence':str(request.evidence_dir),'world_route_verified':False}
 try:row['plan']=request.future.result(timeout=23)
 except Exception as exc:row['error']=str(exc)
 finally:request.close()
 report=json.loads((request.evidence_dir/'report.json').read_text());row['pixel_candidate']=report.get('pixel_candidate');row['elapsed_s']=report.get('elapsed_s');row['usage']=report.get('usage');row['api_calls']=report.get('api_calls')
 results.append(row);(out/('replay-'+args.model+'.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2))
 print(json.dumps(row,ensure_ascii=False),flush=True)
