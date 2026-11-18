"""Persist measured calibration evidence, replay it, then require a NEW image fit.

No saved route, guessed scale or cached obstacle map is execution authority.
Historical timestamps stay historical. Restore time only bounds in-memory reuse.
"""
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile

import cv2
import numpy as np

from .automatic_ground import AutomaticGround
from .ground_projection import ProjectionError,ProjectionFrame
from .surface_height import height_matches

MAX_AGE_S = 6*3600


def algorithm_digest():
    root=Path(__file__).parent
    digest=hashlib.sha256()
    for name in ('automatic_ground.py','ground_tracking.py','ground_calibration.py',
                 'ground_projection.py','surface_height.py','calibration_cache.py'):
        digest.update((root/name).read_bytes())
    return digest.hexdigest()


def image_digest(rgb):
    return hashlib.sha256(cv2.imencode('.png',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))[1].tobytes()).hexdigest()


def save_cache(path,calibrator,*,created_at,provenance='live_worker'):
    if (calibrator.reference is None or len(calibrator.training)<2
            or len(calibrator.validation)<2 or not 7<=len(calibrator.measurements)<=12
            or not math.isfinite(created_at)):
        raise ProjectionError('calibration_cache_unverified')
    arrays={};frames=[]
    for i,(frame,rgb,ground) in enumerate(calibrator.measurements):
        if image_digest(rgb)!=frame.image_sha256:
            raise ProjectionError('calibration_cache_image_mismatch')
        frames.append(asdict(frame));arrays[f'image{i}']=rgb;arrays[f'ground{i}']=ground
    metadata=dict(schema=1,algorithm=algorithm_digest(),created_at=created_at,
        provenance=provenance,frames=frames,reference_id=calibrator.reference.frame.frame_id)
    arrays['metadata']=np.array(json.dumps(metadata))
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.calibration-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:np.savez_compressed(stream,**arrays)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cache(path,current,*,now,wall_time):
    """Replay the evidence only. Caller MUST update with current image before use."""
    path=Path(path)
    if path.stat().st_size>50_000_000:raise ProjectionError('calibration_cache_size')
    with np.load(path,allow_pickle=False) as archive:
        metadata=json.loads(str(archive['metadata']))
        if metadata.get('schema')!=1 or metadata.get('algorithm')!=algorithm_digest():
            raise ProjectionError('calibration_cache_version')
        if not 0<=wall_time-metadata['created_at']<=MAX_AGE_S:
            raise ProjectionError('calibration_cache_expired')
        frames=metadata['frames']
        if not 7<=len(frames)<=12:raise ProjectionError('calibration_cache_unverified')
        ref=frames[-1]
        if (current.map_id!=ref['map_id'] or current.floor_id!=ref['floor_id']
                or list(current.size)!=ref['size']
                or list(current.world_size)!=ref['world_size']
                or list(current.camera_signature[:2])!=ref['camera_signature'][:2]
                or math.hypot(*[(b-a)*s for a,b,s in zip(ref['player_position'],
                                current.player_position,current.world_size)])>8):
            raise ProjectionError('calibration_cache_scene_changed')
        restored=AutomaticGround();previous=None;ids=set()
        for i,values in enumerate(frames):
            values=dict(values)
            for key in ('size','world_size','player_position','camera_signature'):values[key]=tuple(values[key])
            frame=ProjectionFrame(**values);rgb=archive[f'image{i}'];ground=archive[f'ground{i}']
            if (frame.frame_id in ids or previous is not None and frame.captured_at<=previous
                    or frame.size!=current.size or ground.shape!=(current.size[1],current.size[0])
                    or ground.dtype!=np.uint8 or rgb.dtype!=np.uint8
                    or rgb.shape!=(current.size[1],current.size[0],3)
                    or image_digest(rgb)!=frame.image_sha256):
                raise ProjectionError('calibration_cache_evidence_invalid')
            ids.add(frame.frame_id);previous=frame.captured_at
            observed=SimpleNamespace(ground=ground>0,semantic_confidence=np.ones(ground.shape))
            # Historical evidence validation, not a current action or new sample.
            projection,_=restored.update(frame,rgb,observed,now=frame.captured_at+.1)
        if (projection is None or restored.reference.frame.frame_id!=metadata['reference_id']
                or restored.reference.frame.frame_id!=frames[-1]['frame_id']):
            raise ProjectionError('calibration_cache_unverified')
        displacement=[(b-a)*s for a,b,s in zip(ref['player_position'],
                            current.player_position,current.world_size)]
        if not height_matches(displacement,current.height-ref['height'],
                              restored.proof(current)['surface_height']):
            raise ProjectionError('calibration_cache_scene_changed')
    restored.restored_at=now
    restored.cache_id=hashlib.sha256(path.read_bytes()).hexdigest()
    return restored
