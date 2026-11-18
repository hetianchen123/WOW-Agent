"""Real local model replay. Human labels enter ONLY after inference for scoring.

Manual feet/goal, when available, are labelled geometry probes, not automatic NPC
localization. Unknown ground is never opened to make a route pass.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bot.perception.segmentation import LocalSegmenter, DETECTION_PROMPT
from bot.decision.image_navigation import ImageNavigator, ImageRouteError


def save(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def run(args):
    args.checkpoint = args.checkpoint or ROOT/f'data/segmentation/sam2.1_hiera_{args.variant}.pt'
    paths = json.loads(args.models.read_text())
    engine = LocalSegmenter(paths, args.checkpoint, variant=args.variant, sam_device=args.sam_device)
    checkpoint_hash = hashlib.sha256(args.checkpoint.read_bytes()).hexdigest()
    reports = []
    for index, image_path in enumerate(args.images):
        output = args.output/f'{index:02d}-{image_path.stem}'; output.mkdir(parents=True, exist_ok=True)
        image = cv2.cvtColor(cv2.imread(str(image_path)), cv2.COLOR_BGR2RGB)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        excluded = None
        if args.ui_manifest:
            # Explicit UI-only annotations are keyed by exact source-file hash.
            # No scene obstacle/ground annotations are accepted as inference input.
            manifest = json.loads(args.ui_manifest.read_text())
            if digest not in manifest:
                raise ValueError('ui_mask_frame_missing')
            raw = cv2.imread(str(args.ui_manifest.parent/manifest[digest]), cv2.IMREAD_GRAYSCALE)
            if raw is None or raw.shape != image.shape[:2] or not np.isin(raw, [0,255]).all():
                raise ValueError('ui_mask_invalid')
            excluded = raw.astype(bool)
        result = engine.inspect(image, local_detail=args.local_detail, optimize=args.optimize, excluded=excluded)
        np.savez_compressed(output/'map.npz', ground=result.ground, obstacles=result.obstacles,
                            unknown=result.unknown, semantic_labels=result.semantic_labels,
                            semantic_confidence=result.semantic_confidence, ground_evidence=result.ground_evidence,
                            occluded=result.occluded,water=result.water,forbidden=result.forbidden)
        h, w = image.shape[:2]
        report = {'image': str(image_path), 'image_sha256': digest, 'size': [w, h], 'frame_id': digest[:16],
            'source': 'local_segmentation_unverified', 'masks_file': 'map.npz', 'models': paths,
            'sam_variant': args.variant, 'local_detail': args.local_detail, 'sam_checkpoint_sha256': checkpoint_hash,
            'prompt': DETECTION_PROMPT, 'device': {'sam':args.sam_device,'detection':'cpu','semantics':'cpu'}, 'load_s': engine.load_s,
            'optimization':args.optimize, 'diagnostics':result.diagnostics,
            'ui_mask_source':'reviewed_ui_only' if excluded is not None else None,
            'ui_mask_sha256':hashlib.sha256(excluded.tobytes()).hexdigest() if excluded is not None else None,
            'timings': result.timings, 'instances': [], 'executable': False,
            'world_collision_verified': False, 'automatic_player_and_npc_localization': False,
            'ground_fraction': float(result.ground.mean()), 'solid_fraction': float(result.obstacles.mean()),
            'unknown_fraction': float(result.unknown.mean())}
        report['navigation_layers'] = dict(occluded_fraction=float(result.occluded.mean()),
            water_fraction=float(result.water.mean()),forbidden_fraction=float(result.forbidden.mean()),
            obstacle_basis='visual_silhouette_not_world_collision',
            occlusion_policy='bounded_probe_candidate_not_confirmed_ground')
        overlay = image.copy()
        for mask, color in [(result.unknown, [120, 90, 150]), (result.ground, [40, 220, 75]), (result.obstacles, [240, 45, 35])]:
            overlay[mask] = (overlay[mask]*.6+np.asarray(color)*.4).astype(np.uint8)
        detections = image.copy()
        for i, item in enumerate(result.instances):
            record = {k: v for k, v in item.items() if k != 'mask'}
            record['mask_file'] = f'instance-{i:02d}.png'
            cv2.imwrite(str(output/record['mask_file']), item['mask'].astype(np.uint8)*255)
            report['instances'].append(record)
            x1, y1, x2, y2 = [round(v) for v in item['box']]
            cv2.rectangle(detections, (x1, y1), (x2, y2), (255, 200, 0), 1)
            cv2.putText(detections, f'{i}:{item["label"]} {item["detection_score"]:.2f}',
                        (max(0,x1), max(15,y1)), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 220, 0), 1)
        counts = np.bincount(result.semantic_labels.ravel(), minlength=150)
        report['semantic_classes'] = {engine.semantic.config.id2label[int(i)]: int(counts[i])
                                      for i in counts.argsort()[::-1] if counts[i] > 0}
        # Independent review is not loaded before model prediction or mask selection.
        if digest == hashlib.sha256(args.reference_image.read_bytes()).hexdigest():
            reference = json.loads(args.reference.read_text())
            if reference['size'] != [w, h]: raise ValueError('reference_size_mismatch')
            solid = np.zeros((h,w), np.uint8); roi = np.zeros_like(solid)
            cv2.fillPoly(solid, [np.asarray(p, np.int32) for p in reference['obstacles']], 1)
            cv2.fillPoly(roi, [np.asarray(p, np.int32) for p in reference['ground']], 1)
            solid, roi = solid.astype(bool), roi.astype(bool)
            predicted = result.obstacles & roi
            report['audit'] = {
                'reference_source': reference['source'], 'score_region': 'reviewed_fence_neighborhood',
                'solid_recall': float((predicted & solid).sum()/max(1,solid.sum())),
                'solid_iou': float((predicted & solid).sum()/max(1,(predicted | solid).sum())),
                'reference_solid_marked_ground_fraction': float((solid & result.ground).sum()/max(1,solid.sum())),
                'reference_solid_marked_unknown_fraction': float((solid & result.unknown).sum()/max(1,solid.sum())),
                'ground_truth_walkability_available': False}
            navigator = ImageNavigator.from_masks(report['frame_id'], result.ground, result.obstacles,
                                                  result.unknown, source=report['source'])
            report['route_probe_anchor_source'] = 'manual_review_not_automatic_localization'
            report['anchor_diagnostics'] = {}
            for name in ('start', 'goal'):
                x,y = reference[name]
                area = np.s_[max(0,y-12):min(h,y+13),max(0,x-12):min(w,x+13)]
                report['anchor_diagnostics'][name] = dict(
                    pixel_class=engine.semantic.config.id2label[int(result.semantic_labels[y,x])],
                    semantic_confidence=float(result.semantic_confidence[y,x]),
                    ground_fraction=float(result.ground[area].mean()),
                    solid_fraction=float(result.obstacles[area].mean()),
                    unknown_fraction=float(result.unknown[area].mean()))
            try:
                route = navigator.plan(reference['start'], reference['goal'])
                report['route_probe'] = asdict(route)
                cv2.polylines(overlay, [np.asarray(route.points,np.int32)], False, (0,255,255), 2)
            except ImageRouteError as exc: report['route_probe_error'] = str(exc)
            report['route_probe_policy'] = 'strict_visible_ground_legacy_diagnostic_not_approach_controller'
            for name, color in [('start',(255,255,0)), ('goal',(255,255,255))]:
                cv2.circle(overlay, tuple(reference[name]), 5, color, 2)
            # Independent reference only appears in the audit view, never inference.
            audit = overlay.copy()
            cv2.polylines(audit, [np.asarray(p,np.int32) for p in reference['obstacles']], True, (255,255,0), 2)
            cv2.imwrite(str(output/'audit.png'), cv2.cvtColor(audit, cv2.COLOR_RGB2BGR))
        for name, visual in [('map',overlay), ('detections',detections)]:
            cv2.rectangle(visual,(0,0),(w,28),(20,20,20),-1)
            title = ('RED solid / GREEN candidate ground / PURPLE unknown / IMAGE ONLY'
                     if name == 'map' else 'AUTOMATIC DETECTIONS / NO HUMAN BOXES')
            cv2.putText(visual,title,(8,19),cv2.FONT_HERSHEY_SIMPLEX,.43,(255,255,255),1)
            cv2.imwrite(str(output/(name+'.png')),cv2.cvtColor(visual,cv2.COLOR_RGB2BGR))
        save(output/'report.json',report); reports.append(report)
        print(json.dumps({k:report[k] for k in ['image','timings','ground_fraction','solid_fraction','unknown_fraction','audit','route_probe_error'] if k in report}),flush=True)
    save(args.output/'summary.json', reports)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images',type=Path,nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--models',type=Path,default=ROOT/'data/segmentation/models.json')
    parser.add_argument('--variant',choices=['tiny','large'],default='large')
    parser.add_argument('--local-detail',action='store_true')
    parser.add_argument('--optimize',action='store_true')
    parser.add_argument('--sam-device',choices=['cpu','mps'],default='cpu')
    parser.add_argument('--ui-manifest',type=Path,help='JSON: image SHA256 -> UI-only binary mask path')
    parser.add_argument('--checkpoint',type=Path)
    parser.add_argument('--reference',type=Path,default=ROOT/'tests/fixtures/overhead-scene-map.json')
    parser.add_argument('--reference-image',type=Path,default=ROOT/'data/terrain/overhead-route-20261117/scene.png')
    run(parser.parse_args())
