"""Bounded stair trials with native receipts; no image/world point conversion."""
import math
import numpy as np

from .navigation import heading_error, compass_from_delta
from .quest_travel import TravelCommand
from ..perception.ground_projection import finite_pair
from ..perception.surface_height import bounded_height_change


class StairTrial:
    def __init__(self, now, nav, convention, *, deadline, anchor_reference=None):
        # Share the original building deadline and leave time for remeasurement.
        # The owner still enforces its 60 s without NEW spatial progress; model
        # calls never reset that clock. Do not abort a moving staircase solely
        # because a second, shorter timer expired halfway through it.
        self.started, self.deadline = now, deadline-8
        self.origin = nav.position
        self.convention = convention
        self.advance = None
        self.pulse = None
        self.total = self.pulses = self.observations = 0
        self.sequence = None
        self.samples = 0
        self.surface_candidate = None
        self.camera = self.heading = None
        self.anchor_reference = anchor_reference

    def job(self):
        return dict(advance=self.advance, moved=self.total, anchor_reference=self.anchor_reference)

    @staticmethod
    def measured_anchor(plan, frame):
        """Retain only a previously measured viewport anchor, never its route."""
        try:
            matrix=np.asarray(plan['projection_matrix'],float)
            if matrix.shape!=(3,3) or not np.isfinite(matrix).all() or frame.camera_signature[0]!=4:
                return None
            point=np.linalg.solve(matrix,[0.,0.,1.]);point=point[:2]/point[2]
            error=plan['registration']['anchor_error_px']
            if not np.isfinite(point).all() or type(error) not in (int,float) or not 0<=error<=5:
                return None
            return dict(point=point.tolist(),error_px=error,size=list(frame.size),
                camera=list(frame.camera_signature[:2]),source_frame=frame.frame_id)
        except (KeyError,ValueError,TypeError,np.linalg.LinAlgError):
            return None

    def accept(self, result, now, nav, heading, *, coupled):
        if now >= self.deadline or self.pulses >= 24 or self.observations >= 28 or self.total >= 8:
            return TravelCommand(outcome='stair_trial_budget')
        camera = (nav.camera, nav.camera_epoch)
        if (self.pulse is not None or not coupled or not nav.strafe_verified or nav.camera is None
                or nav.camera.view != 4 or getattr(nav.camera, 'paused', False)
                or self.camera is not None and (camera != self.camera
                    or abs(heading_error(self.heading, heading)) > math.radians(1))):
            return TravelCommand(outcome='stair_trial_camera_unconfirmed')
        if (result.get('advance') not in {'w', 's', 'q', 'e'}
                or self.advance is not None and result['advance'] != self.advance
                or not finite_pair(result.get('anchor')) or result.get('metric_route') is not False
                or result.get('requires_new_measurement') is not True or result.get('level_id') is not None):
            return TravelCommand(outcome='stair_trial_evidence_invalid')
        proof=result.get('anchor_validation') or {}
        prior=self.anchor_reference
        if (proof.get('schema')!=1 or proof.get('current_frame')!=result.get('frame_id')
                or type(proof.get('anchor_error_px')) not in (int,float) or not 0<=proof['anchor_error_px']<=5
                or not (proof.get('source')=='prior_motion_anchor_and_orbits'
                    and prior is not None and isinstance(proof.get('prior_frame'),str)
                    and bool(proof['prior_frame']) and proof.get('prior_frame')==prior.get('source_frame')
                    or proof.get('source')=='current_independent_motion'
                    and type(proof.get('fit_movements')) is int and proof['fit_movements']>=2
                    and type(proof.get('validation_movements')) is int and proof['validation_movements']>=2)):
            return TravelCommand(outcome='stair_trial_anchor_unconfirmed')
        self.camera, self.heading = camera, heading
        self.advance = result['advance']
        self.observations += 1
        if result['kind'] == 'stair_surface_candidate':
            if self.total < .4:
                return TravelCommand(outcome='stair_trial_evidence_invalid')
            previous = self.surface_candidate
            self.surface_candidate = (nav.position, nav.sequence, result['frame_id'])
            if (previous and previous[1] != nav.sequence and previous[2] != result['frame_id']
                    and math.hypot(*[(a-b)*s for a,b,s in zip(previous[0],nav.position,nav.world_size)]) <= .08):
                return TravelCommand(outcome='stair_surface_observed')
            return TravelCommand()
        if (result['kind'] != 'stair_probe' or result.get('key') not in {'w', 's', 'q', 'e'}
                or result.get('hold_s') != .04 or result.get('basis') != 'local_stair_corridor_trial'
                or self.deadline-now < 1):
            return TravelCommand(outcome='stair_trial_evidence_invalid')
        self.surface_candidate = None
        self.pulse = (now, nav.position, nav.height, nav.sequence, result['key'])
        self.sequence, self.samples = nav.sequence, 0
        self.pulses += 1
        return TravelCommand(tap=result['key'], hold_s=.04, precise=True)

    def feedback(self, now, nav, heading):
        at, origin, height, sequence, key = self.pulse
        if now >= self.deadline:
            return TravelCommand(outcome='stair_trial_budget')
        if ((nav.camera, nav.camera_epoch) != self.camera or getattr(nav.camera, 'paused', False)
                or abs(heading_error(self.heading, heading)) > math.radians(1)):
            return TravelCommand(outcome='probe_pose_changed')
        delta = [(b-a)*s for a,b,s in zip(origin,nav.position,nav.world_size)]
        moved = math.hypot(*delta)
        if moved > .6 or not bounded_height_change(moved, nav.height-height):
            return TravelCommand(outcome='probe_height_drop')
        if nav.sequence != self.sequence:
            self.sequence = nav.sequence
            self.samples += 1
        if now-at < .3 or self.samples < 2:
            return TravelCommand()
        self.pulse = None
        if moved < .08:
            return TravelCommand(outcome='stair_trial_blocked')
        left = math.pi/2 if self.convention == 'ccw_west' else -math.pi/2
        expected = self.heading+{'w':0, 's':math.pi, 'q':left, 'e':-left}[key]
        if abs(heading_error(expected, compass_from_delta(*delta, self.convention))) > math.radians(25):
            return TravelCommand(outcome='probe_pose_changed')
        self.total += moved
        return TravelCommand(outcome='stair_step_observed')
