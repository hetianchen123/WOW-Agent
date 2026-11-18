"""Physical pinhole-camera fixtures for locally inclined game-like ground.

This renders a static textured 3-D plane and a following camera. The calibration
receives only RGB, masks and simulated native positions; ground-truth projection
is retained solely for independent evaluation. No game input or model inference.
"""
import argparse
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.perception.automatic_ground import AutomaticGround
from bot.perception.ground_projection import ProjectionFrame, ProjectionError
from bot.perception.calibration_cache import image_digest


def camera_plane(position,yaw,slope,size=(800,600)):
    w,h=size;pitch=math.radians(60);distance=7.
    player=np.array([*position,float(np.dot(slope,position))])
    target=player+np.array([0,0,1.2])
    forward=np.array([math.sin(yaw)*math.cos(pitch),-math.cos(yaw)*math.cos(pitch),-math.sin(pitch)])
    right=np.array([math.cos(yaw),math.sin(yaw),0.])
    down=np.cross(right,forward)
    rotation=np.stack([right,down,forward])
    eye=target-forward*distance
    intrinsic=np.array([[w/(2*math.tan(math.radians(30))),0,w/2],
                        [0,w/(2*math.tan(math.radians(30))),h/2],[0,0,1.]])
    plane=np.array([[1,0,0],[0,1,0],[slope[0],slope[1],0.]])
    plane[:,2]=-eye
    world_to_image=intrinsic@rotation@plane
    relative=np.eye(3);relative[:2,2]=-np.asarray(position)
    return world_to_image,relative@np.linalg.inv(world_to_image),float(player[2])


def frames(slope=(0.,0.), *, public_height=True):
    rng=np.random.default_rng(42)
    texture=cv2.GaussianBlur(rng.integers(20,235,(1400,1400,3),np.uint8),(3,3),0)
    texture_plane=np.array([[.025,0,-17.5],[0,.025,-17.5],[0,0,1.]])
    states=[(0.,(0.,0.)),(.18,(0.,0.)),(-.18,(0.,0.)),
            (-.18,(.55,0.)),(-.18,(.55,.55)),(-.18,(0.,.55)),(-.18,(0.,0.))]
    for index,(angle,position) in enumerate(states):
        h,truth,height=camera_plane(position,angle,slope)
        rgb=cv2.warpPerspective(texture,h@texture_plane,(800,600))
        ground=cv2.warpPerspective(np.ones(texture.shape[:2],np.uint8),h@texture_plane,
                                   (800,600),flags=cv2.INTER_NEAREST).astype(bool)
        empty=np.zeros_like(ground)
        masks=SimpleNamespace(ground=ground,semantic_confidence=np.ones(ground.shape),
            obstacles=empty,occluded=empty,water=empty,forbidden=empty,instances=[],timings={})
        frame=ProjectionFrame(str(index),image_digest(rgb),(800,600),2451,'inclined-test-plane',(1000,1000),
            (.4+position[0]/1000,.5+position[1]/1000),None,height if public_height else 0.,
            angle,(4,32,1),100+index)
        yield frame,rgb,masks,truth


def evaluate(slope,*,public_height=True,calibrator=None):
    calibrator=calibrator or AutomaticGround()
    stages=[];projection=None
    try:
        for frame,rgb,masks,truth in frames(slope,public_height=public_height):
            projection,evidence=calibrator.update(frame,rgb,masks,now=frame.captured_at+.1)
            stages.append(evidence['stage'])
        checks=np.array([[300,320,1],[500,320,1],[400,440,1]],float)
        expected=checks@truth.T;expected=expected[:,:2]/expected[:,2,None]
        actual=checks@projection.matrix.T if projection is not None else None
        error=float(np.max(np.linalg.norm(actual[:,:2]/actual[:,2,None]-expected,axis=1))) if actual is not None else None
        return dict(slope=list(slope),public_height=public_height,stages=stages,ready=projection is not None,
                    independent_truth_error_yards=error,proof=evidence)
    except ProjectionError as exc:
        return dict(slope=list(slope),public_height=public_height,stages=stages,ready=False,reason=str(exc))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    rows=[evaluate(slope,public_height=height) for slope in ((0,0),(.25,0),(.6,0),(.35,.35))
          for height in (True,False)]
    (args.output/'report.json').write_text(json.dumps(dict(cases=rows,source='synthetic_3d_pinhole_camera',
        model_recognition_verified=False,game_inputs_sent=False),indent=2))
    for row in rows:print(json.dumps(row))


if __name__=='__main__':main()
