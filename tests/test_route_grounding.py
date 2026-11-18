"""Grounding, frozen geometry and image/world consistency are separate results."""
import json
import math
import numpy as np
import pytest

from bot.decision.grounded_route import (GroundingError, staged_plan, metric_from_image,
    npc_identity, validate_pixel_path, bounded_point, icon_regions)
from bot.decision.route_geometry import image_metric_error, clearance_error, simple_polygon
from bot.decision.route_planner import parse_plan, PlannerError
from bot.decision.route_render import save_route
from test_model_detour import plan


def test_goal_identity_reads_verified_registry_and_rejects_unknown_map_or_phase():
    identity=npc_identity(90893,2451,'READY')
    assert {k:identity[k] for k in ('id','en','zh')}=={'id':244666,'en':'Thrall','zh':'萨尔'}
    assert 'gold/dark' in identity['visual_hint']
    assert npc_identity(90895,2451,'READY')['id']==244667
    assert npc_identity(90893,999,'READY') is None
    assert npc_identity(90893,2451,'ACTIVE') is None


def test_mirrored_world_route_is_rejected_even_when_image_path_is_clear():
    p=plan();p['waypoints'][1]['right_yards']=-4
    with pytest.raises(PlannerError,match='image_world_side_mismatch'):
        parse_plan(json.dumps(p),'f')


def test_world_straight_line_cannot_masquerade_as_image_detour():
    p=plan();p['waypoints'][1]['right_yards']=0
    assert 'world_straight' in image_metric_error(p)


def test_thin_safe_line_still_needs_body_clearance():
    p=plan();p['player_feet']=[500,750];p['goal_feet']=[600,750]
    p['waypoints']=[{'image_point':[600,750]}]
    p['obstacles']=[{'polygon':[[520,754],[580,754],[580,800],[520,800]]}]
    assert clearance_error(p,(1000,1000),8)=='route_clearance_insufficient_segment_0'
    assert clearance_error(p,(1000,1000),3) is None


@pytest.mark.parametrize('p',[
    [[0,0],[10,10],[0,10],[10,0]],
    [[0,0],[10,0],[20,0]],
    [[0,0],[10,0],[10,10],[0,0]],
])
def test_invalid_solid_polygon_is_not_used_as_collision_evidence(p):
    assert not simple_polygon(p)


def test_pixel_to_metric_mapping_preserves_goal_side_and_endpoint():
    route=[[503,444],[503,528],[344,528],[344,452]]
    metric=metric_from_image(route,8.4,20)
    assert metric[0][0]<0 and metric[1][0]<0
    assert metric[-1]==pytest.approx((0,8.4))
    assert sum(math.dist(a,b) for a,b in zip([(0,0)]+metric,metric))<40
    p={'player_feet':route[0],'goal_feet':route[-1],
       'waypoints':[{'image_point':xy,'right_yards':r,'forward_yards':f} for xy,(r,f) in zip(route[1:],metric)]}
    assert image_metric_error(p) is None


def test_fixed_boundary_and_anchor_cannot_be_adjusted_by_planner():
    start,goal=[503,444],[344,452];obstacle=[[378,353],[495,353],[495,520],[378,520]]
    bounds=[260,300,650,535]
    validate_pixel_path([start,[503,528],[344,528],goal],start,goal,obstacle,bounds,8)
    with pytest.raises(GroundingError,match='frozen_obstacle'):
        validate_pixel_path([start,goal],start,goal,obstacle,bounds,8)
    with pytest.raises(GroundingError,match='anchor_changed'):
        validate_pixel_path([[550,444],[550,528],[344,528],goal],start,goal,obstacle,bounds,8)
    with pytest.raises(GroundingError,match='outside_ground_frame'):
        validate_pixel_path([start,[503,538],[344,538],goal],start,goal,obstacle,bounds,8)


def scene():
    image=np.zeros((586,1012,3),dtype=np.uint8)
    image[337:356,336:349]=[255,255,0]
    return image


def test_overhead_real_question_mark_generates_one_npc_proposal():
    from pathlib import Path
    import cv2
    im=np.zeros((586,1012,3),dtype=np.uint8)
    patch=cv2.cvtColor(cv2.imread(str(Path(__file__).parent/'fixtures/overhead-quest-marker.png')),cv2.COLOR_BGR2RGB)
    im[254:294,390:435]=patch
    regions=icon_regions(im)
    assert len(regions)==1
    x,y,w,h=regions[0]
    assert x<413<x+w and y<276<y+h


def test_staged_perception_freezes_scene_and_plans_once():
    calls=[]
    def ask(name,images,facts,prompt):
        calls.append(name)
        if name in ('player','npc_0'):
            point=[503,444] if name=='player' else [344,452]
            x,y,_,_=facts['crop_scene_pixels']
            return {'feet':[(point[0]-x)*3,(point[1]-y)*3],'matches_target':True}
        if name=='barrier':
            x,y,_,_=facts['crop_scene_pixels']
            return {'box':[(396-x)*3,(371-y)*3,(477-x)*3,(502-y)*3]}
        assert name=='path'
        assert facts['solid_box_including_detection_margin']==pytest.approx([378,353,495,520])
        return {'route_pixels':[[503,444],[503,528],[344,528],[344,452]],'explanation':'around lower end'}
    facts={'frame_id':'frame','goal_identity':{'en':'Thrall'},'goal_distance_yards':8.4}
    result=staged_plan(scene(),facts,ask)
    assert calls==['player','npc_0','barrier','path']
    assert result['metric_basis']=='single_image_path_plane_estimate'
    assert result['grounding']['world_route_verified'] is False
    parse_plan(json.dumps(result),'frame')


def test_obscured_npc_does_not_fall_back_to_question_mark_or_run_planner():
    calls=[]
    def ask(name,images,facts,prompt):
        calls.append(name)
        if name=='player':return {'feet':[354,489]}
        return {'feet':None,'matches_target':False}
    with pytest.raises(GroundingError,match='npc_feet_occluded'):
        staged_plan(scene(),{'goal_identity':{'en':'Thrall'}},ask)
    assert calls==['player','npc_0']


def test_multi_image_coordinate_contamination_is_rejected():
    assert not bounded_point([355,330],[285,411])
    assert bounded_point([148,300],[285,411])


@pytest.mark.parametrize('valid',[True,False])
def test_overhead_outline_review_never_authorizes_uncalibrated_world_movement(valid):
    calls=[]; records=[]
    def ask(name,images,facts,prompt):
        calls.append(name)
        if name in ('player','npc_0'):
            p=[503,444] if name=='player' else [344,452]
            x,y,_,_=facts['crop_scene_pixels']
            return {'feet':[(p[0]-x)*3,(p[1]-y)*3],'matches_target':True}
        if name=='barrier':
            x,y,_,_=facts['crop_scene_pixels']
            polygon=[[396,371],[477,371],[477,502],[396,502]]
            if not valid:polygon[1],polygon[2]=polygon[2],polygon[1]
            width,height=facts['image_size']
            return {'polygon':[[(p[0]-x)*3,(p[1]-y)*3] for p in polygon],
                    'ground_polygons':[[[1,1],[width-2,1],[width-2,height-2],[1,height-2]]],
                    'other_solids':[],'unknown_polygons':[]}
        pytest.fail('Image path must be computed locally, never requested from model')
    error='image_valid_projection_unverified' if valid else 'overhead_outline_unconfirmed'
    with pytest.raises(GroundingError,match=error):
        staged_plan(scene(),{'view_mode':'overhead','goal_identity':{'en':'Thrall'},
                            'goal_distance_yards':8.4,'frame_id':'f'},ask,record_local=records.append)
    assert 'path' not in calls
    if valid:
        assert len(records)==1 and not records[0]['executable']
        assert records[0]['route']['points'][0]==(503,444)
        assert records[0]['route']['points'][-1]==(344,452)
        assert records[0]['scene_map']['source']=='model_observation_unverified'


def test_render_keeps_rejected_path_for_visual_review(tmp_path):
    p=plan();p['obstacles']=[{'polygon':[[530,730],[570,730],[570,830],[530,830]]}]
    with pytest.raises(PlannerError):parse_plan(json.dumps(p),'f')
    target=tmp_path/'rejected.png'
    save_route(np.zeros((586,1012,3),dtype=np.uint8),p,target,title='REJECTED')
    assert target.is_file() and target.stat().st_size>1000


def test_departure_restores_clearance_but_never_permits_sliding_or_crossing():
    from bot.decision.route_geometry import clearance_departure
    poly=[[100,100],[200,100],[200,300],[100,300]]
    assert clearance_departure([204,200],[220,200],poly,8)
    assert not clearance_departure([204,200],[204,320],poly,8)
    assert not clearance_departure([204,200],[80,200],poly,8)
    assert not clearance_departure([198,200],[220,200],poly,8)
    validate_pixel_path([[204,200],[220,200],[220,320],[80,320],[80,200]],
                        [204,200],[80,200],poly,[0,0,400,400],8)
    with pytest.raises(GroundingError,match='obstacle'):
        validate_pixel_path([[204,200],[204,320],[80,320],[80,200]],
                            [204,200],[80,200],poly,[0,0,400,400],8)


@pytest.mark.parametrize('crossing',[False,True])
def test_staged_request_strips_crop_offsets_and_records_even_rejected_routes(monkeypatch,tmp_path,crossing):
    from bot.decision import route_planner
    from bot.core.config import RoutePlannerConfig
    monkeypatch.setattr(route_planner,'DATA_DIR',tmp_path)
    calls=[]
    class Client:
        def plan(self,images,facts,cancelled,*,instruction):
            assert 'crop_scene_pixels' not in facts
            calls.append(facts)
            i=len(calls)
            if i==1:
                assert 'goal' not in facts
                answer={'feet':[354,489]}
            elif i==2:answer={'feet':[148,300],'matches_target':True}
            elif i==3:answer={'box':[186,141,429,534],'kind':'wood'}
            else:
                p,g=facts['player_feet'],facts['goal_feet']
                answer={'route_pixels':[p,g] if crossing else [p,[p[0],528],[g[0],528],g]}
            return {'raw_output':json.dumps(answer),'finish_reason':'stop','usage':{'total_tokens':100}}
    request=route_planner.RouteRequest(scene(),{'frame_id':'test','goal_identity':{'en':'Thrall'},
        'goal_distance_yards':8.4},RoutePlannerConfig(staged_grounding=True),client=Client())
    if crossing:
        with pytest.raises(PlannerError,match='obstacle'):request.future.result(timeout=2)
    else:assert request.future.result(timeout=2)['status']=='route'
    report=json.loads((request.evidence_dir/'report.json').read_text())
    assert report['api_calls']==4 and report['usage']['total_tokens']==400
    assert 'crop_scene_pixels' in report['stages'][0]['facts']
    assert (request.evidence_dir/'route.png').is_file()
    request.close()


def test_overhead_request_records_local_route_without_http_path_or_world_projection(monkeypatch,tmp_path):
    from bot.decision import route_planner
    from bot.core.config import RoutePlannerConfig
    monkeypatch.setattr(route_planner,'DATA_DIR',tmp_path)
    calls=[]
    class Client:
        def plan(self,images,facts,cancelled,*,instruction):
            calls.append(facts)
            if len(calls)==1:answer={'feet':[354,489]}
            elif len(calls)==2:answer={'feet':[148,300],'matches_target':True}
            else:
                assert len(calls)==3
                w,h=facts['image_size']
                answer={'polygon':[[30,30],[60,30],[60,60],[30,60]],
                    'ground_polygons':[[[1,1],[w-2,1],[w-2,h-2],[1,h-2]]],
                    'other_solids':[],'unknown_polygons':[]}
            return {'raw_output':json.dumps(answer),'finish_reason':'stop','usage':{'total_tokens':100}}
    request=route_planner.RouteRequest(scene(),{'frame_id':'test','view_mode':'overhead',
        'goal_identity':{'en':'Thrall'},'goal_distance_yards':None},
        RoutePlannerConfig(staged_grounding=True),client=Client())
    with pytest.raises(PlannerError,match='image_valid_projection_unverified'):
        request.future.result(timeout=2)
    report=json.loads((request.evidence_dir/'report.json').read_text())
    assert report['api_calls']==3 and report['usage']['total_tokens']==300
    assert report['local_navigation']['planner']=='bounded_astar'
    assert report['local_navigation']['route']['frame_id']=='test'
    assert not report['executable'] and not report['model_grounding_independently_verified']
    assert (request.evidence_dir/'route.png').is_file()
    assert (request.evidence_dir/'perception.png').is_file()
    request.close()
