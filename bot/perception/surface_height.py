"""Local native-height consistency; it never supplies a room or floor identity.

The image/native-XY fit remains mandatory. Constant public height values carry
no slope information. Variable readings are fitted on training movements and
checked using the same independent movements that validate image geometry.
"""
import math

import numpy as np

from .ground_projection import ProjectionError, finite_pair


def bounded_height_change(distance, delta):
    return (type(distance) in (int,float) and type(delta) in (int,float)
            and math.isfinite(distance) and math.isfinite(delta) and 0<=distance<=1.25
            and abs(delta)<=min(.6,1.25*distance+.15)+1e-8)


def fit_height_gradient(pairs):
    xy=np.asarray([p.delta for p in pairs],float)
    z=np.asarray([p.height_delta for p in pairs],float)
    gradient,_,rank,_=np.linalg.lstsq(xy,z,rcond=None)
    if rank<2 or not np.isfinite(gradient).all() or np.linalg.norm(gradient)>1.25:
        raise ProjectionError('auto_surface_height_fit_invalid')
    if np.max(np.abs(xy@gradient-z))>.15:
        raise ProjectionError('auto_surface_height_fit_error')
    return gradient


def height_error(gradient,pair):
    return abs(float(np.dot(gradient,pair.delta))-pair.height_delta)


def height_proof(gradient,training,validation):
    error=max((height_error(gradient,p) for p in validation),default=0.)
    varied=any(abs(p.height_delta)>.1 for p in [*training,*validation])
    return dict(schema=1,source='native_height_independent_movements',
        variation_observed=varied,gradient=gradient.tolist() if varied else None,
        validation_error_yards=error,fit_movements=len(training),validation_movements=len(validation),
        level_id=None)


def height_matches(delta_xy,delta_height,proof):
    """Validate a new pose, retaining the old conservative rule without a model."""
    if not finite_pair(delta_xy) or type(delta_height) not in (int,float) or not math.isfinite(delta_height):
        return False
    if proof is None:return abs(delta_height)<=.25
    if (not isinstance(proof,dict) or proof.get('schema')!=1
            or proof.get('source')!='native_height_independent_movements'
            or type(proof.get('fit_movements')) is not int or proof['fit_movements']<2
            or type(proof.get('validation_movements')) is not int or proof['validation_movements']<2
            or type(proof.get('validation_error_yards')) not in (int,float)
            or not 0<=proof['validation_error_yards']<=.15):return False
    if proof.get('variation_observed') is False:return abs(delta_height)<=.25
    gradient=proof.get('gradient')
    if (proof.get('variation_observed') is not True or not finite_pair(gradient)
            or math.hypot(*gradient)>1.25):return False
    return abs(sum(a*b for a,b in zip(delta_xy,gradient))-delta_height)<=max(.15,proof['validation_error_yards']+.1)+1e-8
