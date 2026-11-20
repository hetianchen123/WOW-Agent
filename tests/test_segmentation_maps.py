import numpy as np
import pytest

from bot.perception.segmentation import LocalSegmenter, fuse_masks, expanded_box, refinement_check, ground_seeds
from bot.decision.image_navigation import ImageNavigator, ImageRouteError


def semantics():
    shape = (180, 240)
    return np.ones(shape, np.uint8), np.full(shape, .95), np.full(shape, .8), {1: 'grass', 2: 'rock', 3: 'tree'}


def instance(label='wooden fence', score=.9):
    mask = np.zeros((180, 240), bool); mask[50:130, 105:115] = True
    return {'label': label, 'mask_score': score, 'mask': mask, 'box': [90, 40, 130, 140]}


@pytest.mark.parametrize("requested", ["auto", "mps"])
def test_only_automatic_sam_mode_retries_gpu_failure_on_cpu(monkeypatch, requested):
    model = LocalSegmenter.__new__(LocalSegmenter)
    model.requested_sam_device = requested
    model.sam_device = "mps"
    model.sam_fallback = None
    model.sam = object()
    image = np.zeros((2, 2, 3), np.uint8)
    calls = []

    def inspect(frame, **options):
        assert frame is image and options["optimize"]
        calls.append(model.sam_device)
        if model.sam_device == "mps":
            raise RuntimeError("GPU device reset")
        return frame

    def build():
        calls.append("build-" + model.sam_device)
        return object()

    monkeypatch.setattr(model, "_inspect", inspect)
    monkeypatch.setattr(model, "_build_sam", build)
    if requested == "auto":
        assert model.inspect(image, optimize=True) is image
        assert model.inspect(image, optimize=True) is image
        assert calls == ["mps", "build-cpu", "cpu", "cpu"]
        assert model.sam_fallback == "GPU device reset"
    else:
        with pytest.raises(RuntimeError, match="GPU device reset"):
            model.inspect(image, optimize=True)
        assert calls == ["mps"]


def test_incomplete_instance_mask_does_not_reclassify_its_box_holes_as_grass():
    ground, solid, unknown = fuse_masks(*semantics(), [instance()])
    assert solid[80, 110] and unknown[80, 100] and not ground[80, 100]
    assert ground[80, 60]
    assert np.all(ground.astype(int)+solid+unknown == 1)


@pytest.mark.parametrize('item', [instance(score=.7), instance(label='tree'), instance(label='rock tree')])
def test_low_score_or_canopy_mask_is_unknown_not_solid_footprint(item):
    ground, solid, unknown = fuse_masks(*semantics(), [item])
    assert unknown[80, 110] and not solid[80, 110] and not ground[80, 110]


def test_unknown_semantic_classes_and_uncertain_grass_are_not_free_ground():
    labels, confidence, margin, names = semantics()
    labels[50:70, 30:50] = 3
    confidence[50:70, 60:80] = .5
    margin[50:70, 90:110] = .1
    ground, solid, unknown = fuse_masks(labels, confidence, margin, names, [])
    assert all(unknown[60, x] for x in [40, 70, 100])


def test_raster_route_keeps_obstacle_clearance_without_polygonizing_masks():
    ground, solid, unknown = fuse_masks(*semantics(), [instance()])
    navigator = ImageNavigator.from_masks('f', ground, solid, unknown, source='synthetic_segmentation')
    path = navigator.plan((40, 90), (200, 90))
    assert len(path.points) > 2 and path.executable is False
    for a, b in zip(path.points, path.points[1:]): assert navigator.line_clear(a, b)
    assert max(p[1] for p in path.points) > 140 or min(p[1] for p in path.points) < 40


def test_unknown_hole_in_ground_mask_stays_unknown_and_blocks_goal():
    ground = np.ones((180, 240), bool); ground[60:120, 90:150] = False
    empty = np.zeros_like(ground)
    nav = ImageNavigator.from_masks('f', ground, empty, empty, source='test')
    with pytest.raises(ImageRouteError, match='goal_blocked'):
        nav.plan((40, 90), (120, 90))


def test_one_pixel_unknown_barrier_is_not_closed_or_removed():
    ground = np.ones((180, 240), bool)
    unknown = np.zeros_like(ground); unknown[:, 120] = True
    nav = ImageNavigator.from_masks('f', ground, np.zeros_like(ground), unknown, source='test')
    with pytest.raises(ImageRouteError, match='no_observed_route'):
        nav.plan((40, 90), (200, 90))


def test_obstacle_wins_overlap_and_input_mutation_does_not_change_planned_map():
    ground = np.ones((180, 240), bool); solid = np.zeros_like(ground); solid[:, 120] = True
    nav = ImageNavigator.from_masks('f', ground, solid, np.zeros_like(ground), source='test')
    solid[:] = False
    assert not nav.line_clear((40, 90), (200, 90))


def test_bad_mask_shape_and_type_fail_instead_of_resizing_or_thresholding():
    a = np.zeros((180, 240), bool)
    for b in [np.zeros((90, 120), bool), a.astype(np.float32)]:
        with pytest.raises(ImageRouteError, match='image_mask'):
            ImageNavigator.from_masks('f', a, b, a, source='test')


def test_ground_evidence_resolves_only_empty_occlusion_margin_with_semantic_agreement():
    item = instance(label='bush', score=.95)
    item['box'] = [40, 40, 150, 140]
    evidence = np.zeros((180, 240), bool); evidence[70:100, 50:110] = True
    labels, confidence, margin, names = semantics()
    confidence[80, 65] = .5
    ground, solid, unknown = fuse_masks(labels, confidence, margin, names, [item], verified_ground=evidence)
    assert ground[80, 60]  # both SAM ground and semantics, away from canopy edge
    assert unknown[80, 65]  # weak semantics remains unknown
    assert unknown[80, 100] and unknown[80, 110]  # border and canopy remain unknown
    assert unknown[110, 60]  # no additional ground evidence
    assert not solid.any()


@pytest.mark.parametrize('label,score', [('wooden fence',.95), ('rock bush',.95), ('bush',.89)])
def test_ground_confirmation_never_opens_solid_ambiguous_or_low_score_boxes(label, score):
    item = instance(label, score)
    item['box'] = [40, 40, 150, 140]
    ground, _, unknown = fuse_masks(*semantics(), [item], verified_ground=np.ones((180,240),bool))
    assert not ground[80,60] and unknown[80,60]


def test_ui_exclusion_overrides_ground_and_solid_without_mutating_inputs():
    item = instance()
    excluded = np.zeros((180,240), bool); excluded[75:85,40:140] = True
    ground, solid, unknown = fuse_masks(*semantics(), [item], excluded=excluded)
    assert unknown[80,110] and unknown[80,60]
    assert not solid[80,110] and not ground[80,60]
    assert item['mask'][80,110] and solid[90,110]


@pytest.mark.parametrize('kwargs', [dict(excluded=np.zeros((2,2),bool)),
    dict(verified_ground=np.ones((180,240),float))])
def test_bad_auxiliary_evidence_rejected(kwargs):
    with pytest.raises(ValueError, match='evidence_mask_invalid'):
        fuse_masks(*semantics(), [], **kwargs)


def test_refinement_preserves_original_structure_and_rejects_unrelated_growth():
    base = np.zeros((30,30),bool); base[10:20,10:20] = True
    expanded = base.copy(); expanded[8:10,10:20] = True
    assert refinement_check(base, expanded, .9, .9)['accepted']
    unrelated = np.zeros_like(base); unrelated[:10,:10] = True
    assert not refinement_check(base, unrelated, .9, .99)['accepted']
    assert not refinement_check(base, np.ones_like(base), .9, .99)['accepted']
    assert not refinement_check(base, expanded, .9, .8)['accepted']
    assert not refinement_check(base, expanded, .8, .99)['accepted']
    assert not refinement_check(base, expanded, .9, float('nan'))['accepted']


def test_context_prompt_clipped_to_frame_and_ground_seeds_stay_in_observed_interiors():
    assert np.allclose(expanded_box([0,0,100,20], (60,120)), [0,0,115,35])
    free = np.zeros((180,240),bool); free[30:150,30:210] = True
    free[60:120,90:150] = False
    points = ground_seeds(free)
    assert 1 <= len(points) <= 3
    assert all(free[y,x] for x,y in points)
    assert ground_seeds(np.zeros_like(free)) == []
    for x,y in ground_seeds(np.ones((180,240), bool)):
        assert min(x,y,239-x,179-y) >= 11
