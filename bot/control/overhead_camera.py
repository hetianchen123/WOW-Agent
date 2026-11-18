"""One bounded, receipt-checked F6 observation lease; no movement or pitch guessing."""
from ..perception.scene_scale import overhead_profile
from ..perception.travel_channel import CameraScene, CameraResult, TravelFlags
from .quest_travel import TravelCommand


class OverheadObservation:
    def __init__(self, *, reuse_current=False):
        self.request = None
        self.settle = None
        self.sequence = None
        self.sequence_at = None
        self.used = False
        self.done = False
        self.reuse_current = reuse_current

    def step(self, ctx, now, nav):
        if nav is None or nav.camera is None:
            return TravelCommand(outcome='escape_overhead_camera_unavailable')
        if nav.sequence != self.sequence:
            self.sequence, self.sequence_at = nav.sequence, now
        if self.sequence_at is not None and now-self.sequence_at > .75:
            return TravelCommand(outcome='probe_telemetry_stale')
        unsafe = (TravelFlags.IN_COMBAT | TravelFlags.FLYING | TravelFlags.FALLING |
                  TravelFlags.MOUNTED | TravelFlags.SWIMMING | TravelFlags.CASTING)
        if nav.flags & unsafe or nav.camera.paused or not nav.flags & TravelFlags.KEYS_VERIFIED:
            return TravelCommand(outcome='probe_interrupted')
        if not nav.overhead_camera_available:
            return TravelCommand(outcome='escape_overhead_preset_unconfigured')
        if self.done:
            return None if overhead_profile(nav.camera) else TravelCommand(outcome='probe_pose_changed')
        if self.reuse_current and self.request is None and self.settle is None and overhead_profile(nav.camera):
            # A freshly verified view is usable for measurement; do not switch
            # back to the obstructed facade merely because entry starts here.
            self.settle = now+.5, nav.sequence, nav.camera
            return TravelCommand()
        if self.settle is not None:
            at, sequence, camera = self.settle
            if nav.camera != camera:
                return TravelCommand(outcome='probe_pose_changed')
            if now >= at and nav.sequence != sequence:
                self.done = True
                return None
            return TravelCommand()
        if self.request is None:
            self.request = nav.camera.request, now, nav.sequence
            self.used = True
            ctx.log_event('quest_overhead_camera_requested', {'request':(nav.camera.request+1)%8,
                'view':4,'zoom_before':nav.camera.zoom,'composition_verified':False})
        baseline, began, sequence = self.request
        expected = (baseline+1)%8
        if now-began >= 8.6 or nav.camera.request not in {baseline, expected}:
            return TravelCommand(outcome='escape_overhead_camera_unconfirmed')
        if nav.camera.request == expected and nav.camera.result not in {CameraResult.IDLE, CameraResult.PENDING}:
            if nav.sequence == sequence:
                return TravelCommand()
            if nav.camera.applied != CameraScene.OVERHEAD or overhead_profile(nav.camera) is None:
                return TravelCommand(outcome='escape_overhead_camera_unconfirmed')
            self.settle = now+.5, nav.sequence, nav.camera
            ctx.log_event('quest_overhead_camera_received', {'request':expected,'view':nav.camera.view,
                'zoom':nav.camera.zoom,'composition_verified':False,'projection_calibrated':False})
            return TravelCommand()  # Releases F6 before the stable-frame interval.
        return TravelCommand(frozenset({'f6'}))


class FacadeObservation:
    """Restore the ordinary ground view so eaves do not hide a doorway.

    This is only framing. The frontal plane is measured again; overhead scales
    are not reused after changing view or pitch.
    """
    def __init__(self):
        self.request=None;self.settle=None;self.used=False;self.done=False

    def step(self,ctx,now,nav):
        camera=nav.camera
        if camera is None or not nav.camera_restore_available:
            return TravelCommand(outcome='building_facade_camera_unavailable')
        good=(camera.view in {2,5} and camera.zoom is not None and 20<=camera.zoom<=39
              and not camera.paused and camera.applied not in {CameraScene.OVERHEAD,CameraScene.UNKNOWN}
              and camera.result in {CameraResult.DISTANCE_VERIFIED,CameraResult.LIMITED})
        if self.done:return None if good else TravelCommand(outcome='probe_pose_changed')
        if self.request and now-self.request[0]>8.6:
            return TravelCommand(outcome='building_facade_camera_unconfirmed')
        if good and (self.request is None or nav.sequence!=self.request[2]
                     and camera.request==(self.request[1]+1)%8):
            if self.settle is None or self.settle[2]!=camera:
                self.settle=now,nav.sequence,camera
                return TravelCommand()
            if now-self.settle[0]>=.5 and nav.sequence!=self.settle[1]:
                self.done=True
                ctx.log_event('quest_building_facade_camera',dict(view=camera.view,zoom=camera.zoom,
                              projection_calibrated=False))
                return None
            return TravelCommand()
        if self.request is None:
            self.request=now,camera.request,nav.sequence;self.used=True
        return TravelCommand(frozenset({'f8'}))


class BuildingObservationRecovery:
    """One receipt-checked reframe, optionally a stationary look toward a cue.

    The hint selects only a look direction. It never supplies a movement target.
    A different view requires a new image, entrance detection and calibration.
    """
    def __init__(self, *, overhead, look_hint, convention):
        self.view=OverheadObservation() if overhead else FacadeObservation()
        self.hint,self.convention=look_hint,convention
        self.turn=None;self.turned=False
        self.settle=None

    def step(self,ctx,now,nav,heading):
        from .calibration_orbit import CalibrationOrbit
        command=self.view.step(ctx,now,nav)
        if command is not None:return command
        if self.hint in {'left','right'} and not self.turned:
            if self.turn is None:
                pixels=220 if self.hint=='left' else -220
                if self.convention!='ccw_west':pixels=-pixels
                self.turn=CalibrationOrbit(now,nav,heading,pixels,self.convention,keyboard=True)
                ctx.log_event('quest_building_look',dict(direction=self.hint,translation_authorized=False))
            command=self.turn.step(ctx,now,nav,heading)
            if command.outcome!='auto_orbit_ready':return command
            self.turned=True;self.settle=(now,nav.sequence,nav.position,heading)
            return TravelCommand()
        if self.settle:
            at,seq,position,angle=self.settle
            from .navigation import heading_error
            if position!=nav.position or abs(heading_error(angle,heading))>.02:
                return TravelCommand(outcome='probe_pose_changed')
            if now-at<.5 or nav.sequence==seq:return TravelCommand()
        return None
