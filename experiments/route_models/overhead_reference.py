"""Planning-only diagnostic: reviewed geometry is NOT autonomous perception."""
import json,threading,cv2
from pathlib import Path
from dataclasses import replace
from bot.core.config import load_config
from bot.decision.route_planner import DeepSeekRouteClient
from bot.decision.grounded_route import validate_pixel_path
from bot.decision.route_render import save_route
out=Path('data/terrain/overhead-route-20261117');ref=json.loads((out/'review-reference.json').read_text())
rgb=cv2.cvtColor(cv2.imread(str(out/'scene.png')),cv2.COLOR_BGR2RGB);h,w=rgb.shape[:2]
facts={'image_size':[w,h],'start':ref['player_ground_estimate'],'goal':ref['npc_ground_estimate'],
 'fixed_solid_polygon':ref['solid_polygon'],'bounds':[270,290,620,535],'clearance_px':12,
 'annotation_source':'assistant_review_not_automatic_perception','execution_authorized':False}
prompt='Plan one COMPLETE walking route in literal image pixels around the FIXED supplied solid polygon. Start/end exactly at supplied start/goal, all points inside bounds, all segments at least clearance_px from the solid boundary. Do not move anchors or shrink the obstacle. Select connected visible ground around an actual end of the barricade. Return JSON route_pixels (2..7 points including endpoints) and explanation. No keyboard actions or world coordinates. Geometry is independently reviewed input, this tests route planning only.'
_,enc=cv2.imencode('.png',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
row={'facts':facts,'prompt':prompt,'world_route_verified':False}
row.update(DeepSeekRouteClient(replace(load_config().route_planner,max_tokens=900,request_timeout_s=12)).plan([enc.tobytes()],facts,threading.Event(),instruction=prompt))
try:
 parsed=json.loads(row['raw_output']);path=parsed['route_pixels'];row['parsed']=parsed
 norm=lambda p:[p[0]/w*1000,p[1]/h*1000]
 plan={'player_feet':norm(path[0]),'goal_feet':norm(path[-1]),'path':list(map(norm,path)),'obstacles':[{'polygon':list(map(norm,ref['solid_polygon']))}]}
 save_route(rgb,plan,out/'deepseek-reviewed-geometry.png',title='DeepSeek plan using REVIEWED geometry - not autonomous perception')
 validate_pixel_path(path,facts['start'],facts['goal'],facts['fixed_solid_polygon'],facts['bounds'],facts['clearance_px']);row['geometry_valid']=True
except Exception as e:row['geometry_valid']=False;row['validation_error']=str(e)
(out/'deepseek-reviewed-geometry.json').write_text(json.dumps(row,ensure_ascii=False,indent=2));print(json.dumps(row,ensure_ascii=False))
