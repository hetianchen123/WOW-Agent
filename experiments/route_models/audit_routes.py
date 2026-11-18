"""Render exact model paths and grade against a separately reviewed pixel boundary.

The review annotation is never supplied to the production perception/planner.
This certifies image geometry only, not actual game collisions or world projection.
"""
from pathlib import Path
import sys,json,math
import cv2
import numpy as np
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from bot.decision.route_render import draw_route
from bot.decision.route_planner import segment_hits_polygon
from bot.decision.route_geometry import segment_distance


def audit(plan,reference):
    w,h=reference['image_size']
    path=plan.get('path') or [plan['player_feet']]+[wp['image_point'] for wp in plan['waypoints']]
    path=[[p[0]*w/1000,p[1]*h/1000] for p in path]
    poly=reference['solid_polygon'];edges=list(zip(poly,poly[1:]+poly[:1]))
    clearances=[min(segment_distance(a,b,c,d) for c,d in edges) for a,b in zip(path,path[1:])]
    hits=[i for i,(a,b) in enumerate(zip(path,path[1:])) if segment_hits_polygon(a,b,poly)]
    anchor_error=[math.dist(path[0],reference['player_feet']),math.dist(path[-1],reference['goal_feet'])]
    x1,y1,x2,y2=reference['valid_ground_bounds']
    outside=[i for i,p in enumerate(path) if not (x1<=p[0]<=x2 and y1<=p[1]<=y2)]
    return {'pixel_path':path,'crossing_segments':hits,'minimum_clearance_px':round(min(clearances),3),
        'outside_reviewed_ground_points':outside,
        'anchor_error_px':list(map(lambda x:round(x,3),anchor_error)),
        'image_geometry_pass':not hits and not outside and min(clearances)>=reference['clearance_px'] and max(anchor_error)<=15,
        'world_route_verified':False}


def main():
    out=ROOT/'data/terrain/deepseek-validation-20261117'
    reference=json.loads((out/'1917-review-reference.json').read_text())
    rgb=cv2.cvtColor(cv2.imread(str(ROOT/reference['source'])),cv2.COLOR_BGR2RGB)
    oldreport=json.loads((ROOT/'data/terrain/deepseek_20261116_222307_1247c6c3/report.json').read_text())
    old=json.loads(oldreport['raw_output'])
    results=json.loads((out/'1917-production-staged.json').read_text())
    entries=[('BEFORE: single prompt',old,None)]
    for row in results:
        report=json.loads((Path(row['evidence'])/'report.json').read_text())
        plan=row.get('plan') or report.get('pixel_candidate')
        if plan is not None:entries.append((f'AFTER: full pipeline run {row["attempt"]+1}',plan,row))
    panels=[];audits=[]
    for title,plan,row in entries:
        grade=audit(plan,reference)
        report=json.loads((Path(row['evidence'])/'report.json').read_text()) if row else oldreport
        record={'title':title,**grade,'api_calls':report.get('api_calls',1),
            'runtime_error':row.get('error') if row else oldreport.get('error'),
            'tokens':report.get('usage',{}).get('total_tokens'),'elapsed_s':report.get('elapsed_s'),
            'evidence':row['evidence'] if row else str(ROOT/'data/terrain/deepseek_20261116_222307_1247c6c3')}
        audits.append(record)
        overlay=draw_route(rgb,plan,reference=reference)
        crop=cv2.resize(overlay[280:545,280:650],(740,530),interpolation=cv2.INTER_LINEAR)
        panel=np.full((640,740,3),24,dtype=np.uint8);panel[60:590]=crop
        status='PASS image geometry' if grade['image_geometry_pass'] and not record['runtime_error'] else 'REJECTED candidate'
        cv2.putText(panel,title,(15,24),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),1,cv2.LINE_AA)
        cv2.putText(panel,status+f' | clearance {grade["minimum_clearance_px"]:.1f}px',
                    (15,49),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1,cv2.LINE_AA)
        cv2.putText(panel,'Cyan: exact model path | Red: model solid bound',(15,611),cv2.FONT_HERSHEY_SIMPLEX,.47,(255,255,255),1,cv2.LINE_AA)
        cv2.putText(panel,'Green: independent fence review | P: player | G: NPC',(15,632),cv2.FONT_HERSHEY_SIMPLEX,.47,(255,255,255),1,cv2.LINE_AA)
        panels.append(panel)
    if len(panels)>1:
        passed=next((i for i,r in enumerate(audits) if r['image_geometry_pass'] and not r['runtime_error']),None)
        if passed is not None:
            cv2.imwrite(str(out/'route-before-after.png'),cv2.cvtColor(np.hstack([panels[0],panels[passed]]),cv2.COLOR_RGB2BGR))
    if len(panels)>2:
        if len(panels)%2:panels.append(np.zeros_like(panels[0]))
        grid=np.vstack([np.hstack(panels[i:i+2]) for i in range(0,len(panels),2)])
        cv2.imwrite(str(out/'route-all-runs.png'),cv2.cvtColor(grid,cv2.COLOR_RGB2BGR))
    (out/'independent-route-audit.json').write_text(json.dumps({'reference':reference,
        'reference_not_sent_to_production_model':True,'runs':audits},ensure_ascii=False,indent=2))
    print(json.dumps(audits,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
