"""Local detection + instance/semantic segmentation. No API or game input.

Run with the isolated segmentation environment; imports of Torch/Transformers
are deferred so the main controller does not acquire heavyweight dependencies.
All ground is a visual candidate, never a verified collision-free surface.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time

import cv2
import numpy as np
from .building_layout import door_state


GROUND_LABELS = {'grass', 'road', 'earth', 'floor', 'sidewalk', 'field', 'path', 'land','rug','carpet'}
SOLID_LABELS = {'wall', 'building', 'house', 'roof', 'ceiling', 'rock', 'fence', 'column',
                'railing', 'banister', 'cabinet', 'wardrobe', 'bookcase', 'table', 'desk',
                'counter', 'bed', 'sofa', 'chest of drawers', 'windowpane','shelf','bannister'}
DETECTION_PROMPT = 'wooden fence. wooden post. wooden spike. rock. tree. bush. person.'


@dataclass
class SegmentedMap:
    ground: np.ndarray
    obstacles: np.ndarray
    unknown: np.ndarray
    instances: list
    semantic_labels: np.ndarray
    semantic_confidence: np.ndarray
    timings: dict
    diagnostics: dict
    ground_evidence: np.ndarray
    occluded: np.ndarray
    water: np.ndarray
    forbidden: np.ndarray
    raw_semantic_labels: np.ndarray | None = None
    raw_semantic_confidence: np.ndarray | None = None
    review_sources: np.ndarray | None = None
    portal_evidence: dict | None = None


def navigation_evidence(labels, confidence, names, instances, ground, solid, unknown, excluded=None):
    """Classify unknowns by cause, WITHOUT turning them into verified ground.

    Occlusion/vegetation supports bounded probe proposals; water requires a
    separate movement mode. Unresolved solid boxes and UI remain forbidden.
    """
    h,w = labels.shape
    names={int(i):str(label).strip().lower() for i,label in names.items()}
    soft_ids = [i for i,label in names.items() if label in {'grass','plant','tree','person','animal'}]
    water_ids = [i for i,label in names.items() if label in {'water','river','sea','swimming pool'}]
    occluded = np.isin(labels,soft_ids) & (confidence >= .3)
    water = np.isin(labels,water_ids) & (confidence >= .5)
    forbidden = np.zeros_like(ground) if excluded is None else excluded.copy()
    for item in instances:
        label = item['label']
        if any(t in label for t in ('fence','post','spike','rock')):
            x1,y1 = np.maximum(0,np.floor(item['box'][:2]).astype(int))
            x2,y2 = np.minimum([w,h],np.ceil(item['box'][2:]).astype(int))
            # A missing fence part cannot become an occlusion shortcut.
            forbidden[y1:y2,x1:x2] |= unknown[y1:y2,x1:x2]
        elif any(t in label for t in ('person','tree','bush')) and item['mask_score'] >= .85:
            occluded |= item['mask']
    occluded &= unknown & ~solid & ~forbidden & ~water
    water &= ~solid & ~forbidden
    return occluded, water, forbidden


def fuse_masks(labels, confidence, margin, names, instances, *, ground_threshold=.8,
               verified_ground=None, excluded=None):
    """Unresolved detection boxes override semantic ground, even outside SAM masks."""
    if (labels.ndim != 2 or confidence.shape != labels.shape or margin.shape != labels.shape
            or not np.isfinite(confidence).all() or not np.isfinite(margin).all()):
        raise ValueError('segmentation_semantics_invalid')
    ground_ids = [int(i) for i, label in names.items() if label.strip().lower() in GROUND_LABELS]
    solid_ids = [int(i) for i, label in names.items() if label.strip().lower() in SOLID_LABELS]
    ground = np.isin(labels, ground_ids) & (confidence >= ground_threshold) & (margin >= .2)
    solid = np.isin(labels, solid_ids) & (confidence >= .65)
    uncertain = np.zeros(labels.shape, bool)
    h, w = labels.shape
    for mask in (verified_ground, excluded):
        if mask is not None and (mask.dtype != bool or mask.shape != labels.shape):
            raise ValueError('segmentation_evidence_mask_invalid')
    for item in instances:
        mask = item['mask']
        if mask.dtype != bool or mask.shape != labels.shape:
            raise ValueError('segmentation_instance_mask_invalid')
        box = np.asarray(item['box'], float)
        if box.shape != (4,) or not np.isfinite(box).all() or box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError('segmentation_instance_box_invalid')
        x1, y1 = np.maximum(0, np.floor(box[:2]).astype(int))
        x2, y2 = np.minimum([w, h], np.ceil(box[2:]).astype(int))
        if x1 >= x2 or y1 >= y2: raise ValueError('segmentation_instance_box_invalid')
        # Tree canopy/people are occlusion, not known solid ground footprints.
        label = item['label']
        door=door_state(item,labels,confidence,names)
        if door is not None:
            if door=='closed' and item['mask_score']>=.85:
                solid |= mask
            # Door wording alone cannot label stairs as a physical panel. The
            # unchanged semantic wall/unknown map still controls traversability.
            continue
        # Terrain/context boxes are not obstacle extents. A building box can
        # contain its doorway AND the courtyard; a road box contains ground.
        # Keep semantic solidity/uncertainty. These detections add no free pixels.
        context_words=GROUND_LABELS | {'building','house','stairs','stairway','ground','wooden','stone'}
        if set(label.strip().lower().split()) and set(label.strip().lower().split()) <= context_words:
            continue
        if label.strip() in {'roof','ceiling','wall','railing','column','table',
                             'bed','wardrobe','bookcase','cabinet','chest',
                             'closed door','closed gate','locked door'}:
            # A wall/roof surface mask is physical evidence. Only its enclosing
            # rectangle is context; neither a doorway nor grass can erase it.
            if item['mask_score']>=.85:solid |= mask
            else:uncertain |= mask
            continue
        is_solid = any(term in label for term in ('fence', 'post', 'spike', 'rock'))
        ambiguous = any(term in label for term in ('tree', 'bush', 'person'))
        box_unknown = np.zeros(labels.shape, bool)
        box_unknown[y1:y2, x1:x2] = True
        # Only additional SAM ground evidence AND confident semantics may resolve
        # empty margins of strong occlusion masks. Never open solid/low-score boxes.
        if ambiguous and not is_solid and item['mask_score'] >= .9 and verified_ground is not None:
            guarded = cv2.dilate(mask.astype(np.uint8), np.ones((17, 17), np.uint8)).astype(bool)
            box_unknown &= ~(verified_ground & ground & ~guarded)
        uncertain |= box_unknown
        if is_solid and not ambiguous and item['mask_score'] >= .85:
            solid |= mask
        else:
            uncertain |= mask
    ground &= ~solid & ~uncertain
    if excluded is not None:
        ground &= ~excluded
        solid &= ~excluded
    return ground, solid, ~(ground | solid)


def expanded_box(box, shape, fraction=.15):
    """Symmetric context in image pixels; never a world-space obstacle extent."""
    h, w = shape
    box = np.asarray(box, float)
    pad = fraction*max(box[2]-box[0], box[3]-box[1])
    return np.clip(box+[-pad, -pad, pad, pad], [0, 0, 0, 0], [w, h, w, h])


def refinement_check(original, candidate, base_score, score):
    """Quality/stability gates independent of audit labels. No mask hole filling."""
    retained = float((original & candidate).sum()/max(1, original.sum()))
    growth = float(candidate.sum()/max(1, original.sum()))
    accepted = (np.isfinite([base_score, score]).all() and base_score >= .85 and score >= .85
                and retained >= .95 and 1 <= growth <= 2)
    return dict(accepted=bool(accepted), retained=retained, growth=growth, score=float(score))


def ground_seeds(candidate, *, limit=3, min_radius=12):
    """Choose spatially separated interior points, without player/route annotations."""
    remaining = candidate.astype(np.uint8).copy()
    points = []
    for _ in range(limit):
        # Image edges are unobserved, not an infinite continuation of the floor.
        distance = cv2.distanceTransform(np.pad(remaining, 1), cv2.DIST_L2, 5)[1:-1, 1:-1]
        _, radius, _, point = cv2.minMaxLoc(distance)
        if radius < min_radius:
            break
        points.append(point)
        cv2.circle(remaining, point, max(48, int(radius*2)), 0, -1)
    return points


class LocalSegmenter:
    def __init__(self, paths, checkpoint, *, variant='tiny', threads=4, sam_device='cpu'):
        import torch
        from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection
        from transformers import AutoImageProcessor, SegformerForSemanticSegmentation
        began = time.monotonic()
        torch.set_num_threads(threads)
        self.torch = torch
        if sam_device not in ('auto', 'cpu', 'mps'):
            raise ValueError('segmentation_device_invalid')
        has_mps = sam_device != 'cpu' and torch.backends.mps.is_available()
        if sam_device == 'mps' and not has_mps:
            raise RuntimeError('segmentation_mps_unavailable')
        self.requested_sam_device = sam_device
        self.sam_device = 'mps' if has_mps else 'cpu'
        self.sam_fallback = 'segmentation_mps_unavailable' if sam_device == 'auto' and not has_mps else None
        self.checkpoint = str(checkpoint)
        self.sam_variant = {'tiny': 't', 'large': 'l'}[variant]
        self.detector_processor = AutoProcessor.from_pretrained(paths['IDEA-Research/grounding-dino-tiny']['path'], local_files_only=True)
        self.detector = AutoModelForZeroShotObjectDetection.from_pretrained(
            paths['IDEA-Research/grounding-dino-tiny']['path'], local_files_only=True).eval()
        semantic_path = paths['nvidia/segformer-b2-finetuned-ade-512-512']['path']
        self.semantic_processor = AutoImageProcessor.from_pretrained(semantic_path, local_files_only=True)
        self.semantic = SegformerForSemanticSegmentation.from_pretrained(semantic_path, local_files_only=True).eval()
        try:
            self.sam = self._build_sam()
        except RuntimeError as exc:
            if not self._switch_sam_to_cpu(exc):
                raise
        self.load_s = time.monotonic()-began

    def _build_sam(self):
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        return SAM2ImagePredictor(build_sam2(f'configs/sam2.1/sam2.1_hiera_{self.sam_variant}.yaml',
            self.checkpoint, device=self.sam_device, apply_postprocessing=False))

    def _switch_sam_to_cpu(self, error):
        if self.requested_sam_device != 'auto' or self.sam_device != 'mps':
            return False
        self.sam = None
        self.sam_device = 'cpu'
        self.sam_fallback = str(error)[:512]
        self.sam = self._build_sam()
        return True

    def _detect(self, image, prompt, size):
        began = time.monotonic()
        inputs = self.detector_processor(images=image, text=prompt, return_tensors='pt')
        detected = self.detector(**inputs)
        found = self.detector_processor.post_process_grounded_object_detection(
            detected, inputs.input_ids, threshold=.25, text_threshold=.25, target_sizes=[size])[0]
        return found, time.monotonic() - began

    def _embed(self, rgb):
        began = time.monotonic()
        with self.torch.inference_mode():
            self.sam.set_image(rgb)
            self._sync()
        return time.monotonic() - began

    def _semantics(self, image, size):
        torch = self.torch
        inp = self.semantic_processor(images=image, return_tensors='pt')
        logits = self.semantic(**inp).logits
        probs = torch.nn.functional.interpolate(logits, size=size, mode='bilinear', align_corners=False).softmax(dim=1)
        top = probs.topk(2, dim=1)
        return (top.indices[0,0].numpy().astype(np.uint8), top.values[0,0].numpy(),
                (top.values[0,0]-top.values[0,1]).numpy())

    def inspect_geometry(self, rgb):
        """Stationary rotation texture only. These masks cannot authorize walking."""
        from PIL import Image
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
            raise ValueError('segmentation_rgb_invalid')
        began = time.monotonic()
        with self.torch.inference_mode():
            labels,confidence,margin = self._semantics(Image.fromarray(rgb),rgb.shape[:2])
        ground,solid,unknown = fuse_masks(labels,confidence,margin,self.semantic.config.id2label,[],
                                          ground_threshold=.95)
        empty = np.zeros_like(ground)
        return SegmentedMap(ground,solid,unknown,[],labels,confidence,
            {'semantics_s':time.monotonic()-began}, {'geometry_only':True},
            empty,empty,empty,np.ones_like(ground))

    def inspect(self, rgb, *, prompt=DETECTION_PROMPT, local_detail=False, optimize=False, excluded=None,
                review_surfaces=False,review_portals=False):
        options = dict(prompt=prompt, local_detail=local_detail, optimize=optimize, excluded=excluded,
                       review_surfaces=review_surfaces, review_portals=review_portals)
        try:
            return self._inspect(rgb, **options)
        except RuntimeError as exc:
            if not self._switch_sam_to_cpu(exc):
                raise
        return self._inspect(rgb, **options)

    def _inspect(self, rgb, *, prompt, local_detail, optimize, excluded, review_surfaces, review_portals):
        from PIL import Image
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
            raise ValueError('segmentation_rgb_invalid')
        if excluded is not None:
            if excluded.shape != rgb.shape[:2] or excluded.dtype != bool:
                raise ValueError('segmentation_evidence_mask_invalid')
        if optimize and local_detail:
            raise ValueError('segmentation_detail_and_optimize_incompatible')
        image = Image.fromarray(rgb)
        h, w = rgb.shape[:2]
        torch = self.torch
        timings = {}
        diagnostics = {'optimization': optimize, 'ground_proposals': [], 'refinements': [], 'ignored_ui_detections': 0,
                       'sam_device': self.sam_device, 'sam_device_requested': self.requested_sam_device,
                       'sam_fallback': self.sam_fallback,
                       'excluded_pixels': int(excluded.sum()) if excluded is not None else 0}
        with torch.inference_mode():
            if self.sam_device == 'mps':
                # Both stages read the same immutable exposure. Wait for its
                # embedding before issuing any mask prompt for this frame.
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix='sam-embed') as gpu:
                    embedding = gpu.submit(self._embed, rgb)
                    found, timings['detection_s'] = self._detect(image, prompt, (h, w))
                    timings['embedding_s'] = embedding.result()
            else:
                found, timings['detection_s'] = self._detect(image, prompt, (h, w))
            # Suppress near-identical same-label boxes; no human reference used.
            order = torch.argsort(found['scores'], descending=True).tolist()
            chosen = []
            for i in order:
                box = found['boxes'][i].tolist(); label = found['text_labels'][i]
                if excluded is not None:
                    x1,y1 = np.maximum(0, np.floor(box[:2]).astype(int))
                    x2,y2 = np.minimum([w,h], np.ceil(box[2:]).astype(int))
                    if x2 > x1 and y2 > y1 and excluded[y1:y2,x1:x2].mean() >= .9:
                        diagnostics['ignored_ui_detections'] += 1
                        continue
                if any(label == old['label'] and box_iou(box, old['box']) > .8 for old in chosen): continue
                chosen.append({'box': box, 'label': label, 'detection_score': float(found['scores'][i])})
                if len(chosen) >= 32: break
            if self.sam_device == 'cpu':
                timings['embedding_s'] = self._embed(rgb)
            began = time.monotonic()
            for item in chosen:
                masks, scores, _ = self.sam.predict(box=np.asarray(item['box']), multimask_output=True)
                selected = int(np.argmax(scores))
                item.update(mask=masks[selected].astype(bool), mask_score=float(scores[selected]),
                            candidate_scores=[float(s) for s in scores], selected=selected)
                if optimize and item['detection_score'] >= .3 and any(t in item['label'] for t in ('fence', 'post', 'spike', 'rock')):
                    box = expanded_box(item['box'], (h, w))
                    refined, quality, _ = self.sam.predict(box=box, multimask_output=True)
                    best = int(np.argmax(quality))
                    candidate = refined[best].astype(bool)
                    check = refinement_check(item['mask'], candidate, item['mask_score'], float(quality[best]))
                    item['refinement'] = dict(check, prompt_box=box.tolist(), candidate_scores=[float(s) for s in quality])
                    diagnostics['refinements'].append(dict(label=item['label'], **check))
                    if check['accepted']:
                        item['mask'] |= candidate
            self._sync()
            timings['instance_masks_s'] = time.monotonic()-began
            began = time.monotonic()
            if local_detail:
                for item in chosen:
                    if item['detection_score'] < .35 or not any(s in item['label'] for s in ('fence', 'post', 'spike')):
                        continue
                    # Detail crop derives only from automatic box, never labels.
                    x1,y1,x2,y2 = item['box']; pad = .4*max(x2-x1,y2-y1)
                    l,t = max(0,int(x1-pad)),max(0,int(y1-pad))
                    r,b = min(w,int(x2+pad)+1),min(h,int(y2+pad)+1)
                    self.sam.set_image(rgb[t:b,l:r])
                    masks,scores,_ = self.sam.predict(box=np.asarray(item['box'])-[l,t,l,t],multimask_output=True)
                    selected = int(np.argmax(scores))
                    expanded = np.zeros((h,w),bool); expanded[t:b,l:r] = masks[selected]
                    item.update(detail_crop=[l,t,r,b], full_frame_mask_score=item['mask_score'],
                        mask=expanded,mask_score=float(scores[selected]),candidate_scores=[float(s) for s in scores],selected=selected)
            timings['detail_s'] = time.monotonic()-began
            began = time.monotonic()
            labels,confidence,margin = self._semantics(image,(h,w))
            timings['semantics_s'] = time.monotonic()-began
            raw_labels,raw_confidence=labels,confidence
            review_sources=None
            if review_surfaces:
                from .surface_review import needs_surface_review,reconcile_surfaces
                diagnostics['surface_review']=dict(triggered=False)
                if needs_surface_review(labels,confidence,margin,self.semantic.config.id2label,chosen,excluded):
                    began=time.monotonic()
                    gray=cv2.cvtColor(cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY),cv2.COLOR_GRAY2RGB)
                    review=self._semantics(Image.fromarray(gray),(h,w))
                    labels,confidence,margin,review_sources,details=reconcile_surfaces(
                        labels,confidence,margin,review,self.semantic.config.id2label,chosen,excluded)
                    timings['surface_review_s']=time.monotonic()-began
                    diagnostics['surface_review']=dict(details,triggered=True)
            verified = None
            if optimize:
                began = time.monotonic()
                initial, _, _ = fuse_masks(labels, confidence, margin, self.semantic.config.id2label, chosen, excluded=excluded)
                verified = np.zeros((h, w), bool)
                for point in ground_seeds(initial):
                    masks, scores, _ = self.sam.predict(point_coords=np.asarray([point]),
                        point_labels=np.ones(1, dtype=np.int32), multimask_output=True)
                    selected = int(np.argmax(scores)); mask = masks[selected].astype(bool)
                    accepted = bool(np.isfinite(scores[selected]) and scores[selected] >= .9 and mask[point[1], point[0]])
                    diagnostics['ground_proposals'].append(dict(point=list(point), score=float(scores[selected]), accepted=accepted))
                    if accepted:
                        verified |= mask
                self._sync()
                timings['ground_verification_s'] = time.monotonic()-began
        ground, solid, unknown = fuse_masks(labels, confidence, margin, self.semantic.config.id2label, chosen,
                                            verified_ground=verified, excluded=excluded)
        occluded, water, forbidden = navigation_evidence(labels, confidence,
            self.semantic.config.id2label, chosen, ground, solid, unknown, excluded)
        observed=SegmentedMap(ground, solid, unknown, chosen, labels, confidence, timings, diagnostics,
                            verified if verified is not None else np.zeros((h,w), bool), occluded, water, forbidden,
                            raw_labels,raw_confidence,review_sources)
        if review_portals:
            from .portal_review import inspect_portal_detail
            if local_detail:self.sam.set_image(rgb)
            observed=inspect_portal_detail(self,rgb,observed,excluded)
        return observed

    def _sync(self):
        if self.sam_device == 'mps':
            self.torch.mps.synchronize()


def box_iou(a, b):
    width = max(0, min(a[2], b[2])-max(a[0], b[0]))
    height = max(0, min(a[3], b[3])-max(a[1], b[1]))
    intersection = width*height
    return intersection/max(1, (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection)
