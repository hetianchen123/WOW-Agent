"""Change observation position on a recent measured path, without image scale.

This is a one-shot reversal of input-correlated native ground segments. It
cannot invent a rear route, infer floors, or reuse a screenshot after movement.
"""
from dataclasses import replace
import math

from .ground_trace import UNSAFE, blocked_segment
from .navigation import compass_from_delta, heading_error
from .quest_travel import TravelCommand
from .building_memory import NativeTurn, same_camera_controls
from ..perception.surface_height import bounded_height_change
from ..perception.travel_channel import TravelFlags, LifeState


def local_backoff_evidence(scene, size):
    """A nearby visible surface permits a bounded experiment, not a rear route.

    The avatar may be hidden. No screen point is converted to a world point;
    the eventual S pulse is relative to native facing and checked by native XY.
    """
    if (not isinstance(scene, dict) or scene.get('schema') != 1
            or scene.get('executable') is not False
            or scene.get('surface') not in {'unknown', 'exterior', 'interior_floor'}):
        return None
    layout = scene.get('layout') or {}
    if (layout.get('schema') != 1 or layout.get('coordinate_space') != 'current_image'
            or layout.get('size') != list(size)):
        return None
    width, height = size
    best_box, best_support = None, 0
    for region in [*layout.get('floor_regions', []), *layout.get('exterior_regions', [])]:
        box = region.get('box', [])
        if (len(box) != 4 or not all(type(v) in (int, float) and math.isfinite(v) for v in box)
                or region.get('pixels', 0) < 16):
            continue
        overlap_w = max(0, min(box[2], .9 * width) - max(box[0], .1 * width))
        overlap_h = max(0, min(box[3], .9 * height) - max(box[1], .55 * height))
        support = min(region['pixels'], overlap_w * overlap_h)
        if support > best_support:
            best_box, best_support = box, support
    if best_box is not None:
        return dict(basis='local_surface_backoff_probe', local_surface_box=best_box,
                    rear_path_verified=False, projection_calibrated=False)
    return None


def relocation_blocked(router, blocked_positions, origin, target):
    if blocked_segment(router, origin, target):
        return True
    vector = [(b-a)*s for a,b,s in zip(origin,target,(router.width,router.height))]
    length = math.hypot(*vector)
    if length < 1e-9:
        return False
    for point in blocked_positions:
        delta = [(b-a)*s for a,b,s in zip(origin,point,(router.width,router.height))]
        along = sum(a*b for a,b in zip(delta,vector))/length
        cross = abs(delta[0]*vector[1]-delta[1]*vector[0])/length
        if 0 < along <= length+.1 and cross < .3:
            return True
    return False


class BuildingBackoffProbe:
    """One no-history repositioning attempt, with released and measured S taps.

    This deliberately explores an unknown rear direction. It does not certify
    a floor, doorway, calibrated route, or improved visibility. Falling flags,
    new obstacles and unexpected movement end the experiment immediately.
    """
    basis = 'local_surface_backoff_probe'
    key = 's'
    distance_cap = 2.
    pulse_cap = 12

    def __init__(self, now, nav, heading, router, convention, *, deadline,
                 blocked_positions, max_distance=2., max_pulses=12):
        self.limit = min(self.distance_cap, max_distance)
        self.max_pulses = min(self.pulse_cap, max_pulses)
        if (self.limit < .5 or self.max_pulses < 2 or deadline-now < 2
                or heading is None or not math.isfinite(heading)):
            raise ValueError('building_backoff_budget_unavailable')
        self.started = now
        self.deadline = min(deadline, now+12)
        self.origin = self.last_position = nav.position
        self.heading, self.height = heading, nav.height
        self.camera, self.camera_epoch = nav.camera, nav.camera_epoch
        self.identity = (nav.quest_id, nav.map_id, nav.phase, nav.objective_index,
                         nav.objective_target, nav.world_size, nav.destination, nav.destination_map)
        self.router, self.convention = router, convention
        self.blocked_positions = blocked_positions
        self.sequence, self.sequence_at = nav.sequence, now
        self.receipts = set()
        self.pulse = None
        self.pulses, self.total = 0, 0.
        self.speeds = []

    def probe_point(self, position, yards):
        if self.key=='w':yards=-yards
        east = math.sin(self.heading) * (1 if self.convention == 'ccw_west' else -1)
        south = math.cos(self.heading)
        return (position[0]+east*yards/self.router.width,
                position[1]+south*yards/self.router.height)

    def step(self, ctx, now, nav, heading):
        identity = (nav.quest_id, nav.map_id, nav.phase, nav.objective_index,
                    nav.objective_target, nav.world_size, nav.destination, nav.destination_map)
        if (identity != self.identity or nav.position is None or nav.flags & UNSAFE
                or not nav.flags & TravelFlags.KEYS_VERIFIED
                or nav.recovery and nav.recovery.life != LifeState.ALIVE
                or getattr(nav.camera, 'paused', False)
                or not (same_camera_controls(nav.camera,self.camera) if self.key=='w' else nav.camera==self.camera)
                or nav.camera_epoch != self.camera_epoch):
            return TravelCommand(outcome='probe_interrupted')
        if heading is None or ctx.heading_age() > .55:
            return TravelCommand(outcome='probe_heading_stale')
        if abs(heading_error(heading, self.heading)) > math.radians(5):
            return TravelCommand(outcome='probe_pose_changed')
        # A change can veto this flat recovery; an equal (possibly constant)
        # height value is NOT proof that two positions are on the same floor.
        if (nav.height is not None and self.height is not None
                and not (bounded_height_change(self.router.distance(self.last_position,nav.position),
                                               nav.height-self.height) if self.key=='w'
                         else math.isfinite(nav.height) and abs(nav.height-self.height)<=.25)):
            return TravelCommand(outcome='probe_height_drop')
        if nav.sequence != self.sequence:
            self.sequence, self.sequence_at = nav.sequence, now
            self.receipts.add(nav.sequence)
        if now-self.sequence_at > .75:
            return TravelCommand(outcome='probe_telemetry_stale')
        if now >= self.deadline:
            return TravelCommand(outcome='building_relocation_budget')
        if self.pulse:
            at, origin, sequence, hold = self.pulse
            moved = self.router.distance(origin, nav.position)
            delta = [(b-a)*s for a,b,s in zip(origin,nav.position,nav.world_size)]
            rear = [(b-a)*s for a,b,s in zip(origin,self.probe_point(origin,1),nav.world_size)]
            along = sum(a*b for a,b in zip(delta,rear))
            cross = abs(delta[0]*rear[1]-delta[1]*rear[0])
            if moved > (1.1 if self.key=='w' else .8) or along < -.03 or cross > .12 or self.total+moved > self.limit+.1:
                return TravelCommand(outcome='probe_pose_changed')
            if now-at < hold+.25 or len(self.receipts) < 2:
                return TravelCommand()
            self.pulse = None
            self.total += moved
            self.last_position = nav.position
            if self.key=='w':self.height=nav.height
            ctx.log_event('quest_building_relocation_feedback', dict(
                basis=self.basis, from_position=origin, position=nav.position,
                before_sequence=sequence, after_sequences=sorted(self.receipts),
                moved_yards=moved, total_yards=self.total,
                rear_path_verified=False, route_to_target_verified=False))
            if moved < .03 or along < .02:
                return TravelCommand(outcome='building_relocation_blocked')
            self.speeds.append(moved/hold*1.35)
            if self.total >= self.limit-.06:
                return TravelCommand(outcome='building_relocated')
            if self.pulses >= self.max_pulses:
                return TravelCommand(outcome='building_relocated' if self.total >= .5
                                     else 'building_relocation_budget')
            return TravelCommand()
        if self.router.distance(self.last_position, nav.position) > .08:
            return TravelCommand(outcome='probe_pose_changed')
        # First release all existing input and verify a stable native pose.
        if now-self.started < .35 or len(self.receipts) < 2:
            return TravelCommand()
        if self.deadline-now < 1.2 or self.pulses >= self.max_pulses:
            return TravelCommand(outcome='building_relocation_budget')
        remaining = self.limit-self.total
        speed = max([7. if self.key=='w' else 4.5, *self.speeds])
        hold = min(.12 if self.speeds else .07, remaining/speed)
        if hold < .005:
            return TravelCommand(outcome='building_relocated')
        # Reject known collisions in the immediate probe envelope. This point
        # is a direction/budget check, never a waypoint inferred from pixels.
        target = self.probe_point(nav.position, min(remaining, speed*hold+.1))
        if (not all(0 <= v <= 1 for v in target)
                or relocation_blocked(self.router, self.blocked_positions, nav.position, target)):
            return TravelCommand(outcome='building_relocation_blocked')
        self.pulses += 1
        self.receipts = set()
        self.pulse = (now, nav.position, nav.sequence, hold)
        ctx.log_event('quest_building_relocation_pulse', dict(
            basis=self.basis, pulse=self.pulses, position=nav.position,
            sequence=nav.sequence, heading=heading, key=self.key, hold_s=hold,
            rear_path_verified=False, projection_calibrated=False))
        return TravelCommand(tap=self.key, hold_s=hold, precise=True)


class ForwardProbe(BuildingBackoffProbe):
    basis='turnaway_forward_probe'
    key='w'
    distance_cap=4.
    pulse_cap=20


class TurnawayRelocation:
    """Face away, walk out in observed short legs, then look back at the entrance."""
    basis='turn_away_from_building'

    def __init__(self,now,nav,heading,router,convention,*,deadline,blocked_positions,
                 max_distance,max_pulses,observe):
        self.origin=nav.position
        self.original_heading=heading
        self.reverse=(heading+math.pi)%math.tau
        self.router,self.convention=router,convention
        self.deadline=deadline
        self.limit=min(8.,max_distance)
        self.max_pulses=min(40,max_pulses)
        self.blocked_positions=blocked_positions
        self.observe=observe
        self.turn=NativeTurn(now,nav,self.reverse,convention,turnaround=True)
        self.walk=self.request=self.exposure=None
        self.total=self.pulses=self.views=0
        self.tried=set()
        self.returning=False
        self.lanes={}

    @property
    def pulse(self):
        return self.walk.pulse if self.walk else None

    def close(self):
        if self.request:self.request.close();self.request=None

    def look_back(self,now,nav):
        delta=[(a-b)*scale for a,b,scale in zip(self.origin,nav.position,nav.world_size)]
        desired=(compass_from_delta(*delta,self.convention) if math.hypot(*delta)>.3 else self.original_heading)
        self.turn=NativeTurn(now,nav,desired,self.convention,turnaround=True)
        self.returning=True
        return TravelCommand()

    def alternate(self,now,nav):
        sides=sorted(('left','right'),key=lambda side:self.lanes.get(side)!='open')
        for side in sides:
            if side in self.tried or self.lanes.get(side)=='blocked':continue
            self.tried.add(side)
            sign=1 if (side=='left')==(self.convention=='ccw_west') else -1
            self.turn=NativeTurn(now,nav,self.reverse+sign*math.radians(35),self.convention,turnaround=True)
            return TravelCommand()
        return self.look_back(now,nav)

    def step(self,ctx,now,nav,heading):
        if now>=self.deadline:return TravelCommand(outcome='building_relocation_budget')
        if getattr(nav.camera,'paused',False) or nav.flags & UNSAFE:
            return TravelCommand(outcome='probe_interrupted')
        if self.turn:
            command=self.turn.step(now,nav,heading)
            if command is not None:return command
            self.turn=None
            if self.returning:
                return TravelCommand(outcome='building_relocated' if self.total>=.5 else 'building_relocation_blocked')
        if self.walk:
            old,pulses=self.walk.total,self.walk.pulses
            command=self.walk.step(ctx,now,nav,heading)
            self.total+=self.walk.total-old;self.pulses+=self.walk.pulses-pulses
            if not command.outcome:return command
            self.walk=None
            if command.outcome=='building_relocation_blocked':return self.alternate(now,nav)
            if command.outcome not in {'building_relocation_budget','building_relocated'}:return command
            if self.total>=self.limit-.15 or self.pulses>=self.max_pulses-1:
                return self.look_back(now,nav)
        if (self.views>=6 and not self.request) or self.deadline-now<13:
            self.close()
            return self.look_back(now,nav)
        if self.request is None:
            self.request=self.observe(ctx,nav)
            self.exposure=now,nav,heading
            self.views+=1
            ctx.log_event('quest_building_reverse_view',dict(frame_id=self.request.frame_id,
                position=nav.position,heading=heading,moved_yards=self.total))
            return TravelCommand()
        at,old,angle=self.exposure
        if (self.router.distance(old.position,nav.position)>.08 or abs(old.height-nav.height)>.1
                or old.camera!=nav.camera or old.camera_epoch!=nav.camera_epoch
                or old.visual_ui_hidden!=nav.visual_ui_hidden or abs(heading_error(angle,heading))>math.radians(2)):
            self.close()
            return TravelCommand(outcome='probe_pose_changed')
        if at+10<now:
            self.close()
            return self.look_back(now,nav)
        result=self.request.poll()
        if result is None:return TravelCommand()
        fid=self.request.frame_id
        self.close()
        self.lanes=result.get('lanes') or {}
        if (result.get('schema')!=1 or result.get('frame_id')!=fid or result.get('executable') is not False
                or any(self.lanes.get(side) not in {'open','unknown','blocked'} for side in ('left','center','right'))):
            return TravelCommand(outcome='building_observation_invalid')
        ctx.log_event('quest_building_reverse_clearance',dict(frame_id=fid,lanes=self.lanes,
            position=nav.position,moved_yards=self.total))
        if self.lanes['center']=='blocked':return self.alternate(now,nav)
        remaining=min(self.limit-self.total,4. if self.lanes['center']=='open' else 1.)
        if remaining<.5 or self.max_pulses-self.pulses<2:return self.look_back(now,nav)
        self.walk=ForwardProbe(now,nav,heading,self.router,self.convention,
            deadline=self.deadline,blocked_positions=self.blocked_positions,
            max_distance=remaining,max_pulses=self.max_pulses-self.pulses)
        return TravelCommand()


class TraceRelocation:
    basis = 'recent_ground_trace'
    def __init__(self, now, nav, trace, router, convention, *, deadline,
                 blocked_positions, max_distance=2., max_pulses=24):
        if trace is None or nav.height is None or not math.isfinite(nav.height):
            raise ValueError('building_recent_ground_trace_missing')
        self.router,self.convention=router,convention
        self.blocked_positions=blocked_positions
        self.path=[]
        origin=nav.position;length=0.
        self.limit=min(2.,max_distance)
        for point in trace.backtrack(nav,now,router):
            distance=router.distance(origin,point.position)
            if (not 0<=now-point.at<=30 or point.height is None or not math.isfinite(point.height)
                    or abs(point.height-nav.height)>.25 or distance>.75
                    or self.blocked(origin,point.position)):
                break
            remaining=self.limit-length
            if distance>remaining:
                # Interpolate only inside a contiguous measured segment, never
                # bridge missing frames or shortcut an observed corner.
                point=replace(point,position=tuple(a+(b-a)*remaining/distance
                              for a,b in zip(origin,point.position)))
                distance=remaining
            self.path.append(point);length+=distance;origin=point.position
            if length>=self.limit-.01:break
        if self.path:
            # Calibration wiggles can accumulate distance while ending at the
            # same viewpoint. Prefer the farthest measured observation point,
            # with the shortest history prefix when distances tie.
            best=max(range(len(self.path)),key=lambda i:(router.distance(nav.position,self.path[i].position),-i))
            self.path=self.path[:best+1]
            length=sum(router.distance(a,b.position) for a,b in zip(
                [nav.position,*[p.position for p in self.path[:-1]]],self.path))
        if (not self.path or router.distance(nav.position,self.path[-1].position)<.5
                or max_pulses<2 or deadline-now<2):
            raise ValueError('building_recent_ground_trace_missing')
        self.limit=length
        self.deadline=min(deadline,now+12)
        self.max_pulses=min(24,max_pulses)
        self.identity=(nav.map_id,nav.world_size,nav.quest_id,nav.phase,
                       nav.objective_index,nav.objective_target,nav.destination,nav.destination_map)
        self.origin=nav.position;self.height=nav.height
        self.last_position=nav.position
        self.sequence=nav.sequence;self.sequence_at=now
        self.pulse=None;self.pulses=0;self.total=0.
        self.feedback_sequences=set();self.speeds=[]
        self.last_turn=now-1;self.settle_until=now

    def blocked(self,origin,target):
        return relocation_blocked(self.router,self.blocked_positions,origin,target)

    def step(self,ctx,now,nav,heading):
        identity=(nav.map_id,nav.world_size,nav.quest_id,nav.phase,
                  nav.objective_index,nav.objective_target,nav.destination,nav.destination_map)
        if (identity!=self.identity or nav.position is None or nav.height is None
                or not math.isfinite(nav.height) or abs(nav.height-self.height)>.25
                or nav.flags & UNSAFE or not nav.flags & TravelFlags.KEYS_VERIFIED
                or getattr(nav.camera,'paused',False)
                or nav.recovery and nav.recovery.life!=LifeState.ALIVE):
            return TravelCommand(outcome='probe_interrupted')
        if heading is None or ctx.heading_age()>.55:
            return TravelCommand(outcome='probe_heading_stale')
        if nav.sequence!=self.sequence:self.sequence,self.sequence_at=nav.sequence,now
        if now-self.sequence_at>.75:return TravelCommand(outcome='probe_telemetry_stale')
        if now>=self.deadline:return TravelCommand(outcome='building_relocation_budget')
        if self.pulse:
            at,origin,sequence,target,expected,hold=self.pulse
            if abs(heading_error(heading,expected))>math.radians(13):
                return TravelCommand(outcome='probe_pose_changed')
            if nav.sequence!=sequence:self.feedback_sequences.add(nav.sequence)
            if now-at<.25 or len(self.feedback_sequences)<2:return TravelCommand()
            delta=[(b-a)*s for a,b,s in zip(origin,nav.position,nav.world_size)]
            vector=[(b-a)*s for a,b,s in zip(origin,target,nav.world_size)]
            length=math.hypot(*vector);moved=math.hypot(*delta)
            along=sum(a*b for a,b in zip(delta,vector))/length
            cross=abs(delta[0]*vector[1]-delta[1]*vector[0])/length
            if moved>.8 or cross>.12 or along<-.03 or along>length+.08:
                return TravelCommand(outcome='probe_pose_changed')
            self.pulse=None
            self.last_position=nav.position
            if moved<.03 or along<.02:
                self.blocked_positions.append(target)
                return TravelCommand(outcome='building_relocation_blocked')
            self.total+=moved;self.speeds.append(moved/hold*1.35)
            ctx.log_event('quest_building_relocation_feedback',dict(
                basis='recent_ground_trace',position=nav.position,from_position=origin,
                before_sequence=sequence,after_sequences=sorted(self.feedback_sequences),
                moved_yards=moved,total_yards=self.total,route_to_target_verified=False))
            if self.total>self.limit+.1:return TravelCommand(outcome='probe_pose_changed')
            return TravelCommand()
        if self.router.distance(self.last_position,nav.position)>.08:
            return TravelCommand(outcome='probe_pose_changed')
        while self.path and self.router.distance(nav.position,self.path[0].position)<=.06:
            self.path.pop(0)
        if not self.path or self.total>=self.limit-.06:
            return TravelCommand(outcome='building_relocated')
        if self.pulses>=self.max_pulses or self.deadline-now<1.2:
            return TravelCommand(outcome='building_relocation_budget')
        target=self.path[0]
        if not 0<=now-target.at<=30:return TravelCommand(outcome='building_relocation_expired')
        if self.blocked(nav.position,target.position):
            return TravelCommand(outcome='building_relocation_blocked')
        delta=[(b-a)*s for a,b,s in zip(nav.position,target.position,nav.world_size)]
        desired=(compass_from_delta(*delta,self.convention)+math.pi)%math.tau
        error=heading_error(heading,desired)
        if abs(error)>math.radians(7):
            if now-self.last_turn<.8:return TravelCommand()
            self.last_turn=now;self.settle_until=now+.5
            side='left' if (error>0)==(self.convention=='ccw_west') else 'right'
            return TravelCommand(tap=side,hold_s=min(.12,max(.025,abs(error)/math.radians(240))),precise=True)
        if now<self.settle_until or now-ctx.heading_age()<self.last_turn+.2:
            return TravelCommand()
        distance=self.router.distance(nav.position,target.position)
        speed=max([4.5,*self.speeds])
        hold=min(.12 if self.speeds else .07,distance/speed,(self.limit-self.total)/speed)
        if hold<.005:return TravelCommand(outcome='building_relocation_budget')
        self.pulses+=1;self.feedback_sequences=set()
        self.pulse=(now,nav.position,nav.sequence,target.position,heading,hold)
        ctx.log_event('quest_building_relocation_pulse',dict(
            basis='recent_ground_trace',pulse=self.pulses,position=nav.position,
            target=target.position,source_sequence=target.sequence,source_at=target.at,
            key='s',hold_s=hold,projection_calibrated=False))
        return TravelCommand(tap='s',hold_s=hold,precise=True)
