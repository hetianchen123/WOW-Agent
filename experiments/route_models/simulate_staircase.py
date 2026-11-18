"""Ray-rendered staircase for testing geometry at a floor/tread transition.

World surfaces and the player support height are used by the renderer only.
The estimator receives RGB, explicit simulation masks, and native-like poses.
These are not learned labels and cannot measure recognition accuracy.
"""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bot.perception.automatic_ground import AutomaticGround
from bot.perception.calibration_cache import image_digest
from bot.perception.ground_projection import ProjectionFrame, ProjectionError


class StairScene:
    """Horizontal treads, vertical risers, a bottom floor and a top landing."""
    def __init__(self, *, rise=.25, depth=.5, steps=6, width=3., size=(800, 600)):
        self.rise, self.depth, self.steps, self.width, self.size = rise, depth, steps, width, size
        self.texture = cv2.GaussianBlur(np.random.default_rng(74).integers(
            20, 235, (1400, 1400, 3), dtype=np.uint8), (3, 3), 0)

    def height(self, position):
        x, y = position
        if abs(x) > self.width/2 or y >= 0:
            return 0.
        return float(min(self.steps, math.ceil(-y/self.depth))*self.rise)

    def render(self, position, yaw):
        w, h = self.size
        pitch, distance = math.radians(60), 7.
        player = np.array([*position, self.height(position)])
        forward = np.array([math.sin(yaw)*math.cos(pitch), -math.cos(yaw)*math.cos(pitch), -math.sin(pitch)])
        right = np.array([math.cos(yaw), math.sin(yaw), 0.])
        rotation = np.stack([right, np.cross(right, forward), forward])
        eye = player+np.array([0, 0, 1.2])-forward*distance
        yy, xx = np.mgrid[:h, :w]
        focal = w/(2*math.tan(math.radians(30)))
        rays = np.stack([(xx-w/2)/focal, (yy-h/2)/focal, np.ones_like(xx)], axis=-1)@rotation
        depth = np.full((h, w), np.inf)
        xyz = np.full((h, w, 3), np.nan)
        labels = np.zeros((h, w), np.uint8)
        surfaces = np.full((h, w), -1, np.int16)

        def plane(axis, value, bounds, label, identity):
            direction = rays[:, :, axis]
            t = np.divide(value-eye[axis], direction, out=np.full((h, w), np.inf),
                          where=np.abs(direction)>1e-8)
            with np.errstate(invalid='ignore'):
                points = eye+rays*t[:, :, None]
            visible = (t > 0) & (t < depth)
            for dimension, low, high in bounds:
                visible &= (points[:, :, dimension] >= low) & (points[:, :, dimension] <= high)
            depth[visible] = t[visible]
            xyz[visible] = points[visible]
            labels[visible] = label
            surfaces[visible] = identity

        # The bottom plane also extends around the sides of the stairs, so a
        # view includes several tempting but non-coplanar ground regions.
        plane(2, 0., [(0, -8, 8), (1, -8, 8)], 1, 0)
        for i in range(self.steps):
            z = (i+1)*self.rise
            plane(2, z, [(0, -self.width/2, self.width/2),
                         (1, -(i+1)*self.depth, -i*self.depth)], 2, i+1)
            plane(1, -i*self.depth, [(0, -self.width/2, self.width/2),
                                    (2, i*self.rise, z)], 2, 100+i)
        plane(2, self.steps*self.rise,
              [(0, -self.width/2, self.width/2), (1, -8, -self.steps*self.depth)], 1, self.steps+1)
        # Solid side walls expose drops as boundaries, not walkable floor.
        for side in (-1, 1):
            for i in range(self.steps):
                plane(0, side*self.width/2,
                      [(1, -(i+1)*self.depth, -i*self.depth), (2, 0, (i+1)*self.rise)], 3, 200+i)
        uv = np.nan_to_num(xyz[:, :, :2]).copy()
        riser = (surfaces >= 100) & (surfaces < 200)
        uv[:, :, 1][riser] = xyz[:, :, 2][riser]
        indices = np.rint(uv*90+700).astype(int) % 1400
        rgb = self.texture[indices[:, :, 1], indices[:, :, 0]].copy()
        rgb[riser] = (rgb[riser].astype(float)*.65).astype(np.uint8)
        rgb[labels == 0] = 0
        return rgb, labels, surfaces, xyz


def frames(scene, *, start=(0., .2), public_height=True, mask_mode='all_surfaces'):
    # Camera yaw is measured separately, then physical forward/right/back/left
    # displacements cross the first riser and return. World axes stay fixed.
    positions = [start, start, start, (start[0], start[1]-.55),
                 (start[0]+.55, start[1]-.55), (start[0]+.55, start[1]), start]
    for i, position in enumerate(positions):
        yaw = (0., .18, -.18)[i] if i < 3 else -.18
        rgb, labels, surfaces, xyz = scene.render(position, yaw)
        ground = np.isin(labels, [1, 2])
        if mask_mode == 'flat_only':
            ground = labels == 1
        elif mask_mode == 'support_oracle':
            # Diagnostic upper bound only, unavailable to the runtime model.
            ground = (labels != 0) & (np.abs(xyz[:, :, 2]-scene.height(position)) < 1e-6)
        elif mask_mode != 'all_surfaces':
            raise ValueError('unknown mask mode')
        empty = np.zeros_like(ground)
        masks = SimpleNamespace(ground=ground, semantic_confidence=np.ones(ground.shape),
            obstacles=labels == 3, occluded=empty, forbidden=labels == 3, water=empty,
            instances=[], timings={}, semantic_labels=labels)
        frame = ProjectionFrame(str(i), image_digest(rgb), scene.size, 2451,
            'stair-simulation', (1000, 1000), (.4+position[0]/1000, .5+position[1]/1000),
            None, scene.height(position) if public_height else 0., -yaw, (4, 32, 1), 100+i)
        yield frame, rgb, masks, xyz, surfaces


def evaluate(scene, **options):
    cal = AutomaticGround()
    stages = []
    try:
        for frame, rgb, masks, xyz, surfaces in frames(scene, **options):
            projection, evidence = cal.update(frame, rgb, masks, now=frame.captured_at+.1)
            stages.append(evidence['stage'])
        if projection is None:
            return dict(options, ready=False, stages=stages, reason='not_enough_measurements')
        # Evaluate held-out image locations against ray/surface intersections.
        yy, xx = np.mgrid[:scene.size[1]:17, :scene.size[0]:17]
        valid = np.isfinite(xyz[yy, xx]).all(axis=-1)
        pixels = np.c_[xx[valid], yy[valid]]
        selected = np.array([projection.contains(tuple(map(float, p))) for p in pixels])
        pixels = pixels[selected]
        truth = xyz[pixels[:, 1], pixels[:, 0], :2]-np.asarray(options.get('start', (0., .2)))
        predicted = np.array([projection.delta(tuple(map(float, p))) for p in pixels])
        error = np.linalg.norm(predicted-truth, axis=1)
        allowed = masks.ground[pixels[:,1],pixels[:,0]]
        nearby = allowed & (np.linalg.norm(truth,axis=1)<=2)
        return dict(options, ready=True, stages=stages, proof=evidence,
                    truth_max_error_yards=float(error.max()), truth_median_error_yards=float(np.median(error)),
                    truth_allowed_ground_max_error_yards=float(error[allowed].max()) if allowed.any() else None,
                    truth_near_ground_max_error_yards=float(error[nearby].max()) if nearby.any() else None)
    except ProjectionError as exc:
        return dict(options, ready=False, stages=stages, reason=str(exc))


def trial(scene, *, start=(0., .2), public_height=True):
    from bot.control.stair_trial import StairTrial
    from experiments.route_models.segmentation_worker import stair_job
    # Establish a camera anchor by real rendered floor translations first;
    # the later stair observation cannot silently replace it with a lower floor.
    floor=AutomaticGround()
    for f,image,observed,_,_ in frames(scene,start=(0.,1.5),public_height=public_height,mask_mode='flat_only'):
        floor.update(f,image,observed,now=f.captured_at+.1)
    if floor.reference is None:
        return dict(ready=False,reason='floor_anchor_unmeasured',steps=[])
    prior=dict(point=floor.anchor.tolist(),error_px=floor.anchor_error,size=list(scene.size),
        camera=[4,32],source_frame='prior-floor-motion')
    cal = AutomaticGround()
    samples = frames(scene, start=start, public_height=public_height, mask_mode='flat_only')
    job=dict(quest_id=12345,phase='READY',stair_trial={'advance':None,'moved':0,'anchor_reference':prior},
        building_scene=dict(schema=1,surface='unknown',doorways=[],executable=False))
    for _ in range(3):
        frame, rgb, masks, _, _ = next(samples)
        try:stair_job(cal,frame,rgb,masks,job,{1:'floor',2:'stairs',3:'wall'},now=frame.captured_at+.1)
        except ProjectionError as exc:return dict(ready=False,reason=str(exc),steps=[])
    nav = SimpleNamespace(position=frame.player_position, height=frame.height, sequence=3,
        world_size=frame.world_size, camera=SimpleNamespace(view=4, zoom=32, request=0),
        camera_epoch=1, strafe_verified=True)
    control = StairTrial(103, nav, 'ccw_west', deadline=180,anchor_reference=prior)
    position = np.asarray(start, float)
    now = 103.
    rows = []
    for i in range(28):
        rgb, labels, _, _ = scene.render(position, -.18)
        empty = np.zeros(labels.shape, bool)
        observed = SimpleNamespace(ground=labels == 1, semantic_labels=labels,
            semantic_confidence=np.ones(labels.shape), instances=[], obstacles=labels == 3,
            forbidden=labels == 3, water=empty, occluded=empty)
        try:
            current=replace(frame,frame_id=f'trial-{i}',image_sha256=image_digest(rgb),captured_at=now,
                            player_position=nav.position,height=nav.height)
            job['stair_trial']=control.job()
            result=stair_job(cal,current,rgb,observed,job,{1:'floor',2:'stairs',3:'wall'},now=now+.1)
        except ProjectionError as exc:
            return dict(ready=False, reason=str(exc), steps=rows)
        nav.sequence += 1
        cmd = control.accept(result, now, nav, .18, coupled=True)
        rows.append(dict(kind=result['kind'], key=cmd.tap, position=position.tolist(),
                         height=scene.height(position), outcome=cmd.outcome))
        if cmd.outcome:
            return dict(ready=cmd.outcome == 'stair_surface_observed', reason=cmd.outcome,
                        steps=rows, moved_yards=control.total, final_height=scene.height(position))
        if cmd.tap:
            angle = .18+{'w':0, 's':math.pi, 'q':math.pi/2, 'e':-math.pi/2}[cmd.tap]
            position += np.array([-math.sin(angle), -math.cos(angle)])*7*cmd.hold_s
            nav.position = (.4+float(position[0])/1000, .5+float(position[1])/1000)
            nav.height = scene.height(position) if public_height else 0.
            nav.sequence += 1
            control.feedback(now+.31, nav, .18)
            nav.sequence += 1
            receipt = control.feedback(now+.41, nav, .18)
            if receipt.outcome != 'stair_step_observed':
                return dict(ready=False, reason=receipt.outcome, steps=rows)
        now += 1
    return dict(ready=False, reason='simulation_budget', steps=rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    scene = StairScene()
    rgb, _, _, _ = scene.render((0., .2), 0.)
    cv2.imwrite(str(args.output/'stairs.png'), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    rows = [evaluate(scene, public_height=height, mask_mode=mode)
            for height in (True, False) for mode in ('flat_only', 'all_surfaces', 'support_oracle')]
    report = dict(source='synthetic_3d_ray_rendering', cases=rows,
                  trials=[trial(scene, public_height=h) for h in (True, False)],
                  model_recognition_verified=False, game_inputs_sent=False)
    (args.output/'report.json').write_text(json.dumps(report, indent=2))
    for row in rows:
        print(json.dumps({k: v for k, v in row.items() if k != 'proof'}))
    for row in report['trials']:
        print(json.dumps({k: v if k != 'steps' else len(v) for k, v in row.items()}))


if __name__ == '__main__':
    main()
