"""Deterministic route overlays: render rejected candidates too, never redraw the plan."""
import math
from pathlib import Path
import cv2
import numpy as np


def draw_route(rgb, plan, *, title='', reference=None):
    image=cv2.cvtColor(rgb.copy(),cv2.COLOR_RGB2BGR)
    h,w=image.shape[:2]
    def point(p):
        if not isinstance(p,(list,tuple)) or len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in p):
            return None
        return round(p[0]*w/1000),round(p[1]*h/1000)
    for field,color in [('ground_polygons',(80,210,80)),('unknown_polygons',(180,80,180))]:
        for poly in plan.get(field,[]) or []:
            points=[point(p) for p in poly]
            if len(points)>=3 and all(p is not None for p in points):
                cv2.polylines(image,[np.array(points,np.int32)],True,color,1,cv2.LINE_AA)
    for obstacle in plan.get('obstacles',[]) or []:
        if not isinstance(obstacle,dict):continue
        points=[point(p) for p in obstacle.get('polygon',[]) or []]
        if len(points)>=3 and all(p is not None for p in points):
            cv2.polylines(image,[np.array(points,np.int32)],True,(50,60,255),2,cv2.LINE_AA)
    path=plan.get('path')
    if not path:
        path=([plan['player_feet']] if plan.get('player_feet') is not None else [])+[
            wp['image_point'] for wp in plan.get('waypoints',[]) if isinstance(wp,dict) and 'image_point' in wp]
    points=[point(p) for p in path or []]
    for i,(a,b) in enumerate(zip(points,points[1:])):
        if a is not None and b is not None:
            cv2.arrowedLine(image,a,b,(255,230,20),2,cv2.LINE_AA,tipLength=.15)
    for i,p in enumerate(points):
        if p is not None:
            cv2.circle(image,p,4,(255,230,20),-1)
            cv2.putText(image,str(i),(p[0]+5,p[1]-5),cv2.FONT_HERSHEY_SIMPLEX,.4,(255,255,255),1,cv2.LINE_AA)
    for field,color,label in [('player_feet',(255,30,255),'P'),('goal_feet',(20,240,255),'G')]:
        p=point(plan.get(field))
        if p is not None:
            cv2.circle(image,p,7,color,2)
            cv2.putText(image,label,(p[0]+9,p[1]+12),cv2.FONT_HERSHEY_SIMPLEX,.5,color,1,cv2.LINE_AA)
    if reference:
        poly=reference.get('solid_polygon',[])
        if poly:
            cv2.polylines(image,[np.array(poly,np.int32)],True,(70,255,70),2,cv2.LINE_AA)
    if title:
        cv2.rectangle(image,(0,0),(w,31),(25,25,25),-1)
        cv2.putText(image,title[:130],(8,21),cv2.FONT_HERSHEY_SIMPLEX,.48,(255,255,255),1,cv2.LINE_AA)
    return cv2.cvtColor(image,cv2.COLOR_BGR2RGB)


def save_route(rgb, plan, destination, **kwargs):
    rendered=draw_route(rgb,plan,**kwargs)
    if not cv2.imwrite(str(Path(destination)),cv2.cvtColor(rendered,cv2.COLOR_RGB2BGR)):
        raise OSError('route overlay could not be saved')
