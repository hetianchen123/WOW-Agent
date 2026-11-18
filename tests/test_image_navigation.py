import json
import math
from pathlib import Path

import pytest

from bot.decision.image_navigation import SceneMap, ImageNavigator, ImageRouteError
from bot.decision.route_geometry import segment_distance
from bot.decision.route_planner import segment_hits_polygon


def rect(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def navigator(obstacles=(), unknown=(), ground=None, radius=5, cell=3):
    scene=SceneMap('frame',(240,180),tuple(ground or [rect(1,1,238,178)]),
                   tuple(obstacles),tuple(unknown),'test_geometry')
    return ImageNavigator(scene,body_radius_px=radius,uncertainty_px=0,cell_px=cell)


def independent_check(path, polygons, radius):
    for a,b in zip(path,path[1:]):
        for poly in polygons:
            assert not segment_hits_polygon(a,b,poly)
            assert min(segment_distance(a,b,c,d) for c,d in zip(poly,poly[1:]+poly[:1]))>=radius


def test_free_ground_returns_complete_route_without_api_or_input():
    result=navigator().plan((20,40),(210,100))
    assert result.points==((20,40),(210,100))
    assert result.expanded_nodes==0 and not result.executable


def test_wall_detour_and_smoothed_segments_keep_character_clearance():
    wall=rect(90,35,145,145)
    nav=navigator([wall])
    path=nav.plan((40,95),(200,95)).points
    assert len(path)>2
    independent_check(path,[wall],5)


def test_new_unknown_region_changes_route_without_a_fixed_side_sequence():
    wall=rect(90,40,145,130)
    a=navigator([wall]).plan((40,90),(200,90))
    assert max(p[1] for p in a.points)>130
    bottom=rect(5,132,235,175)
    b=navigator([wall],[bottom]).plan((40,90),(200,90))
    assert min(p[1] for p in b.points)<40
    independent_check(b.points,[wall,bottom],5)


def test_disconnected_observed_ground_does_not_treat_unlabelled_gap_as_free():
    nav=navigator(ground=[rect(1,1,95,178),rect(145,1,238,178)])
    with pytest.raises(ImageRouteError,match='no_observed_route'):
        nav.plan((40,90),(200,90))


def test_narrow_gap_may_pass_point_but_cannot_pass_character():
    walls=[rect(100,1,125,85),rect(100,95,125,178)]
    nav=navigator(walls,radius=6)
    with pytest.raises(ImageRouteError,match='no_observed_route'):
        nav.plan((40,90),(200,90))


def test_diagonal_touching_blocks_do_not_create_a_corner_shortcut():
    blocks=[rect(1,1,119,89),rect(119,89,238,178)]
    with pytest.raises(ImageRouteError,match='no_observed_route'):
        navigator(blocks,radius=0,cell=2).plan((180,35),(50,145))


def test_thin_obstacle_smaller_than_grid_is_still_blocked():
    wall=rect(119,1,120,178)
    with pytest.raises(ImageRouteError,match='no_observed_route'):
        navigator([wall],radius=0,cell=8).plan((40,90),(200,90))


@pytest.mark.parametrize('start,goal,reason',[
    ((110,80),(200,80),'start_blocked'),
    ((40,80),(110,80),'goal_blocked'),
    ((-1,80),(200,80),'start_outside'),
    ((40,80),(240,80),'goal_outside'),
])
def test_invalid_endpoints_are_not_silently_moved_to_clear_ground(start,goal,reason):
    with pytest.raises(ImageRouteError,match=reason):
        navigator([rect(90,35,145,145)]).plan(start,goal)


def test_cancel_and_search_budgets_do_not_return_partial_paths():
    nav=navigator([rect(90,35,145,145)])
    for options,reason in [({'cancelled':lambda:True},'cancelled'),
                           ({'timeout_s':0},'time_budget'),({'max_nodes':1},'node_budget')]:
        with pytest.raises(ImageRouteError,match=reason):
            nav.plan((40,90),(200,90),**options)


def test_frame_and_annotation_source_are_retained_without_world_claims():
    route=navigator().plan((20,30),(200,100))
    assert route.frame_id=='frame' and route.map_source=='test_geometry'
    assert route.executable is False


def test_missing_ground_and_bow_tie_annotations_are_rejected():
    with pytest.raises(ImageRouteError,match='ground_unknown'):
        ImageNavigator(SceneMap('f',(100,100),(),source='review'))
    with pytest.raises(ImageRouteError,match='polygon_invalid'):
        navigator([[[10,10],[50,50],[10,50],[50,10]]])


def test_fractional_endpoint_at_image_edge_does_not_index_past_mask():
    assert not navigator().line_clear((239.9,100),(200,100))


def test_real_overhead_reference_routes_independently_of_recorded_human_path():
    data=json.loads((Path(__file__).parent/'fixtures/overhead-scene-map.json').read_text())
    scene=SceneMap(data['frame_id'],tuple(data['size']),tuple(data['ground']),
                   tuple(data['obstacles']),source=data['source'])
    nav=ImageNavigator(scene)
    result=nav.plan(data['start'],data['goal'])
    independent_check(result.points,data['obstacles'],12)
    assert result.points[0]==tuple(data['start']) and result.points[-1]==tuple(data['goal'])
    assert result.length_px>math.dist(data['start'],data['goal'])
    # A different starting side must find its own passage on the same map.
    alternative=nav.plan((300,495),data['goal'])
    independent_check(alternative.points,data['obstacles'],12)
    assert max(p[0] for p in result.points)>512
    assert min(p[0] for p in alternative.points)<308
