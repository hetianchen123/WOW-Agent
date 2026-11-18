"""Frozen local perception -> one complete image route -> consistent metric estimate.

The path generator cannot move the separately detected feet/solid boundary to make
its own answer pass. Execution stays local; these calls occur only at a route request.
"""
import json
import math
import re
from pathlib import Path
import cv2

from ..core.paths import ROOT
from ..perception.scene_scale import AvatarCrop
from .route_geometry import segment_distance, clearance_departure, simple_polygon
from .image_navigation import SceneMap, ImageNavigator, ImageRouteError
from dataclasses import asdict


class GroundingError(ValueError):
    pass


def npc_identity(quest_id, map_id, phase):
    """Read only the already verified addon registry, never infer an unknown NPC."""
    source=(ROOT/'addon/WowAgent/RetailQuestData.lua').read_text()
    registered=re.search(r'\bmap\s*=\s*(\d+)',source)
    if not registered or int(registered[1])!=map_id or phase not in ('READY','PENDING'):
        return None
    quests=dict((int(m[0]),(int(m[1]),int(m[2]))) for m in re.findall(
        r'\[(\d+)\]\s*=\s*\{\s*pickup\s*=\s*(\d+),\s*turnIn\s*=\s*(\d+)',source))
    ids=quests.get(quest_id)
    if not ids:return None
    target=ids[1 if phase=='READY' else 0]
    names=re.search(r'\['+str(target)+r'\]\s*=\s*\{enUS\s*=\s*"([^"]+)",\s*zhCN\s*=\s*"([^"]+)"',source)
    if not names:return None
    identity={'id':target,'en':names[1],'zh':names[2]}
    # Appearance of these two registered NPCs was checked in the map 2451
    # overhead reference. This disambiguates identity; it supplies no route.
    hints={244666:'Bulky male orc in gold/dark heavy armor with a large hammer. '
                   'The slender blue/white-clothed humanoid is a DIFFERENT NPC.',
           244667:'Slender female human in blue/white clothing. '
                   'The bulky gold-armored male orc with a hammer is a DIFFERENT NPC.'}
    if target in hints:identity['visual_hint']=hints[target]
    return identity


def icon_regions(rgb):
    h,w=rgb.shape[:2]
    hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV)
    yellow=cv2.inRange(hsv,(15,150,180),(40,255,255))
    icons=[]
    scale=w/1012
    for contour in cv2.findContours(yellow,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[0]:
        x,y,iw,ih=cv2.boundingRect(contour)
        if (8*scale<=iw<=25*scale and 12*scale<=ih<=30*scale and ih/iw>=1.25
                and .15*w<x<.85*w and .4*h<y<.85*h):
            icons.append((x,y,iw,ih))
    # Glow/border fragments of ONE question mark can form separate contours.
    # Merge proposals before asking the model; otherwise crop-dependent feet
    # estimates of the same NPC are incorrectly treated as two distinct NPCs.
    unique=[]
    for b in sorted(icons,key=lambda b:b[2]*b[3],reverse=True):
        center=(b[0]+b[2]/2,b[1]+b[3]/2)
        if not any(math.dist(center,(a[0]+a[2]/2,a[1]+a[3]/2))<.6*max(a[3],b[3]) for a in unique):
            unique.append(b)
    unique.sort(key=lambda b:abs(b[0]+b[2]/2-w/2))
    return unique[:3]


def bounded_point(p,size):
    return (isinstance(p,list) and len(p)==2 and all(type(v) in (int,float) and math.isfinite(v)
            and 0<=v<size[i] for i,v in enumerate(p)))


def metric_from_image(path, goal_distance, pitch_deg):
    """Local flat-ground approximation tied to ONE image path, not independent LLM coordinates.

    Undo vertical foreshortening at the saved camera pitch, then fit the scale and
    goal-aligned basis to the measured NPC distance. This is NOT exact 3D depth.
    """
    if not .5<=goal_distance<=20 or not 12<=pitch_deg<=60:
        raise GroundingError('route_projection_scale_unavailable')
    origin,goal=path[0],path[-1]
    s=math.sin(math.radians(pitch_deg))
    gx,gy=goal[0]-origin[0],(goal[1]-origin[1])/s
    denominator=gx*gx+gy*gy
    if denominator<400:
        raise GroundingError('route_projection_anchor_too_close')
    result=[]
    for p in path[1:]:
        dx,dy=p[0]-origin[0],(p[1]-origin[1])/s
        result.append(((gx*dy-gy*dx)*goal_distance/denominator,
                       (gx*dx+gy*dy)*goal_distance/denominator))
    return result


def validate_pixel_path(path,start,goal,obstacle,bounds,radius):
    if not isinstance(path,list) or not 2<=len(path)<=7 or not all(
            isinstance(p,list) and len(p)==2 and all(type(v) in (int,float) and math.isfinite(v)
                                                   for v in p) for p in path):
        raise GroundingError('route_pixel_path_invalid')
    if math.dist(path[0],start)>2 or math.dist(path[-1],goal)>2:
        raise GroundingError('route_frozen_anchor_changed')
    if any(not bounds[0]<=p[0]<=bounds[2] or not bounds[1]<=p[1]<=bounds[3] for p in path):
        raise GroundingError('route_pixel_outside_ground_frame')
    # A polygon is a rectangle here; no model-defined self-crossing boundary.
    from .route_planner import segment_hits_polygon
    for i,(a,b) in enumerate(zip(path,path[1:])):
        clearance=min(segment_distance(a,b,c,d) for c,d in zip(obstacle,obstacle[1:]+obstacle[:1]))
        if math.dist(a,b)<2 or segment_hits_polygon(a,b,obstacle) or (clearance<radius-1e-3 and not (
                i==0 and clearance_departure(a,b,obstacle,radius))):
            raise GroundingError(f'route_frozen_obstacle_crossing_{i}')


def staged_plan(rgb,facts,ask,*,pitch_deg=20,record_local=None,cancelled=lambda:False):
    """ask(name, images, facts, instruction) saves each stage and returns parsed JSON."""
    h,w=rgb.shape[:2]
    target=facts.get('goal_identity')
    if not isinstance(target,dict) or not target.get('en'):
        raise GroundingError('route_npc_identity_unavailable')
    scale=3
    def crop(box):
        x1,y1,x2,y2=map(round,box)
        x1,y1=max(0,x1),max(0,y1);x2,y2=min(w,x2),min(h,y2)
        if x2-x1<20 or y2-y1<20:raise GroundingError('route_crop_too_small')
        return cv2.resize(rgb[y1:y2,x1:x2],None,fx=scale,fy=scale),[x1,y1,x2,y2]
    def locate(name,box,instruction):
        picture,box=crop(box);size=[picture.shape[1],picture.shape[0]]
        answer=ask(name,[picture],{'image_size':size,'crop_scene_pixels':box,'goal':target},instruction)
        p=answer.get('feet')
        if p is None:return None,answer
        if not bounded_point(p,size):raise GroundingError('route_crop_coordinate_out_of_bounds')
        return [box[0]+p[0]/scale,box[1]+p[1]/scale],answer
    player,_=locate('player',AvatarCrop.for_scene((w,h)).pixels,
        'Find visible FEET of the central small upright humanoid. Distinguish pets, NPCs, mounts and text. '
        'Return JSON feet [x,y] in literal PIXELS of this single enlarged image, evidence. '
        'If hidden return null. Do not use normalized coordinates or image center as feet.')
    if player is None or not .44*w<=player[0]<=.56*w or not .55*h<=player[1]<=.9*h:
        raise GroundingError('route_player_feet_unconfirmed')
    matches=[]
    for index,(x,y,iw,ih) in enumerate(icon_regions(rgb)):
        cx=x+iw/2
        goal,answer=locate('npc_'+str(index),(cx-ih*2.5,y+ih-4,cx+ih*2.5,y+ih+ih*7),
            'Find the FEET of the requested quest NPC named in facts in this single enlarged image. '
            'Do not substitute a nearby humanoid, question icon, pet or mount. Return JSON '
            'feet [x,y] in literal PIXELS of THIS image or null, matches_target true/false, evidence. '
            'Hidden by effects means null. This crop may contain a DIFFERENT NPC. '
            'Compare visible clothing/build/weapon against goal.visual_hint when provided; '
            'do NOT assume the supplied name is visible text. Contradictory appearance or '
            'unknown identity means matches_target false. Give actual visible evidence.')
        if goal is not None and answer.get('matches_target') is True:
            if not any(math.dist(goal,other)<12*w/1012 for other in matches):matches.append(goal)
    if len(matches)!=1:
        raise GroundingError('route_npc_feet_ambiguous' if matches else 'route_npc_feet_occluded')
    goal=matches[0]
    span=math.dist(player,goal)
    overhead=facts.get('view_mode')=='overhead'
    if overhead:
        # Wider context is essential: the old between-feet crop cut off an end
        # of the curved fence. An AABB also swallowed its nearby free ground.
        pad=max(120*w/1012,span)
        picture,box=crop((min(player[0],goal[0])-pad,
            min(player[1],goal[1])-120*h/586,max(player[0],goal[0])+pad,
            max(player[1],goal[1])+60*h/586))
        size=[picture.shape[1],picture.shape[0]]
        observed=ask('barrier',[picture],{'image_size':size,'crop_scene_pixels':box},
            'This is a steep overhead view. Trace the COMPLETE outer silhouette of the nearest '
            'connected wooden barricade, including all wooden panels, upright posts and diagonal '
            'spikes. Exclude characters, pets, bodies, grass, bushes and their shadows. Return JSON '
            'polygon with 4..16 vertices in perimeter order, literal PIXELS of this single image, '
            'kind and evidence. Follow the actual shape, not an axis-aligned rectangle that fills '
            'adjacent free ground. Do not extend the obstacle to image borders. If an end is cut '
            'off or the complete boundary cannot be identified return polygon null. '
            'Also return ground_polygons (visible ground regions), other_solids (other rocks, walls, '
            'trees or structures), unknown_polygons (occluded/unclear ground). Each is a list of '
            'simple polygons in the SAME crop pixels. Ground may overlap solids: solids and unknown '
            'are subtracted locally. Only include ground you can actually see; grass color alone '
            'does not prove passage. Do not mark bushes, effects, shadows hiding terrain or UI as '
            'known ground. Include connected ground around both ends when visible. Never invent '
            'hidden ground. Use at most 8 polygons per list and 24 vertices per polygon.')
        poly=observed.get('polygon')
        if not isinstance(poly,list) or not 4<=len(poly)<=16 or not all(bounded_point(q,size) for q in poly) or not simple_polygon(poly):
            raise GroundingError('route_overhead_outline_unconfirmed')
        obstacle=[[box[0]+q[0]/scale,box[1]+q[1]/scale] for q in poly]
        barrier_kind=observed.get('kind','overhead_solid_outline')
        margin=10*w/1012
        radius=8*w/1012+margin
        def polygons(key):
            group=observed.get(key)
            if not isinstance(group,list) or len(group)>8:
                raise GroundingError('route_overhead_scene_map_unconfirmed')
            converted=[]
            for poly in group:
                if (not isinstance(poly,list) or not 3<=len(poly)<=24 or
                        not all(bounded_point(q,size) for q in poly) or not simple_polygon(poly)):
                    raise GroundingError('route_overhead_scene_map_unconfirmed')
                converted.append([[box[0]+q[0]/scale,box[1]+q[1]/scale] for q in poly])
            return tuple(converted)
        scene=SceneMap(facts['frame_id'],(w,h),polygons('ground_polygons'),
                       (obstacle,*polygons('other_solids')),polygons('unknown_polygons'),
                       'model_observation_unverified')
        local={'scene_map':asdict(scene),'start':player,'goal':goal,'executable':False,
               'planner':'bounded_astar','perception_verified':False}
        try:
            route=ImageNavigator(scene,body_radius_px=8*w/1012,uncertainty_px=margin).plan(
                player,goal,cancelled=cancelled)
            local['route']=asdict(route)
        except ImageRouteError as exc:
            local['error']=str(exc)
            raise GroundingError(str(exc)) from None
        finally:
            if record_local:record_local(local)
        # No model path call and no conversion using the unrelated low-view pitch.
        raise GroundingError('route_overhead_image_valid_projection_unverified')
    else:
        picture,box=crop((min(player[0],goal[0])-10*w/1012,
            min(player[1],goal[1])-max(120*h/586,span*.75),max(player[0],goal[0])+35*w/1012,
            max(player[1],goal[1])+50*h/586))
        size=[picture.shape[1],picture.shape[0]]
        answer=ask('barrier',[picture],{'image_size':size,'crop_scene_pixels':box},
            'Locate the COMPLETE nearest solid obstruction BETWEEN the humanoids. Include ALL diagonal '
            'wooden spikes AND the upright posts to their lowest ground base if a barricade is present. '
            'Return JSON box [left,top,right,bottom] in literal PIXELS of this image, kind, evidence. '
            'Do not trace just a thin post or include pets/fire/bushes/NPCs. If no reliable whole boundary '
            'can be localized return box null; do not invent a bounding box.')
        b=answer.get('box')
        barrier_kind=answer.get('kind','conservative_solid_bound')
        if not isinstance(b,list) or len(b)!=4 or not bounded_point(b[:2],size) or not bounded_point([b[2]-.01,b[3]-.01],size) or b[0]>=b[2] or b[1]>=b[3]:
            raise GroundingError('route_barrier_extent_unconfirmed')
        margin=18*w/1012
        rect=[box[0]+b[0]/scale-margin,box[1]+b[1]/scale-margin,
              box[0]+b[2]/scale+margin,box[1]+b[3]/scale+margin]
        obstacle=[[rect[0],rect[1]],[rect[2],rect[1]],[rect[2],rect[3]],[rect[0],rect[3]]]
        bounds=[0,.3*h,w-1,.915*h]
        radius=8*w/1012
        frozen={'player_feet':player,'goal_feet':goal,'solid_box_including_detection_margin':rect,
                'image_size':[w,h],'bounds':bounds,'clearance_px':radius,
                'failed_routes':facts.get('failed_routes',[])}
        # Supply numerical clearance constraints, never a hardcoded detour or side.
        # The model repeatedly mistook the nearest vertical face for the bottom face.
        faces=[('x','less',rect[0]-player[0],rect[0]-radius-4),
               ('x','greater',player[0]-rect[2],rect[2]+radius+4),
               ('y','less',rect[1]-player[1],rect[1]-radius-4),
               ('y','greater',player[1]-rect[3],rect[3]+radius+4)]
        outside=[face for face in faces if face[2]>0]
        if not outside:raise GroundingError('route_start_inside_obstacle_uncertainty')
        axis,relation,gap,limit=max(outside,key=lambda f:f[2])
        if gap<radius:
            frozen['departure_constraint']={'axis':axis,'relation':relation,'limit_pixels':limit,
                'current_clearance_pixels':gap,'applies_to':'first waypoint AFTER player feet',
                'other_axis':'keep unchanged for this departure leg'}
    answer=ask('path',[rgb],frozen,
        'Plan ONE COMPLETE ground route in literal IMAGE PIXELS around the FIXED solid obstacle from the '
        'separate perception pass. Start and end exactly at supplied feet. Do not move these anchors '
        'or shrink the boundary. Every line segment must avoid the closed obstacle with clearance_px extra '
        'clearance and stay in bounds. Choose a route around an actual end on connected visible ground, '
        'If the start is outside the solid boundary but already closer than clearance_px, the FIRST leg '
        'must move directly AWAY from the nearest boundary until full clearance is restored; '
        'it must not slide along the face. All later legs require full clearance. '
        'When departure_constraint is provided, satisfy its numeric inequality at route_pixels[1] '
        'and keep the other coordinate unchanged. Then choose the rest of the complete route. '
        'not over wooden tips or UI. Return JSON route_pixels (2..7 points including endpoints), '
        'explanation. If a full grounded route cannot be found return route_pixels null. '
        'No world coordinates, no keyboard commands, no predetermined side or shape.')
    path=answer.get('route_pixels')
    validate_pixel_path(path,player,goal,obstacle,bounds,radius)
    # Snap rounding noise at fixed anchors, never move any intermediate point.
    path[0],path[-1]=player,goal
    distance=facts.get('goal_distance_yards')
    if type(distance) not in (int,float):raise GroundingError('route_projection_scale_unavailable')
    metric=metric_from_image(path,distance,pitch_deg)
    norm=lambda p:[p[0]/w*1000,p[1]/h*1000]
    return {'frame_id':facts['frame_id'],'status':'route','player_feet':norm(player),'goal_feet':norm(goal),
        'obstacles':[{'kind':barrier_kind,'polygon':list(map(norm,obstacle))}],
        'waypoints':[{'right_yards':r,'forward_yards':f,'image_point':norm(p),'basis':'frozen visual route'}
                     for (r,f),p in zip(metric,path[1:])],
        'intent':answer.get('explanation',''),'uncertainty':'Local flat-ground projection with approximate saved camera pitch; requires displacement verification.',
        'grounding':{'source':'separate_local_crops','margin_px':margin,'pixel_path':path,
                     'ground_bounds':bounds,'world_route_verified':False,'pitch_deg':pitch_deg},
        'metric_basis':'single_image_path_plane_estimate'}
