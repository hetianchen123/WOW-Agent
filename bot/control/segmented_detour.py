"""Task-owned segmented approach: capture -> local plan -> one measured pulse.

The artifact remains non-executable. This controller separately leases one pulse
only after fresh native state, calibration and pose checks. No cloud fallback.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from threading import Event
import time
import atexit
from uuid import uuid4

import cv2

from ..core.paths import ROOT,DATA_DIR
from ..perception.ground_projection import ProjectionFrame
from ..perception.travel_channel import TravelFlags,TaskFlags,LifeState
from ..perception.vision_worker import VisionWorker
from .overhead_camera import OverheadObservation, FacadeObservation, BuildingObservationRecovery
from ..perception.building_scene import BUILDING_REOBSERVE
from .navigation import compass_from_delta,heading_error
from .quest_travel import TravelCommand
from .calibration_orbit import CalibrationOrbit
from .route_window import RouteWindow
from .building_relocation import TraceRelocation, BuildingBackoffProbe, TurnawayRelocation, local_backoff_evidence
from .building_memory import BuildingRouteMemory, PassageLook
from .stair_trial import StairTrial
from ..perception.surface_height import bounded_height_change, height_matches


_worker=None


def close_local_worker():
    if _worker is not None:_worker.close()


atexit.register(close_local_worker)


def local_worker():
    global _worker
    if _worker is None or _worker.closed.is_set():
        _worker=VisionWorker([str(DATA_DIR/'segmentation/.venv/bin/python'),
            str(ROOT/'experiments/route_models/segmentation_worker.py')],DATA_DIR/'segmentation/worker.log')
    return _worker


def preflight(config,nav=None):
    if not config or not config.enabled:
        raise ValueError('segmentation_disabled')
    for name in ('models','checkpoint'):
        if not (ROOT/getattr(config,name)).is_file():
            raise ValueError('segmentation_'+name+'_missing')
    if nav is not None:
        if nav.destination is None or nav.destination_map!=nav.map_id:
            raise ValueError('segmentation_search_destination_missing')
        if not nav.strafe_verified:
            raise ValueError('segmentation_strafe_bindings_required')
        if (not nav.calibration_orbit_available
                and not (nav.overhead_camera_available and nav.flags & TravelFlags.KEYS_VERIFIED)):
            raise ValueError('segmentation_orbit_binding_required')
    if not (DATA_DIR/'segmentation/.venv/bin/python').is_file():
        raise ValueError('segmentation_environment_missing')
    return 'automatic-local-plane'


class LocalRouteRequest:
    def __init__(self, png, frame, job):
        self.cancelled=Event()
        job = deepcopy(dict(job, frame=asdict(frame)))
        self.pool=ThreadPoolExecutor(max_workers=1)

        def guard():
            if self.cancelled.is_set():raise ValueError('segmentation_cancelled')

        def run():
            guard()
            if hashlib.sha256(png).hexdigest() != frame.image_sha256:
                raise ValueError('segmentation_capture_encoding_mismatch')
            folder = DATA_DIR / 'terrain' / ('segmented-' + frame.frame_id)
            folder.mkdir(parents=True, exist_ok=False)
            path = folder / 'image.png'
            path.write_bytes(png)
            (folder / 'request.json').write_text(json.dumps(job, indent=2))
            guard()
            return local_worker().inspect([path], target=job, guard=guard, timeout=20)[0]

        self.future = self.pool.submit(run)

    def poll(self):
        return self.future.result() if self.future.done() else None

    def close(self):
        self.cancelled.set()
        self.pool.shutdown(wait=False,cancel_futures=True)


class SegmentedDetour:
    def __init__(self,now,nav,router,convention,config,capture,*,deadline,request_factory=LocalRouteRequest,
                 goal_mode='npc',ground_trace=None,overview_capture=None,overview_request_factory=None,
                 travel_goal=None):
        if goal_mode not in {'npc','travel','building_entry','building_cross','building_search'}:
            raise ValueError('invalid_goal_mode')
        if goal_mode=='travel' and travel_goal is None:raise ValueError('auto_travel_destination_missing')
        self.goal_mode=goal_mode
        self.travel_goal=travel_goal
        self.crossing_baseline=None
        self.crossing_origin=None
        self.crossing_confirmed=None
        self.building_memory=BuildingRouteMemory(nav.world_size)
        self.passages=self.building_memory.passages
        self.return_passage=None;self.return_look=None;self.return_looked=set()
        self.distance_limit=40. if self.building else 8.
        self.pulse_limit=160 if self.building else 32
        self.call_limit=48 if self.building else 16
        self.explored=[nav.position]
        self.origin=nav.position
        self.progress_at=now
        self.floor_id=preflight(config,nav)
        self.config,self.capture,self.router,self.convention=config,capture,router,convention
        self.request_factory=request_factory
        self.started,self.deadline=now,deadline
        self.identity=(nav.quest_id,nav.map_id,nav.phase,nav.objective_index,nav.objective_target,
                       nav.world_size,nav.destination,nav.destination_map)
        self.overhead=(OverheadObservation(reuse_current=goal_mode!='npc')
                       if not self.building or nav.camera and nav.camera.view==4 else FacadeObservation())
        self.request=self.pending=self.anchor=self.plan=self.pulse=None
        self.sequence=None;self.sequence_at=now
        self.last_turn=now-1;self.turn_sequence=None
        self.calls=self.pulses=0
        self.total=0.0
        self.blocked=self.building_memory.blocked
        self.visited=self.building_memory.visited
        self.session_id=uuid4().hex
        self.motion_history=[]
        self.calibration_action=None
        self.calibration_pulse=None
        self.orbit=None
        self.orbit_request=None
        self.orbits=0
        self.orbit_keyboard_preferred=False
        self.observe_until=None
        self.window=None
        self.pulse_feedback_sequence=None
        self.pulse_feedback_samples=0
        self.recapture_not_before=now
        self.route_speeds=[]
        self.pulse_hold_s=0.
        self.closed=False
        self.building_recoveries=0
        self.building_recovery=None
        self.building_scene=None
        self.building_measurement=None
        self.initial_building_reframe=None
        self.heading_wait=None
        self.ground_trace=ground_trace
        self.relocation=None
        self.relocation_used=False
        self.relocation_reason=None
        self.building_observation=None
        self.overview_capture=overview_capture
        self.full_window_measurement=False
        self.wide_observation_origin=None
        self.expired_building_recaptures=0
        self.overview_request_factory=overview_request_factory
        self.overview_request=self.overview_anchor=None
        self.overview_used=False
        self.backoff_blocked=False
        self.rear_contact=None
        self.visual_ui_hidden=nav.visual_ui_hidden
        self.stair=None
        self.calibration_coupled=False

    @property
    def building(self):
        return self.goal_mode.startswith('building_')

    def seed_building_observation(self, scene, observation=None):
        """Retain the pose and exposure time of an obstructed facade or threshold."""
        if self.goal_mode=='npc':return
        measurement=scene.get('ground_measurement') or {}
        if observation is not None and self.goal_mode=='building_entry':
            self.building_scene=scene
            self.building_measurement=measurement
            self.building_observation=observation
            self.initial_building_reframe=('building_threshold_occluded' if scene.get('doorways')
                                          else 'building_facade_obstructed')
            return
        if (type(measurement.get('texture_features')) is int
                and measurement['texture_features']<40):
            self.building_scene=scene;self.building_measurement=measurement
            self.initial_building_reframe='auto_ground_texture_insufficient'

    def close(self):
        self.closed=True
        if self.request:self.request.close();self.request=None
        if self.overview_request:self.overview_request.close();self.overview_request=None
        self.cancel_relocation()
        self.stair=None

    def cancel_relocation(self):
        if isinstance(self.relocation,TurnawayRelocation):self.relocation.close()
        self.relocation=None

    def building_observation_matches(self, now, nav, heading, *, max_age=15):
        if self.building_observation is None:
            return False
        old,angle,stamp,_,_=self.building_observation
        return (0<=now-stamp<=max_age and old.quest_id==nav.quest_id and old.phase==nav.phase
                and old.objective_index==nav.objective_index and old.objective_target==nav.objective_target
                and old.map_id==nav.map_id and old.world_size==nav.world_size
                and old.destination==nav.destination and old.destination_map==nav.destination_map
                and self.router.distance(old.position,nav.position)<=.08
                and old.camera==nav.camera and old.camera_epoch==nav.camera_epoch
                and old.visual_ui_hidden==nav.visual_ui_hidden and old.height==nav.height
                and abs(heading_error(angle,heading))<=math.radians(1))

    def deadline_result(self,ctx,now,nav):
        details=dict(calls=self.calls,pulses=self.pulses,moved_yards=self.total,
                     elapsed_s=round(now-self.started,3),feedback_complete=not bool(
                         self.pulse or self.calibration_pulse or self.relocation and self.relocation.pulse
                         or self.stair and self.stair.pulse))
        pending=self.pulse or self.calibration_pulse or (self.relocation and self.relocation.pulse) or (self.stair and self.stair.pulse)
        if pending and nav.position is not None:
            details['last_action_age_s']=round(now-pending[0],3)
            details['unconfirmed_displacement_yards']=self.router.distance(pending[1],nav.position)
        ctx.log_event('quest_segmented_deadline',details)
        return TravelCommand(outcome='segmentation_budget_exhausted')

    def discard_building_geometry(self):
        if self.request:self.request.close()
        self.request=self.pending=self.anchor=self.plan=self.window=None
        self.calibration_action=self.calibration_pulse=self.orbit=self.orbit_request=None
        self.observe_until=None
        self.motion_history=[];self.orbits=0
        self.orbit_keyboard_preferred=False
        self.calibration_coupled=False
        # New generation discards geometry from the previous camera/measurement.
        # Overall deadline, observation/pulse/travel budgets and collisions persist.
        self.session_id=uuid4().hex

    def record_position(self, now, position):
        if all(self.router.distance(point,position)>.8 for point in self.explored):
            self.explored.append(position)
            self.progress_at=now
            return True
        return False

    def measure_after_facade(self, ctx, now, nav, reason):
        self.discard_building_geometry()
        self.building_scene=self.building_measurement=self.building_observation=None
        self.full_window_measurement=bool(nav.overhead_camera_available and self.overview_capture)
        self.wide_observation_origin=nav.position if self.full_window_measurement else None
        self.overhead=(OverheadObservation(reuse_current=True) if nav.overhead_camera_available
                       else FacadeObservation())
        self.recapture_not_before=now+.5
        ctx.log_event('quest_building_remeasurement_started',dict(
            reason=reason,position=nav.position,requested_view=4 if nav.overhead_camera_available else 'facade',
            full_window=self.full_window_measurement,session_id=self.session_id))

    def reached_threshold(self,ctx,nav,frame_id,basis):
        self.plan = self.window = self.anchor = None
        self.goal_mode = 'building_cross'
        self.crossing_origin = nav.position
        self.crossing_baseline = self.total
        ctx.log_event('quest_building_threshold_reached', dict(
            position=nav.position, sequence=nav.sequence, source_frame=frame_id,
            basis=basis, entrance_verified=False))
        return TravelCommand()

    def start_building_relocation(self,ctx,nav,reason,relocation,evidence=None):
        self.discard_building_geometry()
        self.building_recovery=None;self.initial_building_reframe=None
        self.building_scene=self.building_measurement=self.building_observation=None
        self.visited.append(tuple(nav.position))
        self.relocation=relocation;self.relocation_used=True;self.relocation_reason=reason
        if self.goal_mode=='building_cross':
            self.goal_mode='building_search' if self.passages else 'building_entry'
        self.crossing_baseline=self.crossing_origin=self.crossing_confirmed=None
        self.return_passage=None;self.return_look=None
        ctx.log_event('quest_building_relocation_started',dict(reason=reason,
            basis=relocation.basis,position=nav.position,
            points=len(getattr(relocation,'path',[])),max_distance=relocation.limit,
            deadline=relocation.deadline,evidence=evidence,
            projection_calibrated=False,needs_new_observation=True))
        return TravelCommand()

    def turn_away_from_entrance(self,ctx,now,nav,heading,reason):
        deadline=min(now+48,self.deadline-8)
        if (self.goal_mode!='building_entry' or self.relocation_used or self.backoff_blocked
                or self.overview_capture is None or not self.config.building_access
                or heading is None or nav.height is None or not nav.camera_restore_available
                or not nav.flags & TravelFlags.OUTDOORS
                or deadline-now<27 or self.distance_limit-self.total<3.
                or self.pulse_limit-self.pulses<8 or self.call_limit-self.calls<7):
            return None
        def observe(context,sample):
            from .building_access import BuildingSceneRequest
            factory=self.overview_request_factory or BuildingSceneRequest
            image=self.overview_capture(context.capture)
            height,width=image.shape[:2]
            if width>1012:
                image=cv2.resize(image,(1012,round(height*1012/width)),interpolation=cv2.INTER_AREA)
            self.calls+=1
            return factory(image,self.config,sample.camera.view,bool(sample.flags & TravelFlags.OUTDOORS),
                           full_window=True,visual_ui_hidden=sample.visual_ui_hidden,mode='building_turnaway_view')
        relocation=TurnawayRelocation(now,nav,heading,self.router,self.convention,deadline=deadline,
            blocked_positions=self.blocked,observe=observe,max_distance=self.distance_limit-self.total,
            max_pulses=self.pulse_limit-self.pulses)
        return self.start_building_relocation(ctx,nav,reason,relocation,
            dict(basis='inspect_away_from_obstructed_entry',source_frame=self.building_observation[3]))

    def recover_building_view(self,ctx,now,nav,reason,heading=None):
        if self.goal_mode=='npc' or reason not in BUILDING_REOBSERVE:
            return TravelCommand(outcome='segmentation_'+reason)
        self.backoff_blocked |= any(p['key']=='s' and p['moved']<.15 for p in self.motion_history)
        if (heading is not None and self.building_observation_matches(now,nav,heading)
                and (self.building_scene or {}).get('surface') in {'unknown','exterior','interior_floor'}
                and reason in {'building_threshold_occluded','tracking_seed_insufficient',
                               'registration_insufficient_matches','auto_ground_texture_insufficient',
                               'tracking_local_texture_missing','tracking_insufficient_matches'}):
            escape=self.turn_away_from_entrance(ctx,now,nav,heading,reason)
            if escape is not None:return escape
        distance_budget=self.distance_limit-self.total
        if reason=='building_threshold_occluded':
            distance_budget=min(distance_budget,1.)
        # Use the still-fresh walking history BEFORE stationary view retries
        # consume its lifetime. No trace can instead use a separately labelled,
        # bounded experiment; neither branch certifies a route to the doorway.
        if (not self.relocation_used and not self.backoff_blocked and self.ground_trace is not None
                and self.call_limit-self.calls>=2):
            deadline=min(now+12,self.deadline-8,self.progress_at+60-8)
            try:
                relocation=TraceRelocation(now,nav,self.ground_trace,self.router,self.convention,
                    deadline=deadline,blocked_positions=self.blocked,
                    max_distance=distance_budget,max_pulses=self.pulse_limit-self.pulses)
            except ValueError as exc:
                ctx.log_event('quest_building_relocation_unavailable',dict(reason=str(exc),
                    observation_failure=reason,translation_authorized=False))
            else:
                return self.start_building_relocation(ctx,nav,reason,relocation)
        if (not self.relocation_used and self.building_observation is not None
                and heading is not None and ctx.heading_age()<=.55
                and self.call_limit-self.calls>=2):
            _,_,_,frame_id,size=self.building_observation
            evidence=local_backoff_evidence(self.building_scene,size)
            if evidence and not self.backoff_blocked and self.building_observation_matches(now,nav,heading):
                try:
                    relocation=BuildingBackoffProbe(now,nav,heading,self.router,self.convention,
                        deadline=min(now+12,self.deadline-8,self.progress_at+60-8),
                        blocked_positions=self.blocked,max_distance=distance_budget,
                        max_pulses=self.pulse_limit-self.pulses)
                except ValueError as exc:
                    ctx.log_event('quest_building_relocation_unavailable',dict(reason=str(exc),
                        observation_failure=reason,translation_authorized=False))
                else:
                    return self.start_building_relocation(ctx,nav,reason,relocation,
                        dict(evidence,source_frame=frame_id))
        if (not self.overview_used and not self.relocation_used and self.overview_capture is not None
                and heading is not None and ctx.heading_age()<=.55
                and self.call_limit-self.calls>=3
                and min(self.deadline,self.progress_at+60)-now>=22):
            # The ordinary task crop can omit the only visible ground. Widen
            # this observation once; its pixels never enter the route planner.
            from .building_access import BuildingSceneRequest
            self.discard_building_geometry()
            self.building_observation=None
            rgb=self.overview_capture(ctx.capture)
            factory=self.overview_request_factory or BuildingSceneRequest
            visibility=dict(visual_ui_hidden=nav.visual_ui_hidden) if nav.visual_ui_hidden is not None else {}
            self.overview_request=factory(rgb,self.config,nav.camera.view,
                bool(nav.flags & TravelFlags.OUTDOORS),full_window=True,**visibility)
            self.overview_anchor=(nav,heading,now,self.overview_request.frame_id,
                                  (rgb.shape[1],rgb.shape[0]))
            self.overview_used=True;self.calls+=1;self.relocation_reason=reason
            ctx.log_event('quest_building_overview_requested',dict(
                reason=reason,frame_id=self.overview_request.frame_id,
                position=nav.position,route_planning=False,movement_authorized=False))
            return TravelCommand()
        if self.building_recoveries>=2 or self.deadline-now<10:
            ctx.log_event('quest_building_view_recovery_exhausted',dict(reason=reason,
                attempts=self.building_recoveries,remaining_s=round(self.deadline-now,2)))
            return TravelCommand(outcome='building_observation_recovery_exhausted')
        self.discard_building_geometry()
        self.building_observation=None
        self.building_recoveries+=1
        overhead=nav.camera.view!=4
        door_failure=reason.startswith('building_')
        hint=((self.building_scene or {}).get('look_hint') if door_failure else
              (self.building_measurement or {}).get('look_hint'))
        self.building_recovery=BuildingObservationRecovery(overhead=overhead,
            look_hint=hint,convention=self.convention)
        ctx.log_event('quest_building_view_recovery',dict(reason=reason,attempt=self.building_recoveries,
            view=4 if overhead else 'facade',look_hint=hint,
            look_basis='entrance' if door_failure else 'ground_texture',remaining_s=round(self.deadline-now,2),
            old_geometry_discarded=True,translation_authorized=False))
        return TravelCommand()  # Release existing inputs before changing view.

    def step(self,ctx,now,nav,heading):
        if self.closed:return TravelCommand(outcome='segmentation_closed')
        if now>=self.deadline:return self.deadline_result(ctx,now,nav)
        if self.building and now-self.progress_at>=60:
            ctx.log_event('quest_building_no_progress',dict(seconds=now-self.progress_at,
                calls=self.calls,pulses=self.pulses,passages=len(self.passages)))
            return TravelCommand(outcome='building_no_progress_budget')
        if nav.sequence!=self.sequence:self.sequence,self.sequence_at=nav.sequence,now
        if now-self.sequence_at>.75:return TravelCommand(outcome='probe_telemetry_stale')
        current_identity=(nav.quest_id,nav.map_id,nav.phase,nav.objective_index,nav.objective_target,
                          nav.world_size,nav.destination,nav.destination_map)
        busy=nav.flags & (TravelFlags.IN_COMBAT|TravelFlags.FLYING|TravelFlags.FALLING|
                         TravelFlags.SWIMMING|TravelFlags.MOUNTED|TravelFlags.CASTING)
        reason=('goal_changed' if current_identity!=self.identity else
                'position_missing' if nav.position is None else
                'height_missing' if nav.height is None else
                'movement_bindings_lost' if not nav.flags & TravelFlags.KEYS_VERIFIED else
                'character_busy' if busy else
                'character_not_alive' if nav.recovery and nav.recovery.life!=LifeState.ALIVE else
                'outdoor_surface_lost' if self.goal_mode=='travel' and not nav.flags & TravelFlags.OUTDOORS else
                'npc_selection_lost' if self.goal_mode=='npc' and not nav.task & TaskFlags.NPC_SELECTED else
                'heading_missing' if heading is None else
                'heading_stale' if ctx.heading_age()>.55 else None)
        # Camera receipts and heading travel on different channels. A brief
        # delayed heading releases input and waits; it is not a failed route.
        # Other interruptions retain priority and the overall deadline stands.
        if reason in {'heading_missing','heading_stale'}:
            if self.heading_wait is None:
                self.heading_wait=now
                ctx.log_event('quest_segmented_waiting_heading',dict(reason=reason,
                    camera_recovery=bool(self.building_recovery),sequence=nav.sequence,
                    max_wait_s=1.,movement_authorized=False))
            if now-self.heading_wait<1.:
                return TravelCommand()
        elif reason is None:
            self.heading_wait=None
        if reason:
            ctx.log_event('quest_segmented_interrupted',dict(reason=reason,
                mode=self.goal_mode,position=nav.position,sequence=nav.sequence,
                heading_age=ctx.heading_age(),busy_flags=int(busy),
                camera_recovery=bool(self.building_recovery),
                movement_pending=bool(self.pulse or self.calibration_pulse or self.stair and self.stair.pulse),
                request_pending=bool(self.request),elapsed_s=round(now-self.started,3)))
            return TravelCommand(outcome='probe_interrupted')
        pending_motion=(self.pulse or self.calibration_pulse
                        or self.relocation is not None and self.relocation.pulse
                        or self.stair is not None and self.stair.pulse)
        if nav.visual_ui_hidden!=self.visual_ui_hidden and not pending_motion:
            old=self.visual_ui_hidden
            self.discard_building_geometry()
            if self.overview_request:self.overview_request.close();self.overview_request=None
            self.cancel_relocation()
            self.stair=None
            self.return_look=None
            self.building_scene=self.building_measurement=self.building_observation=None
            self.building_recovery=None;self.initial_building_reframe=None
            self.visual_ui_hidden=nav.visual_ui_hidden
            self.overhead=(OverheadObservation(reuse_current=True)
                if not self.building or nav.camera.view==4 else FacadeObservation())
            self.recapture_not_before=now+.5
            ctx.log_event('quest_segmented_ui_changed',dict(previous=old,current=nav.visual_ui_hidden,
                old_geometry_discarded=True,requires_new_observation=True))
            return TravelCommand()  # A pending movement receipt is consumed first.
        if (self.goal_mode!='travel' and nav.task & TaskFlags.NPC_SELECTED
                and nav.task & TaskFlags.NPC_RANGE_KNOWN and nav.task & TaskFlags.NPC_WITHIN_5_YARDS):
            self.close();return TravelCommand(outcome='detoured' if self.goal_mode=='npc' else 'building_entered')
        if self.stair and self.stair.pulse:
            old_total=self.stair.total
            command=self.stair.feedback(now,nav,heading)
            self.total+=self.stair.total-old_total
            if command.outcome!='stair_step_observed':return command
            self.record_position(now,nav.position)
            ctx.log_event('quest_stair_step_observed',dict(position=nav.position,height=nav.height,
                moved_yards=self.stair.total-old_total,total_yards=self.stair.total,
                sequence=nav.sequence,requires_new_image=True,level_id=None))
            self.anchor=None;self.recapture_not_before=now+.2
            return TravelCommand()
        if self.goal_mode=='building_cross' and self.crossing_confirmed is not None:
            if (self.router.distance(self.crossing_confirmed['position'],nav.position)>.15
                    or abs(nav.height-self.crossing_confirmed['height'])>.1):
                return TravelCommand(outcome='probe_pose_changed')
            try:
                transition=self.building_memory.crossed(self.crossing_confirmed,return_id=self.return_passage)
            except ValueError as exc:
                return TravelCommand(outcome=str(exc))
            ctx.log_event('quest_building_passage_crossed',dict(self.crossing_confirmed,
                indoors=not bool(nav.flags & TravelFlags.OUTDOORS),
                **transition))
            self.visited=self.building_memory.visited;self.blocked=self.building_memory.blocked
            self.return_passage=None;self.return_look=None
            self.crossing_confirmed=None;self.crossing_baseline=None;self.crossing_origin=None
            self.goal_mode='building_search'
            self.building_scene=None;self.building_measurement=None
            self.plan=self.window=self.anchor=None
            self.recapture_not_before=now+.5
            # Keep the SAME controller, all collisions and the total budget.
            # A new image, rather than an outdoor flag, supplies the next route.
            return TravelCommand()
        if self.return_look:
            command=self.return_look.step(now,nav,heading)
            if command is not None:return command
            self.return_look=None
            self.recapture_not_before=now+.2
            ctx.log_event('quest_building_return_look_complete',dict(passage_id=self.return_passage,
                current_area=self.building_memory.current,requires_new_observation=True))
            return TravelCommand()
        if self.initial_building_reframe:
            reason,self.initial_building_reframe=self.initial_building_reframe,None
            if reason=='building_facade_obstructed':
                if self.building_observation_matches(now,nav,heading,max_age=20):
                    escape=self.turn_away_from_entrance(ctx,now,nav,heading,reason)
                    if escape is not None:return escape
                self.measure_after_facade(ctx,now,nav,reason)
                return TravelCommand()
            if reason=='building_threshold_occluded' and not self.building_observation_matches(now,nav,heading):
                ctx.log_event('quest_building_observation_discarded',dict(
                    frame_id=self.building_observation[3],reason='entry_capture_changed_or_expired'))
                if self.building_observation_matches(now,nav,heading,max_age=20):
                    # An expired door image may prompt a stationary look behind.
                    # Only the new reverse image and native pose can lease a step.
                    escape=self.turn_away_from_entrance(ctx,now,nav,heading,reason)
                    if escape is not None:return escape
                self.measure_after_facade(ctx,now,nav,'entry_capture_changed_or_expired')
                return TravelCommand()  # Measure the current frame on the next tick.
            return self.recover_building_view(ctx,now,nav,reason,heading)
        if self.relocation:
            old_total,old_pulses=self.relocation.total,self.relocation.pulses
            command=self.relocation.step(ctx,now,nav,heading)
            received=self.relocation.total-old_total
            self.total+=received
            if (received>0 and command.outcome in {None,'building_relocated'}
                    and self.record_position(now,nav.position)):
                ctx.log_event('quest_building_new_observation_position',dict(
                    position=nav.position,sequence=nav.sequence,basis=self.relocation.basis))
            self.pulses+=self.relocation.pulses-old_pulses
            if not command.outcome:return command
            ctx.log_event('quest_building_relocation_result',dict(reason=command.outcome,
                basis=self.relocation.basis,position=nav.position,moved_yards=self.relocation.total,
                needs_new_observation=True,route_to_target_verified=False))
            if command.outcome=='building_relocation_blocked' and isinstance(self.relocation,BuildingBackoffProbe):
                self.rear_contact=(nav.position,(heading+math.pi)%math.tau)
                self.backoff_blocked=True
            was_turnaway=isinstance(self.relocation,TurnawayRelocation)
            self.cancel_relocation()
            if was_turnaway and command.outcome in {'building_relocated','building_relocation_blocked'}:
                self.discard_building_geometry()
                self.full_window_measurement=False
                self.wide_observation_origin=None
                self.overhead=FacadeObservation()
                self.recapture_not_before=now+.5
                ctx.log_event('quest_building_facade_reacquisition',dict(position=nav.position,
                    reason=command.outcome,sequence=nav.sequence,entrance_verified=False))
                return TravelCommand()
            if (command.outcome=='building_relocated' or
                    command.outcome=='building_relocation_blocked' and self.relocation_reason=='building_threshold_occluded'):
                # A fresh measurement can now select a connected observation
                # point or try another direction after a native contact.
                self.measure_after_facade(ctx,now,nav,command.outcome)
                return TravelCommand()
            if command.outcome in {'building_relocation_budget','building_relocation_blocked',
                                   'building_relocation_expired'}:
                return self.recover_building_view(ctx,now,nav,self.relocation_reason,heading)
            return command
        if self.overview_request:
            old,angle,stamp,frame_id,size=self.overview_anchor
            if (self.router.distance(old.position,nav.position)>.08
                    or nav.camera!=old.camera or nav.camera_epoch!=old.camera_epoch
                    or abs(heading_error(angle,heading))>math.radians(1)):
                self.overview_request.close();self.overview_request=None
                return TravelCommand(outcome='probe_pose_changed')
            try:
                if now-stamp>20:raise ValueError('building_overview_expired')
                result=self.overview_request.poll()
            except (ValueError,OSError,KeyError) as exc:
                self.overview_request.close();self.overview_request=None
                ctx.log_event('quest_building_overview_failed',dict(reason=str(exc),frame_id=frame_id))
                return self.recover_building_view(ctx,now,nav,self.relocation_reason,heading)
            if result is None:return TravelCommand()
            self.overview_request.close();self.overview_request=None
            if (result.get('frame_id')!=frame_id or result.get('schema')!=1
                    or result.get('executable') is not False):
                return TravelCommand(outcome='building_observation_invalid')
            self.building_scene=result
            self.building_measurement=result.get('ground_measurement')
            self.building_observation=self.overview_anchor
            ctx.log_event('quest_building_overview_observed',dict(frame_id=frame_id,
                surface=result.get('surface'),local_evidence=local_backoff_evidence(result,size),
                route_planning=False,requires_native_feedback=True))
            return self.recover_building_view(ctx,now,nav,self.relocation_reason,heading)
        if self.building_recovery:
            command=self.building_recovery.step(ctx,now,nav,heading)
            if command is not None:return command
            self.overhead=self.building_recovery.view
            self.building_recovery=None
            self.building_scene=None
            self.building_measurement=None
            self.recapture_not_before=now+.5
            ctx.log_event('quest_building_view_recovered',dict(view=nav.camera.view,
                needs_new_entrance_observation=True,projection_calibrated=False))
            return TravelCommand()
        if self.orbit_request:
            at,initial,angle,pixels=self.orbit_request
            if (self.router.distance(initial.position,nav.position)>.08 or abs(nav.height-initial.height)>.1
                    or not nav.calibration_orbit_available):
                return TravelCommand(outcome='auto_orbit_lease_unconfirmed')
            if now-at>.55:
                if (self.goal_mode!='npc' and nav.sequence!=initial.sequence and nav.camera==initial.camera
                        and nav.camera_epoch==initial.camera_epoch and not nav.calibration_orbit_active
                        and abs(heading_error(angle,heading))<=math.radians(1)):
                    self.orbit_request=None
                    self.orbit=CalibrationOrbit(now,nav,heading,pixels,self.convention,keyboard=True)
                    ctx.log_event('quest_calibration_orbit_fallback',dict(
                        reason='overhead_lease_not_granted',fallback='verified_turn_keys',position=nav.position))
                    return TravelCommand()  # Release F4 before the next stationary turn.
                return TravelCommand(outcome='auto_orbit_lease_unconfirmed')
            if (nav.sequence!=initial.sequence and nav.camera.request==(initial.camera.request+1)%8
                    and nav.calibration_orbit_active):
                self.orbit_request=None
                self.orbit=CalibrationOrbit(now,nav,heading,pixels,self.convention)
                return TravelCommand(keys=frozenset({'f4'}),camera_yaw_px=pixels)
            return TravelCommand(keys=frozenset({'f4'}))
        if self.orbit:
            command=self.orbit.step(ctx,now,nav,heading)
            if command.outcome!='auto_orbit_ready':return command
            self.orbit_keyboard_preferred=self.orbit.keyboard
            self.calibration_coupled=not self.orbit.keyboard
            ctx.log_event('quest_calibration_orbit_feedback',dict(
                yaw_deg=round(math.degrees(heading_error(self.orbit.heading,heading)),2),
                via='turn_keys' if self.orbit.keyboard else 'mouse',
                heading_confirmed=True,camera_geometry_confirmed=False))
            self.orbit=None;self.anchor=None
            return TravelCommand()
        if self.observe_until is not None:
            if now<self.observe_until:return TravelCommand()
            self.observe_until=None
        if self.calibration_pulse:
            at,origin,sequence,key,height,direction=self.calibration_pulse
            if abs(heading_error(direction,heading))>math.radians(1):
                return TravelCommand(outcome='auto_translation_camera_turned')
            if now-at<.6 or nav.sequence==sequence:return TravelCommand()
            moved=self.router.distance(origin,nav.position)
            if moved>1.25:return TravelCommand(outcome='auto_translation_displacement_excessive')
            if not bounded_height_change(moved,nav.height-height):
                ctx.log_event('quest_segmented_height_rejected',dict(stage='calibration',
                    horizontal_yards=moved,height_delta=nav.height-height,
                    before_sequence=sequence,after_sequence=nav.sequence))
                return TravelCommand(outcome='probe_height_drop')
            if moved>=.15:
                dx,dy=[(b-a)*s for a,b,s in zip(origin,nav.position,nav.world_size)]
                left=math.pi/2 if self.convention=='ccw_west' else -math.pi/2
                expected=direction+{'w':0,'s':math.pi,'q':left,'e':-left}[key]
                if abs(heading_error(expected,compass_from_delta(dx,dy,self.convention)))>math.radians(30):
                    return TravelCommand(outcome='auto_input_direction_mismatch')
            self.motion_history.append(dict(key=key,moved=moved,origin=origin,position=nav.position,
                                             height_before=height,height_after=nav.height,
                                             height_delta=nav.height-height,
                                             before_sequence=sequence,after_sequence=nav.sequence))
            ctx.log_event('quest_automatic_calibration_feedback',self.motion_history[-1])
            self.calibration_pulse=None;self.anchor=None
            self.total+=moved
            if len([p for p in self.motion_history if p['moved']<.15])>=2:
                if self.building:
                    return self.recover_building_view(ctx,now,nav,'auto_calibration_repeated_collision',heading)
                return TravelCommand(outcome='auto_calibration_repeated_collision')
            return TravelCommand()
        if self.pulse:
            at,origin,seq,target,direction,height=self.pulse
            delta=[(b-a)*s for a,b,s in zip(origin,nav.position,nav.world_size)]
            proof=(self.plan or {}).get('registration',{}).get('surface_height')
            reference=self.anchor[3] if self.anchor else None
            cumulative=([(b-a)*s for a,b,s in zip(reference.player_position,nav.position,nav.world_size)]
                        if reference else delta)
            cumulative_height=nav.height-(reference.height if reference else height)
            surface_changed=(not height_matches(delta,nav.height-height,proof)
                             or not height_matches(cumulative,cumulative_height,proof))
            if surface_changed and (proof is None or not height_matches([0.,0.],0.,proof)
                    or not bounded_height_change(self.router.distance(origin,nav.position),nav.height-height)):
                ctx.log_event('quest_segmented_height_rejected',dict(stage='route',
                    delta_xy=delta,height_delta=nav.height-height,
                    cumulative_xy=cumulative,cumulative_height=cumulative_height,
                    model=proof,before_sequence=seq,after_sequence=nav.sequence))
                return TravelCommand(outcome='probe_height_drop')
            if self.window and self.anchor and (nav.camera!=self.anchor[0].camera
                    or nav.camera_epoch!=self.anchor[0].camera_epoch):
                return TravelCommand(outcome='probe_pose_changed')
            if nav.sequence!=seq and nav.sequence!=self.pulse_feedback_sequence:
                self.pulse_feedback_sequence=nav.sequence;self.pulse_feedback_samples+=1
            if now-at<(.25 if self.window else .55):return TravelCommand()
            if nav.sequence==seq:return TravelCommand()
            if self.window and self.pulse_feedback_samples<2:return TravelCommand()
            moved=self.router.distance(origin,nav.position)
            progress=self.router.distance(origin,target)-self.router.distance(nav.position,target)
            search_goal=self.plan.get('search_goal_world') if self.plan else None
            if search_goal and self.router.distance(nav.position,search_goal)<.5:
                self.visited.append(tuple(search_goal))
            self.pulse=None
            observed_at = time.monotonic()

            def log_feedback(status):
                ctx.log_event('quest_segmented_movement_feedback', dict(
                    frame_id=reference.frame_id if reference else None, before_sequence=seq,
                    after_sequence=nav.sequence, position=nav.position, yards=moved, progress=progress,
                    status=status, pulse_to_feedback_ms=round((now-at)*1000, 2),
                    capture_to_feedback_ms=round((observed_at-reference.captured_at)*1000, 2) if reference else None))

            if moved>1.25:
                log_feedback('discontinuous')
                return TravelCommand(outcome='probe_pose_changed')
            if moved<(.03 if self.window else .1) or progress<(.02 if self.window else .03):
                log_feedback('insufficient_progress')
                self.plan=None;self.window=None
                self.blocked.append(target)
                if len(self.blocked)>=2:return TravelCommand(outcome='segmentation_repeated_collision')
            else:
                dx,dy=[(b-a)*s for a,b,s in zip(origin,nav.position,nav.world_size)]
                if abs(heading_error(direction,compass_from_delta(dx,dy,self.convention)))>math.radians(35):
                    log_feedback('wrong_direction')
                    return TravelCommand(outcome='probe_pose_changed')
                self.total+=moved
                self.record_position(now,nav.position)
                if self.pulse_hold_s>0:self.route_speeds.append(moved/self.pulse_hold_s*1.3)
                if self.window:
                    try:self.window.feedback(origin,nav.position)
                    except ValueError as exc:
                        log_feedback(str(exc))
                        self.plan=None;self.window=None
                        return TravelCommand(outcome=str(exc))
                log_feedback('verified')
                if surface_changed:
                    # A bounded, on-route step onto another surface is not a
                    # licence to extrapolate the old plane. Keep its movement
                    # receipt, release input, and measure the new local ground.
                    source_frame=self.plan['frame_id']
                    self.discard_building_geometry()
                    self.building_scene=self.building_measurement=self.building_observation=None
                    self.recapture_not_before=now+.5
                    ctx.log_event('quest_segmented_surface_changed',dict(
                        source_frame=source_frame,position=nav.position,
                        delta_xy=delta,height_delta=nav.height-height,
                        cumulative_xy=cumulative,cumulative_height=cumulative_height,
                        before_sequence=seq,after_sequence=nav.sequence,
                        requires_new_measurement=True,level_id=None))
                    return TravelCommand()
                if (self.goal_mode=='travel' and search_goal
                        and self.router.distance(nav.position,search_goal)<=.25
                        and self.router.distance(self.origin,self.travel_goal)
                            - self.router.distance(nav.position,self.travel_goal)>=.5):
                    ctx.log_event('quest_segmented_travel_frontier_observed',dict(
                        position=nav.position,goal=self.travel_goal,source_frame=self.plan['frame_id'],
                        sequence=nav.sequence,total_yards=self.total))
                    return TravelCommand(outcome='detoured')
                if (self.goal_mode=='building_entry' and self.full_window_measurement
                        and nav.camera.view==4 and nav.camera_restore_available
                        and self.plan.get('automatic_goal',{}).get('basis')=='local_observation_frontier'
                        and search_goal and self.router.distance(nav.position,search_goal)<=.2
                        and self.wide_observation_origin is not None
                        and self.router.distance(self.wide_observation_origin,nav.position)>=.3):
                    frame_id=self.plan['frame_id']
                    self.discard_building_geometry()
                    self.building_scene=self.building_measurement=self.building_observation=None
                    self.overhead=FacadeObservation()
                    self.full_window_measurement=False
                    self.wide_observation_origin=None
                    self.recapture_not_before=now+.5
                    ctx.log_event('quest_building_facade_reacquisition',dict(
                        source_frame=frame_id,position=nav.position,sequence=nav.sequence,
                        entrance_verified=False))
                    return TravelCommand()
                if (search_goal and self.router.distance(nav.position,search_goal)<.15
                        and self.plan.get('automatic_goal',{}).get('basis')=='connector_observation_frontier'):
                    ctx.log_event('quest_building_connector_approached',dict(
                        position=nav.position,height=nav.height,source_frame=self.plan['frame_id'],
                        connector=self.plan['automatic_goal'],before_sequence=seq,after_sequence=nav.sequence,
                        surface_measured=False,passage_verified=False,requires_new_observation=True))
                    prior=StairTrial.measured_anchor(self.plan,self.anchor[3])
                    self.discard_building_geometry()
                    self.stair=StairTrial(now,nav,self.convention,deadline=self.deadline,anchor_reference=prior)
                    if nav.camera.view!=4:self.overhead=OverheadObservation(reuse_current=False)
                    self.recapture_not_before=now+.35
                    return TravelCommand()
                if (search_goal and self.router.distance(nav.position,search_goal)<.15
                        and self.goal_mode=='building_cross' and self.crossing_baseline is not None
                        and self.crossing_origin is not None
                        and self.router.distance(self.crossing_origin,nav.position)>=.3
                        and self.plan.get('automatic_goal',{}).get('basis')=='door_floor_crossing'):
                    # IsOutdoors cannot confirm passage through an indoor door
                    # or an outdoor courtyard gate. Require the measured floor
                    # beyond the threshold AND actual movement to that target.
                    self.crossing_confirmed=dict(position=nav.position,
                        threshold_position=self.crossing_origin,
                        height=nav.height,
                        source_frame=self.plan['frame_id'],target=search_goal,
                        before_sequence=seq,after_sequence=nav.sequence,
                        moved_yards=self.total-self.crossing_baseline,
                        net_yards=self.router.distance(self.crossing_origin,nav.position),
                        basis='measured_door_floor_and_native_movement')
                    self.plan=None;self.window=None;self.anchor=None
                    return TravelCommand()
                if (search_goal and self.router.distance(nav.position,search_goal)<.5
                        and self.goal_mode in {'building_entry','building_search'}
                        and self.plan.get('automatic_goal',{}).get('basis')=='door_threshold'):
                    return self.reached_threshold(ctx,nav,self.plan['frame_id'],'native_movement')
            if self.window and self.window.reusable(nav.position,now):
                # Update only the verified pose, retaining the ORIGINAL exposure
                # and expiry. No old frame is relabelled as newly captured.
                _,angle,stamp,frame=self.anchor
                self.anchor=(nav,angle,stamp,frame)
                ctx.log_event('quest_segmented_prefix_continued',dict(frame_id=frame.frame_id,
                    remaining_yards=self.window.remaining(),pulses=self.window.pulses))
            else:
                self.plan=None;self.window=None;self.anchor=None
                self.recapture_not_before=at+.55
            return TravelCommand()
        if self.total>self.distance_limit or self.pulses>=self.pulse_limit or self.calls>=self.call_limit and self.plan is None and self.request is None:
            return TravelCommand(outcome='segmentation_budget_exhausted')
        command=self.overhead.step(ctx,now,nav)
        if command is not None:return command
        if self.anchor is not None and (self.pending is not None or self.request or self.plan or self.calibration_action):
            old,angle,stamp,frame=self.anchor
            if (self.router.distance(old.position,nav.position)>.08 or nav.camera!=old.camera
                    or nav.camera_epoch!=old.camera_epoch or abs(nav.height-old.height)>.1):
                return TravelCommand(outcome='probe_pose_changed')
            if not self.plan and abs(heading_error(angle,heading))>math.radians(1):
                return TravelCommand(outcome='probe_pose_changed')
            if now-stamp>15:
                if self.building and self.request and self.expired_building_recaptures==0:
                    self.expired_building_recaptures+=1
                    self.measure_after_facade(ctx,now,nav,'segmentation_frame_expired')
                    return TravelCommand()
                return TravelCommand(outcome='segmentation_frame_expired')
        if self.calibration_action:
            action,self.calibration_action=self.calibration_action,None
            self.anchor=None
            if action['kind']=='calibration_orbit':
                if self.deadline-now<5.5:return TravelCommand(outcome='segmentation_feedback_budget_reserved')
                if self.orbits>=4:return TravelCommand(outcome='auto_orbit_budget')
                self.orbits+=1
                # The addon's F4 mouse lease is explicitly OVERHEAD-only.
                # Frontal building measurements use verified turn keys and
                # still require image registration to prove camera rotation.
                if (self.orbit_keyboard_preferred or nav.camera.view!=4
                        or not nav.calibration_orbit_available):
                    self.orbit=CalibrationOrbit(now,nav,heading,action['yaw_pixels'],self.convention,keyboard=True)
                    return self.orbit.step(ctx,now,nav,heading)
                self.orbit_request=(now,nav,heading,action['yaw_pixels'])
                return TravelCommand(keys=frozenset({'f4'}))
            if action['kind']=='calibration_observe':
                self.observe_until=now+.7
                return TravelCommand()
            key=action['key']
            if self.deadline-now<.9:return TravelCommand(outcome='segmentation_feedback_budget_reserved')
            if not nav.strafe_verified:return TravelCommand(outcome='auto_strafe_bindings_required')
            if len(self.motion_history)>=7:return TravelCommand(outcome='auto_calibration_sample_budget')
            self.pulses+=1
            self.calibration_pulse=(now,nav.position,nav.sequence,key,nav.height,heading)
            ctx.log_event('quest_automatic_calibration_probe',dict(key=key,hold_s=.07,
                frame_id=action['frame_id'],metric_route=False))
            return TravelCommand(tap=key,hold_s=.07,precise=True)
        if self.plan:
            points=self.plan.get('next_segment_world',[])
            if len(points)<2:return TravelCommand(outcome='segmentation_plan_invalid')
            if self.window and not self.window.reusable(nav.position,now):
                self.plan=None;self.window=None;self.anchor=None
                return TravelCommand()
            target=self.window.target(nav.position) if self.window else tuple(points[1])
            distance=self.router.distance(nav.position,target)
            if distance<.05:
                self.visited.append(nav.position);self.plan=None;self.anchor=None
                if self.goal_mode=='npc' and len(self.visited)>=3:return TravelCommand(outcome='auto_npc_search_region_exhausted')
                return TravelCommand()
            if distance>(2.1 if self.window else .8):return TravelCommand(outcome='segmentation_step_budget_invalid')
            dx,dy=[(b-a)*s for a,b,s in zip(nav.position,target,nav.world_size)]
            desired=compass_from_delta(dx,dy,self.convention)
            error=heading_error(heading,desired)
            if abs(error)>math.radians(8):
                if self.deadline-now<1.5:return TravelCommand(outcome='segmentation_feedback_budget_reserved')
                if now-self.last_turn<.5 or nav.sequence==self.turn_sequence:return TravelCommand()
                self.last_turn,self.turn_sequence=now,nav.sequence
                left=(error>0)==(self.convention=='ccw_west')
                return TravelCommand(tap='left' if left else 'right',hold_s=min(.12,max(.025,abs(error)/math.radians(240))))
            if now-self.last_turn<.5 or now-ctx.heading_age()<self.last_turn+.2:return TravelCommand()
            if self.deadline-now<.9:return TravelCommand(outcome='segmentation_feedback_budget_reserved')
            speed=max([8.,*self.route_speeds,*[p['moved']/.07*1.3 for p in self.motion_history]])
            if self.window:
                remaining=min(distance,self.window.remaining(),self.distance_limit-self.total)-.03
                if remaining<=.03:
                    return TravelCommand(outcome='segmentation_budget_exhausted')
                self.window.pulses+=1
                duration=min(.07 if self.window.limit<=.75 or not self.route_speeds else .12,remaining/speed)
            else:duration=min(.07,distance/speed)
            self.pulses+=1
            self.pulse=now,nav.position,nav.sequence,target,desired,nav.height
            self.pulse_feedback_sequence=None;self.pulse_feedback_samples=0
            self.pulse_hold_s=duration
            ctx.log_event('quest_segmented_movement_issued',dict(target=target,hold_s=duration,
                         frame_id=self.plan['frame_id'],before_sequence=nav.sequence,
                         capture_to_issue_ms=round((time.monotonic()-self.anchor[3].captured_at)*1000, 2)))
            return TravelCommand(tap='w',hold_s=duration,precise=True)
        if self.request:
            try:result=self.request.poll()
            except (ValueError,OSError,KeyError) as exc:
                return self.recover_building_view(ctx,now,nav,str(exc),heading)
            if result is None:return TravelCommand()
            self.request.close();self.request=None
            if result.get('frame_id')!=self.anchor[3].frame_id:return TravelCommand(outcome='segmentation_result_frame_mismatch')
            expected=f'{nav.quest_id}:{nav.phase.name}:{nav.objective_index or 0}'
            if result.get('target_key')!=expected:return TravelCommand(outcome='segmentation_result_target_mismatch')
            if self.stair:
                allowed={'stair_probe','stair_surface_candidate','calibration_orbit','calibration_observe'}
                if self.stair.observations==0:allowed.add('calibration_probe')
                if result.get('kind') not in allowed:
                    return TravelCommand(outcome='stair_trial_evidence_invalid')
            if result.get('stationary_only') is True:
                if (not self.building or nav.camera.view!=4 or self.stair is not None
                        or result.get('kind') not in {'calibration_observe','calibration_orbit'}):
                    return TravelCommand(outcome='auto_calibration_action_invalid')
            elif self.building:
                scene=result.get('building_scene')
                layout=scene.get('layout',{}) if isinstance(scene,dict) else {}
                goal=result.get('automatic_goal',{})
                connector_frontier=(goal.get('basis')=='connector_observation_frontier'
                    and layout.get('schema')==1 and layout.get('coordinate_space')=='current_image'
                    and layout.get('size')==list(self.anchor[3].size)
                    and bool(layout.get('floor_regions') or layout.get('exterior_regions'))
                    and goal.get('requires_separate_surface_measurement') is True
                    and goal.get('requires_reobservation') is True
                    and any(c.get('kind')==goal.get('connector_kind')=='stairs'
                            and c.get('box')==goal.get('connector_box')
                            and c.get('requires_separate_surface_measurement') is True
                            for c in layout.get('connectors',[])))
                local_frontier=(self.goal_mode!='building_cross'
                    and (connector_frontier or goal.get('basis') in (
                        {'local_observation_frontier','building_task_search_region','return_observation_frontier'}
                        if self.goal_mode=='building_search' else {'local_observation_frontier'}))
                    and layout.get('schema')==1 and layout.get('coordinate_space')=='current_image'
                    and layout.get('size')==list(self.anchor[3].size)
                    and bool(layout.get('floor_regions') or layout.get('exterior_regions')))
                near_threshold = False
                if goal.get('basis') == 'threshold_reobservation':
                    from ..perception.ground_projection import finite_pair
                    destination = result.get('search_goal_world')
                    near_threshold = (self.goal_mode == 'building_cross'
                        and finite_pair(self.crossing_origin) and finite_pair(goal.get('threshold_origin'))
                        and tuple(goal['threshold_origin']) == tuple(self.crossing_origin)
                        and finite_pair(destination)
                        and isinstance(result.get('next_segment_world'),list)
                        and all(finite_pair(p) and self.router.distance(self.crossing_origin,p) <= 2.001
                                for p in result['next_segment_world'])
                        and self.router.distance(self.crossing_origin,destination) <= 2.001
                        and self.router.distance(nav.position,destination) <= 2.001
                        and layout.get('schema') == 1 and layout.get('coordinate_space') == 'current_image'
                        and layout.get('size') == list(self.anchor[3].size)
                        and goal.get('requires_reobservation') is True)
                    if not near_threshold:
                        return TravelCommand(outcome='building_entrance_evidence_missing')
                if (not isinstance(scene,dict) or scene.get('schema')!=1 or scene.get('executable') is not False
                        or not isinstance(scene.get('doorways'),list)
                        or scene.get('surface')=='structure'
                        or goal.get('basis')=='connector_observation_frontier' and not connector_frontier
                        or result.get('kind')=='route' and not scene['doorways']
                        and not (local_frontier or near_threshold)):
                    return TravelCommand(outcome='building_entrance_evidence_missing')
                self.building_scene=result['building_scene']
                self.building_measurement=result.get('ground_measurement')
                self.building_observation=(nav,heading,self.anchor[2],self.anchor[3].frame_id,self.anchor[3].size)
                ctx.log_event('quest_building_entrance_observation',dict(frame_id=result['frame_id'],
                    scene=self.building_scene,ground_measurement=result.get('ground_measurement'),
                    calibration_ready=result.get('kind')=='route'))
            if result.get('kind') in {'stair_probe','stair_surface_candidate'}:
                if (self.stair is None or not self.building
                        or result.get('executable') is not False or result.get('requires_fresh_feedback') is not True):
                    return TravelCommand(outcome='stair_trial_evidence_invalid')
                if self.distance_limit-self.total<.6 or self.pulses>=self.pulse_limit:
                    return TravelCommand(outcome='segmentation_budget_exhausted')
                command=self.stair.accept(result,now,nav,heading,coupled=self.calibration_coupled)
                if command.outcome=='stair_surface_observed':
                    ctx.log_event('quest_stair_surface_observed',dict(position=nav.position,
                        height=nav.height,moved_yards=self.stair.total,pulses=self.stair.pulses,
                        requires_new_measurement=True,passage_verified=False,level_id=None))
                    self.stair=None
                    self.discard_building_geometry()
                    self.goal_mode='building_search'
                    self.crossing_baseline=self.crossing_origin=self.crossing_confirmed=None
                    self.building_scene=self.building_measurement=self.building_observation=None
                    self.recapture_not_before=now+.5
                    return TravelCommand()
                if command.tap:
                    self.pulses+=1
                    ctx.log_event('quest_stair_probe_issued',dict(frame_id=result['frame_id'],
                        key=command.tap,hold_s=command.hold_s,metric_route=False))
                elif not command.outcome:
                    self.anchor=None;self.recapture_not_before=now+.4
                return command
            if result.get('kind')=='building_reobserve':
                if (not self.building or result.get('executable') is not False
                        or result.get('requires_fresh_feedback') is not True
                        or result.get('reason') not in BUILDING_REOBSERVE):
                    return TravelCommand(outcome='building_observation_invalid')
                if result['reason'].startswith('building_return_'):
                    edge=self.building_memory.return_edge()
                    if (edge is None or type(result.get('return_passage_id')) is not int
                            or result['return_passage_id']!=edge['passage_id']):
                        return TravelCommand(outcome='building_return_identity_changed')
                    self.return_passage=edge['passage_id']
                    if self.return_passage not in self.return_looked and self.deadline-now>=15:
                        self.discard_building_geometry()
                        self.return_look=PassageLook(now,nav,edge,self.convention)
                        self.return_looked.add(self.return_passage)
                        ctx.log_event('quest_building_return_look',dict(passage_id=self.return_passage,
                            current_area=self.building_memory.current,translation_authorized=False))
                        return TravelCommand()
                return self.recover_building_view(ctx,now,nav,result['reason'],heading)
            if result.get('kind') in {'calibration_orbit','calibration_probe','calibration_observe'}:
                kind=result['kind'];evidence=result.get('calibration',{})
                if (result.get('executable') is not False or result.get('requires_fresh_feedback') is not True
                    or kind=='calibration_orbit' and (evidence.get('stage')!='orbit'
                        or type(result.get('yaw_pixels')) is not int or not 0<abs(result['yaw_pixels'])<=220)
                    or kind=='calibration_probe' and (evidence.get('stage')!='translation'
                        or result.get('key') not in {'w','s','q','e'} or result.get('hold_s')!=.07
                        or result.get('metric_route') is not False)
                    or kind=='calibration_observe' and (evidence.get('stage')!='observe'
                        or result.get('wait_s')!=.7)):
                    return TravelCommand(outcome='auto_calibration_action_invalid')
                self.calibration_action=result
                ctx.log_event('quest_automatic_calibration_state',dict(frame_id=result['frame_id'],**evidence))
                return TravelCommand()
            # Only review artifacts produced by our bounded local pipeline qualify.
            from ..perception.ground_projection import finite_pair
            points=result.get('next_segment_world',[])
            budget=result.get('movement_budget_yards')
            evidence=result.get('registration',{})
            bound=evidence.get('combined_bound_yards')
            try:
                window=RouteWindow(result,nav.world_size,self.anchor[2]) if 'execution_window' in result else None
            except (KeyError,TypeError,ValueError):return TravelCommand(outcome='segmentation_window_invalid')
            goal_kind = ('building_entry_region' if self.building else
                         'automatic_travel_frontier' if self.goal_mode=='travel' else 'automatic_npc_search_region')
            if (result.get('executable') is not False or result.get('requires_fresh_feedback') is not True
                    or result.get('calibration_source')!='automatic_native_motion'
                    or evidence.get('fit_movements',0)<2 or evidence.get('validation_movements',0)<2
                    or result.get('automatic_goal',{}).get('goal_kind')!=goal_kind
                    or evidence.get('current_frame')!=self.anchor[3].frame_id
                    or not height_matches([0.,0.],0.,evidence.get('surface_height'))
                    or type(bound) not in (int,float) or not 0<=bound<=.35
                    or type(budget) not in (int,float) or not 0<budget<=(window.limit if window else .75)
                    or not isinstance(points,list) or not 2<=len(points)<=100
                    or any(not finite_pair(p) or not all(0<=v<=1 for v in p) for p in points)
                    or self.router.distance(points[0],nav.position)>.08
                    or sum(self.router.distance(a,b) for a,b in zip(points,points[1:]))>budget+.001):
                return TravelCommand(outcome='segmentation_plan_invalid')
            return_id=result['automatic_goal'].get('return_passage_id')
            if return_id is not None or self.return_passage is not None:
                edge=self.building_memory.return_edge()
                if (edge is None or type(return_id) is not int or return_id!=edge['passage_id']
                        or result['automatic_goal'].get('from_area')!=self.building_memory.current
                        or result['automatic_goal'].get('to_area')!=edge['from_area']):
                    return TravelCommand(outcome='building_return_identity_changed')
                self.return_passage=return_id
            goal_position = result.get('search_goal_world')
            if (self.goal_mode in {'building_entry','building_search'}
                    and result['automatic_goal'].get('basis') == 'door_threshold'
                    and window is not None and now < window.expires_at
                    and finite_pair(goal_position)
                    and all(self.router.distance(nav.position,p) <= .06
                            for p in [goal_position,*points])):
                return self.reached_threshold(ctx,nav,result['frame_id'],'measured_current_position')
            self.plan=result;self.window=window
            diagnostics = result.get('model_diagnostics', {})
            ctx.log_event('quest_segmented_plan_ready',dict(frame_id=result['frame_id'],
                cache=result.get('calibration_cache_status'),bound_yards=bound,
                prefix_yards=budget,window=bool(window),
                capture_to_plan_ms=round((time.monotonic()-self.anchor[3].captured_at)*1000, 2),
                sam_device=diagnostics.get('sam_device'),sam_fallback=diagnostics.get('sam_fallback'),
                model_timings=result.get('model_timings'),
                observation_s=round(now-self.anchor[2],3),remaining_s=round(self.deadline-now,3)))
            return TravelCommand()
        if self.pending is not None:
            exposure, capture_ms, encoding_ms = self.pending
            old,angle,stamp,frame=self.anchor
            if nav.sequence==old.sequence:return TravelCommand()
            if now-stamp>.25:return TravelCommand(outcome='segmentation_capture_unsynchronized')
            job=dict(automatic=True,session_id=self.session_id,models=str(ROOT/self.config.models),
                     checkpoint=str(ROOT/self.config.checkpoint),device=self.config.device,
                     quest_id=nav.quest_id,phase=nav.phase.name,objective_index=nav.objective_index,
                     convention=self.convention,blocked=self.blocked,visited=self.visited,
                     motion_history=self.motion_history,strafe_verified=nav.strafe_verified,
                     destination=self.travel_goal if self.goal_mode=='travel' else nav.destination,
                     goal_mode=self.goal_mode,passages=self.building_memory.incident(),
                     navigation_context=self.building_memory.snapshot(),return_passage_id=self.return_passage,
                     crossing_origin=self.crossing_origin,
                     native_outdoors=bool(nav.flags & TravelFlags.OUTDOORS),
                     visual_ui_hidden=nav.visual_ui_hidden,
                     full_window=self.full_window_measurement and old.camera.view==4,
                     calibration_cache=str(ROOT/self.config.calibration_cache))
            if self.stair is not None:job['stair_trial']=self.stair.job()
            if self.rear_contact and self.router.distance(self.rear_contact[0],nav.position)<=.55:
                # A failed input identifies a direction at this position, not
                # the shape or location of a wall. Respect it after a camera turn.
                left=math.pi/2 if self.convention=='ccw_west' else -math.pi/2
                job['blocked_probe_keys']=[key for key,offset in
                    (('w',0),('s',math.pi),('q',left),('e',-left))
                    if abs(heading_error(heading+offset,self.rear_contact[1]))<math.radians(45)]
            self.request=self.request_factory(exposure,frame,job);self.pending=None;self.calls+=1
            ctx.log_event('quest_segmented_observation',dict(frame_id=frame.frame_id,call=self.calls,
                capture_ms=capture_ms,encoding_ms=encoding_ms,
                capture_to_submit_ms=round((time.monotonic()-frame.captured_at)*1000, 2),
                captured_sequence=old.sequence,confirmed_sequence=nav.sequence))
            return TravelCommand()
        if now<self.recapture_not_before:return TravelCommand()
        capture_started=time.monotonic()
        if self.full_window_measurement and nav.camera.view==4:
            image=self.overview_capture(ctx.capture)
            height,width=image.shape[:2]
            if width>1012:
                image=cv2.resize(image,(1012,round(height*1012/width)),interpolation=cv2.INTER_AREA)
        else:
            image=self.capture(ctx.capture)
        capture_finished = time.monotonic()
        captured_at=(capture_started+capture_finished)/2
        encoded, buffer = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not encoded:
            return TravelCommand(outcome='segmentation_capture_encoding_failed')
        png = buffer.tobytes()
        digest = hashlib.sha256(png).hexdigest()
        encoding_finished = time.monotonic()
        frame=ProjectionFrame(uuid4().hex,digest,(image.shape[1],image.shape[0]),nav.map_id,self.floor_id,
            nav.world_size,nav.position,None,nav.height,heading,(nav.camera.view,nav.camera.zoom,nav.camera_epoch),captured_at)
        self.anchor=(nav,heading,now,frame)
        # The local worker saves the exact bytes hashed at exposure. Injected
        # planners can still consume the original RGB array in memory.
        exposure = png if self.request_factory is LocalRouteRequest else image
        self.pending = (exposure, round((capture_finished-capture_started)*1000, 2),
                        round((encoding_finished-capture_finished)*1000, 2))
        return TravelCommand()
