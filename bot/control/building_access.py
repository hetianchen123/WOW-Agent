"""Task-owned building access: surface review, flight recovery, grounded entry.

Native XY / !IsFlying cannot identify a floor. Only local image evidence can
authorize a landing; fallback air targets come from previously reviewed ground.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from threading import Event
import time
from uuid import uuid4

import cv2

from ..core.paths import ROOT, DATA_DIR
from ..perception.travel_channel import TravelFlags as F, TaskFlags, LifeState, ObjectiveAction
from ..perception.quest_channel import QuestState
from .pathfinding import GridRouter
from .quest_travel import QuestTrip, TravelCommand
from .segmented_detour import local_worker
from .building_relocation import local_backoff_evidence
from .overhead_camera import OverheadObservation

GROUND_MEMORY_SECONDS = 6 * 3600  # A candidate destination, always reviewed again after landing.


def building_target_receipt(nav, observation, now):
    """Hand off a native reachable target, never a map pin or indoor flag."""
    if all(nav.task & flag for flag in (
            TaskFlags.NPC_SELECTED,TaskFlags.NPC_RANGE_KNOWN,TaskFlags.NPC_WITHIN_5_YARDS)):
        return dict(basis='native_npc_range',navigation_sequence=nav.sequence)
    if (nav.phase!=QuestState.ACTIVE or nav.objective_action!=ObjectiveAction.COMBAT
            or not nav.objective_target or observation is None
            or observation.navigation.value!=nav or not observation.navigation.fresh(now,.75)
            or not observation.combat.fresh(now,.75)):
        return None
    combat=observation.combat.value
    if (combat.target_id!=nav.objective_target or combat.target_dead or combat.unsafe
            or not combat.key_verified or combat.in_range is not True or combat.line_of_sight_blocked):
        return None
    return dict(basis='native_quest_target_in_range',target_id=combat.target_id,
        target_epoch=combat.state_epoch,combat_sequence=combat.sequence,
        navigation_sequence=nav.sequence)


def building_identity(nav):
    return dict(quest_id=nav.quest_id,map_id=nav.map_id,phase=int(nav.phase),
                objective_index=nav.objective_index,destination=list(nav.destination),
                world_size=list(nav.world_size))


class BuildingSceneRequest:
    def __init__(self, image, config, camera_view=None, native_outdoors=None, *, full_window=False,
                 visual_ui_hidden=None,mode='building_scene'):
        self.frame_id=uuid4().hex
        self.folder=DATA_DIR/'terrain'/('building-'+self.frame_id)
        self.folder.mkdir(parents=True)
        path=self.folder/'image.png'
        if not cv2.imwrite(str(path),cv2.cvtColor(image,cv2.COLOR_RGB2BGR)):
            raise ValueError('building_capture_failed')
        self.digest=hashlib.sha256(path.read_bytes()).hexdigest()
        self.cancelled=Event()
        self.pool=ThreadPoolExecutor(max_workers=1)
        job=dict(mode=mode,frame_id=self.frame_id,models=str(ROOT/config.models),
                 checkpoint=str(ROOT/config.checkpoint),device=config.device,camera_view=camera_view,
                 native_outdoors=native_outdoors,full_window=full_window,visual_ui_hidden=visual_ui_hidden)
        (self.folder/'request.json').write_text(json.dumps(job,indent=2))
        def guard():
            if self.cancelled.is_set(): raise ValueError('building_observation_cancelled')
        self.future=self.pool.submit(lambda:local_worker().inspect([path],target=job,guard=guard,timeout=20)[0])

    def poll(self):
        if not self.future.done(): return None
        result=self.future.result()
        if result.get('frame_id')!=self.frame_id or result.get('image_sha256')!=self.digest:
            raise ValueError('building_observation_identity_changed')
        return result

    def close(self):
        self.cancelled.set()
        self.pool.shutdown(wait=False,cancel_futures=True)


@dataclass(frozen=True)
class ExteriorPoint:
    map_id: int
    world_size: tuple
    position: tuple
    at: float
    image: str
    digest: str


class BuildingAccess:
    def __init__(self, config, *, capture, convention, request_factory=BuildingSceneRequest,
                 entry_factory=None, memory_path=None):
        self.config,self.capture,self.convention=config,capture,convention
        self.request_factory,self.entry_factory=request_factory,entry_factory
        self.memory_path=Path(memory_path) if memory_path else None
        self.points=[]
        self.intents=[]
        if self.memory_path:
            try:
                raw=json.loads(self.memory_path.read_text())
                for row in raw['points']:
                    p=ExteriorPoint(**row)
                    if (len(p.position)==2 and len(p.world_size)==2 and min(p.world_size)>0
                            and all(math.isfinite(v) and 0<=v<=1 for v in p.position)
                            and 0<=time.time()-p.at<=GROUND_MEMORY_SECONDS and Path(p.image).is_file()
                            and hashlib.sha256(Path(p.image).read_bytes()).hexdigest()==p.digest):
                        self.points.append(p)
                for row in raw.get('building_intents',[]):
                    if (isinstance(row.get('identity'),dict) and type(row.get('at')) in (int,float)
                            and 0<=time.time()-row['at']<=GROUND_MEMORY_SECONDS
                            and Path(row.get('image','')).is_file()
                            and hashlib.sha256(Path(row['image']).read_bytes()).hexdigest()==row.get('digest')):
                        self.intents.append(row)
            except (OSError,ValueError,KeyError,TypeError):
                self.points=[]
                self.intents=[]
        self.identity=None
        self.reset()

    def interrupt(self):
        if self.request:self.request.close();self.request=None
        if self.entry:self.entry.close();self.entry=None
        self.recovery=None;self.anchor=None;self.settle=None
        self.entry_observation=None
        self.dismount_receipt=None;self.surface_view=None
        self.target_receipt=None
        self.review_ui_state=None
        self.state='initial'  # Retain the building/ground-only policy across defense.

    def reset(self):
        self.request=self.anchor=self.recovery=self.entry=None
        self.state='initial'
        self.airborne=False
        self.ground_only=False
        self.roof_recoveries=0
        self.failures=0
        self.settle=None
        self.dismount_receipt=None
        self.dismount_next=None
        self.dismount_reobserve_used=False
        self.surface_view=None
        self.surface_view_used=False
        self.review_context='initial'
        self.review_ui_state=None
        self.started=None
        self.sequence=None;self.sequence_at=None
        self.target_ground=None
        self.last_review_position=None
        self.last_review=None
        self.entry_observation=None
        self.scan_turns=0;self.scan_at=0
        self.heading_wait=None
        self.search_inside=False
        self.target_receipt=None

    def close(self):
        if self.request:self.request.close()
        if self.entry:self.entry.close()
        self.reset()

    @property
    def locked(self):
        # ground_travel hands a camera-bound observation to entry on the next tick.
        return self.state not in {'pass', 'airborne', 'inside'}

    def _remember(self,nav,request):
        if request is None:return
        p=ExteriorPoint(nav.map_id,tuple(nav.world_size),tuple(nav.position),time.time(),
                        str(request.folder/'image.png'),request.digest)
        router=GridRouter(*nav.world_size)
        self.points=[old for old in self.points if old.map_id!=p.map_id
                     or router.distance(old.position,p.position)>5][-15:]+[p]
        self._save_memory()

    def _save_memory(self):
        if self.memory_path:
            self.memory_path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.memory_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'schema':2,'points':[asdict(v) for v in self.points],
                'building_intents':self.intents[-16:]},indent=2))
            temporary.replace(self.memory_path)

    def remember_building(self,nav,*,image,digest,at=None):
        """Persist the roof/entry task, not a successful route or indoor position."""
        identity=building_identity(nav)
        self.intents=[r for r in self.intents if r.get('identity')!=identity]
        self.intents.append(dict(identity=identity,at=time.time() if at is None else at,
                                 image=str(image),digest=digest))
        self._save_memory()

    def _resume_building(self,ctx,nav):
        identity=building_identity(nav)
        if not self.ground_only and any(r.get('identity')==identity and
                0<=time.time()-r['at']<=GROUND_MEMORY_SECONDS for r in self.intents):
            self.ground_only=True
            ctx.log_event('quest_building_resumed',dict(identity,ground_only=True,
                          entrance_verified=False))

    def _fallback(self,nav):
        router=GridRouter(*nav.world_size)
        candidates=[p for p in self.points if p.map_id==nav.map_id and tuple(p.world_size)==tuple(nav.world_size)
                    and 0<=time.time()-p.at<=GROUND_MEMORY_SECONDS and 10<=router.distance(p.position,nav.position)<=500]
        return min(candidates,key=lambda p:router.distance(p.position,nav.position),default=None)

    def _review(self,now,context=None):
        self.state='review';self.settle=None;self.anchor=None
        self.target_receipt=None
        self.review_started=now
        if context is not None:self.review_context=context

    def _begin_dismount(self,ctx,now,nav,next_state):
        # Stable !FLYING/!FALLING confirms a supporting surface, even on a roof.
        # Dismounting removes visual occlusion; it does NOT confirm exterior ground.
        if nav.flags & (F.FLYING|F.FALLING|F.SWIMMING|F.CASTING|F.IN_COMBAT):
            return TravelCommand(outcome='building_dismount_unsafe')
        self.dismount_receipt=(now,nav.sequence)
        self.dismount_next=next_state;self.state='dismount'
        key=nav.sky_dismount_key or '8'
        ctx.log_event('quest_building_dismount',dict(key=key,sequence=nav.sequence,
            next_state=next_state,exterior_verified=next_state=='ground_travel'))
        return TravelCommand(tap=key)

    def step(self,ctx,now,nav,heading,*,dialogue_open=False,in_combat=False,target_receipt=None):
        identity=(nav.quest_id,nav.map_id,nav.phase,nav.objective_index,nav.destination)
        if identity!=self.identity:
            self.close();self.identity=identity
        if (in_combat or nav.flags & F.IN_COMBAT or dialogue_open
                or nav.recovery and nav.recovery.life!=LifeState.ALIVE):
            # Defense/death/dialogue own control. Reobserve on resumption.
            self.interrupt()
            return None
        if (not nav.position or not nav.world_size or not nav.destination
                or not nav.flags & F.KEYS_VERIFIED):
            return TravelCommand(outcome='building_state_unavailable')
        if self.state=='initial':self._resume_building(ctx,nav)
        if self.sequence!=nav.sequence:self.sequence,self.sequence_at=nav.sequence,now
        if self.sequence_at is not None and now-self.sequence_at>1.2:
            self.close();return TravelCommand(outcome='building_telemetry_stale')
        if heading is None or ctx.heading_age()>.75:
            if self.heading_wait is None:
                self.heading_wait=now
                ctx.log_event('quest_building_waiting_heading',dict(sequence=nav.sequence,
                    heading_age=ctx.heading_age(),max_wait_s=1.,movement_authorized=False))
            return TravelCommand(outcome='building_heading_stale') if now-self.heading_wait>=1 else TravelCommand()
        self.heading_wait=None
        router=GridRouter(*nav.world_size)
        if self.started is None:self.started=now
        if self.ground_only and now-self.started>180:
            return TravelCommand(outcome='building_access_budget')
        if nav.flags & F.FLYING:
            self.airborne=True
            if self.request:self.request.close();self.request=None
            if self.recovery:
                command=self.recovery.step(now,nav,heading,heading_age=ctx.heading_age())
                return command
            self.state='airborne'
            return None
        if nav.flags & (F.FALLING|F.CASTING):
            return TravelCommand() if self.locked or self.airborne else None
        if nav.flags & F.SWIMMING:return TravelCommand(outcome='building_water_landing')
        receipt=building_target_receipt(nav,None,now) or (
            target_receipt if target_receipt and target_receipt.get('navigation_sequence')==nav.sequence else None)
        if self.ground_only and not nav.flags & F.MOUNTED and receipt:
            if self.entry:self.entry.close();self.entry=None
            if self.request:self.request.close();self.request=None
            if self.state!='inside':
                ctx.log_event('quest_building_target_handoff',dict(receipt,position=nav.position,
                    action_handoff=True,task_completed=False,level_id=None))
            self.state='inside';self.target_receipt=receipt
            return None
        if self.state=='inside':
            # Losing a target's range cannot restore unobserved direct steering.
            self.target_receipt=None;self.search_inside=True;self.state='ground_travel'
        if self.airborne:
            self.airborne=False;self._review(now,'landing')
        if self.state=='initial':self._review(now)
        if self.state=='dismount':
            at,sequence=self.dismount_receipt
            if now-at>4:return TravelCommand(outcome='building_dismount_unconfirmed')
            if nav.flags & F.MOUNTED or nav.sequence==sequence or now-at<.3:
                return TravelCommand()
            self.dismount_receipt=None;self.settle=None
            if self.dismount_next=='review':
                self._review(now,'after_dismount')
            else:self.state=self.dismount_next
            return TravelCommand()
        if self.state=='surface_view':
            command=self.surface_view.step(ctx,now,nav)
            if command is not None:return command
            # Changing the camera does not change whether this is an initial
            # on-foot observation or a landing with an unconfirmed surface.
            self.surface_view=None;self._review(now)
            return TravelCommand()
        if self.state=='recover':
            # Retake off; NEVER walk/jump off the roof or accept a ground fallback.
            command=self.recovery.step(now,nav,heading,heading_age=ctx.heading_age())
            if self.recovery.mode in {'walk','mount'} or 'fly' in self.recovery.failed_modes:
                return TravelCommand(outcome='building_roof_flight_unavailable')
            return command
        if self.state=='review':
            if now-self.review_started>24:return TravelCommand(outcome='building_observation_timeout')
            if self.settle is None:
                self.settle=(now,nav.sequence,nav.position);return TravelCommand()
            at,sequence,position=self.settle
            if router.distance(position,nav.position)>.3:
                if self.request:self.request.close();self.request=None
                self.settle=None;return TravelCommand()
            if now-at<.5 or nav.sequence==sequence:return TravelCommand()
            if self.request is None:
                visibility=dict(visual_ui_hidden=nav.visual_ui_hidden) if nav.visual_ui_hidden is not None else {}
                image=self.capture(ctx.capture)
                self.request=self.request_factory(image,self.config,
                    nav.camera.view if nav.camera else None,bool(nav.flags & F.OUTDOORS),**visibility)
                self.anchor=(nav,heading,now,self.request.frame_id,(image.shape[1],image.shape[0]))
                self.review_ui_state=nav.visual_ui_hidden
                ctx.log_event('quest_building_observation',dict(state=self.state,position=nav.position,
                    frame_id=self.request.frame_id,airborne=False))
                return TravelCommand()
            captured,angle,at,frame_id,size=self.anchor
            if (router.distance(captured.position,nav.position)>.3 or captured.camera!=nav.camera
                    or captured.camera_epoch!=nav.camera_epoch
                    or nav.visual_ui_hidden!=self.review_ui_state
                    or abs((heading-angle+math.pi)%math.tau-math.pi)>math.radians(2) or now-at>20):
                self.request.close();self.request=None;self.settle=None
                return TravelCommand()
            try:result=self.request.poll()
            except (OSError,ValueError,RuntimeError) as exc:
                ctx.log_event('quest_building_observation_failed',dict(error=str(exc),
                              frame_id=self.request.frame_id,context=self.review_context))
                return TravelCommand(outcome='building_observation_failed')
            if result is None:return TravelCommand()
            request,self.request=self.request,None
            request.close()
            if result.get('schema')!=1 or result.get('executable') is not False:
                return TravelCommand(outcome='building_observation_invalid')
            surface=result.get('surface')
            self.last_review=result;self.last_review_position=nav.position
            ctx.log_event('quest_building_surface',dict(result,position=nav.position,
                sequence=nav.sequence,state=self.state))
            if surface=='structure':
                self.ground_only=True
                self.remember_building(nav,image=request.folder/'image.png',digest=request.digest)
                point=self._fallback(nav)
                if self.roof_recoveries>=1 or point is None:
                    return TravelCommand(outcome='building_exterior_landing_unavailable')
                if not nav.flags & F.CAN_FLY:return TravelCommand(outcome='building_roof_flight_unavailable')
                self.target_ground=point
                self.recovery=QuestTrip(nav.world_size,point.position,self.convention,arrival_radius=2,
                    log_event=ctx.log_event,flight_threshold=5)
                self.state='recover';self.roof_recoveries+=1;self.started=now
                ctx.log_event('quest_building_roof_recovery',dict(destination=point.position,
                    source_image=point.image,basis='reviewed_exterior_ground',needs_new_landing_review=True))
                return TravelCommand()
            if surface=='exterior':
                # Glide can stop before the exact point. A fresh exterior review
                # authorizes this new ground surface, not a radius around old XY.
                self._remember(nav,request)
                self.recovery=None
                self.state='ground_travel' if self.ground_only else 'pass'
                self.settle=None
                self.failures=0
                return TravelCommand()
            if (not nav.flags & F.OUTDOORS and (surface=='interior_floor'
                    or result.get('layout',{}).get('floor_regions'))):
                self.state='ground_travel';self.ground_only=True;self.search_inside=True
                self.recovery=None
                return TravelCommand()
            # Two identical, mount-occluded frames are not two independent failures.
            # First remove the occluder, then change the view before giving up.
            if surface=='unknown' and nav.flags & F.MOUNTED and not self.dismount_reobserve_used:
                self.dismount_reobserve_used=True
                return self._begin_dismount(ctx,now,nav,'review')
            # Cold starts and expired task memories can rediscover an entrance
            # from the current image. A local patch is useful even when the
            # facade dominates the support-surface vote.
            local_ground=(surface=='unknown' and not nav.flags & F.MOUNTED
                and self.recovery is None and result.get('frame_id')==frame_id
                and local_backoff_evidence(result,size) is not None)
            visible_entry=local_ground and bool(result.get('doorways'))
            obscured_facade=(local_ground and not visible_entry and result.get('building_visible') is True
                and self.review_context=='initial' and not self.roof_recoveries
                and bool(nav.flags & F.OUTDOORS) and self.config.building_access)
            if visible_entry or obscured_facade:
                self.ground_only=True
                if obscured_facade or all(d.get('threshold_observed') is False for d in result['doorways']):
                    self.entry_observation=self.anchor
            if (surface=='unknown' or surface=='water' and (
                    result.get('layout',{}).get('floor_regions') or result.get('layout',{}).get('exterior_regions'))
                    ) and self.ground_only and not nav.flags & F.MOUNTED:
                # A coarse surface vote may be hidden by a facade/actors. It
                # must not gate stationary local measurement of a known entry
                # task. This is NOT an exterior receipt or permission to walk
                # toward the quest pin. Entry retains fresh geometry, corridor,
                # roof/water and native-motion gates, just like NPC detours.
                self.recovery=None;self.settle=None;self.state='ground_travel'
                ctx.log_event('quest_building_local_observation',dict(
                    frame_id=result.get('frame_id',request.frame_id),surface=surface,
                    basis=('obstructed_facade_and_local_ground' if obscured_facade else
                           'current_door_and_local_ground' if visible_entry else 'pending_entry_needs_local_evidence'),
                    exterior_verified=False,
                    translation_authorized=False,door_candidates=len(result.get('doorways',[]))))
                return TravelCommand()
            if surface=='unknown' and not nav.flags & F.MOUNTED and not self.surface_view_used:
                self.surface_view_used=True;self.surface_view=OverheadObservation();self.state='surface_view'
                ctx.log_event('quest_building_surface_reframe',dict(reason='surface_occluded',
                              previous_context=self.review_context,view=4,movement_authorized=False))
                return TravelCommand()
            self.failures+=1
            if self.failures>=2:return TravelCommand(outcome='building_surface_unconfirmed')
            self._review(now);return TravelCommand()
        if self.state=='ground_travel':
            if nav.flags & F.MOUNTED:
                return self._begin_dismount(ctx,now,nav,'ground_travel')
            self.settle=None
            # A task pin inside a building is never an exterior approach goal.
            # Start looking for the entrance here, even when the pin is far away.
            if self.entry_factory is None:return TravelCommand(outcome='building_entry_planner_unavailable')
            self.entry=self.entry_factory(now,nav,ctx)
            if self.search_inside:self.entry.goal_mode='building_search'
            if self.last_review and hasattr(self.entry,'seed_building_observation'):
                self.entry.seed_building_observation(self.last_review, self.entry_observation)
            self.entry_observation=None
            self.state='entry'
            ctx.log_event('quest_building_entry_started',dict(position=nav.position,destination=nav.destination))
            return TravelCommand()
        if self.state=='entry':
            command=self.entry.step(ctx,now,nav,heading)
            if command.outcome=='building_entered':
                if not building_target_receipt(nav,None,now):
                    return TravelCommand(outcome='building_target_receipt_missing')
                self.entry.close();self.entry=None;self.state='inside'
                ctx.log_event('quest_building_entered',dict(position=nav.position,range_verified=bool(
                    nav.task & TaskFlags.NPC_WITHIN_5_YARDS),indoors=not bool(nav.flags & F.OUTDOORS)))
                return TravelCommand()
            return command
        return None
