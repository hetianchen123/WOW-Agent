"""One model plan -> multiple metric waypoints -> local movement receipts.

Only route failures/new viewpoints trigger another API call; pulses do not.
"""
from __future__ import annotations
import math
from uuid import uuid4

from ..decision.route_planner import RouteRequest, PlannerError, credentials
from ..decision.grounded_route import npc_identity
from ..perception.scene_scale import anchor_profile, overhead_profile
from ..perception.travel_channel import TaskFlags
from .ground_trace import blocked_segment
from .navigation import compass_from_delta, heading_error
from .quest_travel import TravelCommand
from .overhead_camera import OverheadObservation


def world_waypoints(plan, origin, destination, router):
    x,y=router.metric(origin); tx,ty=router.metric(destination)
    distance=math.hypot(tx-x,ty-y)
    if not .5<=distance<=20:
        raise PlannerError('route_goal_scale_unavailable')
    fx,fy=(tx-x)/distance,(ty-y)/distance
    # x east+, y south+: clockwise/right vector is (-fy, fx).
    goals=[]
    for wp in plan['waypoints']:
        r,f=wp['right_yards'],wp['forward_yards']
        p=((x+fx*f-fy*r)/router.width,(y+fy*f+fx*r)/router.height)
        if not all(0<=v<=1 for v in p):
            raise PlannerError('route_waypoint_outside_map')
        goals.append(p)
    if not goals or router.distance(goals[-1],destination)>4:
        raise PlannerError('route_endpoint_far_from_goal')
    return goals


class ModelRouteFollower:
    """Follows model-supplied world points; never invents alternate routes."""
    def __init__(self, now, nav, goals, router, convention):
        self.goals=list(goals)
        self.router,self.convention=router,convention
        self.origin=self.last_position=nav.position
        self.height=nav.height
        self.pulse=None
        self.last_turn,self.settle_until=now-1,now
        self.turn_sequence=None
        self.trace=[nav.position]
        self.feedback=[]
        self.total=0
        self.pulses=0

    def step(self, ctx, now, nav, heading):
        if self.total>=40 or self.pulses>=80:
            return TravelCommand(outcome='route_execution_budget')
        if nav.height is not None and self.height is not None and abs(nav.height-self.height)>1.5:
            return TravelCommand(outcome='probe_height_drop')
        if self.pulse:
            at,origin,sequence,expected,target,duration=self.pulse
            if abs(heading_error(heading,expected))>math.radians(13):
                return TravelCommand(outcome='probe_pose_changed')
            if now-at<duration+.5:
                return TravelCommand()
            if nav.sequence==sequence:
                return TravelCommand(outcome='probe_telemetry_stale') if now-at>=1.6 else TravelCommand()
            moved=self.router.distance(origin,nav.position)
            if moved>3:
                return TravelCommand(outcome='probe_pose_changed')
            progress=self.router.distance(origin,target)-self.router.distance(nav.position,target)
            self.pulse=None
            self.last_position=nav.position
            self.total+=moved
            record={'from':origin,'position':nav.position,'target':target,'yards':round(moved,3),
                    'progress_yards':round(progress,3),'sequence':nav.sequence}
            self.feedback.append(record)
            ctx.log_event('quest_model_movement_feedback',record)
            if moved<.15 or progress<.05:
                self.router.block_ahead(nav.position,target)
                return TravelCommand(outcome='route_collision' if moved<.15 else 'route_no_progress')
            x,y=self.router.metric(origin); tx,ty=self.router.metric(nav.position)
            if abs(heading_error(expected,compass_from_delta(tx-x,ty-y,self.convention)))>math.radians(35):
                return TravelCommand(outcome='route_deviation')
            self.trace.append(nav.position)
            self.router.remember_passage([origin,nav.position])
            return TravelCommand()
        if self.router.distance(self.last_position,nav.position)>.2:
            return TravelCommand(outcome='probe_pose_changed')
        while self.goals and self.router.distance(nav.position,self.goals[0])<=.55:
            reached=self.goals.pop(0)
            ctx.log_event('quest_model_waypoint_reached',{'waypoint':reached,'remaining':len(self.goals)})
        if not self.goals:
            return TravelCommand(outcome='route_exhausted_without_range')
        target=self.goals[0]
        distance=self.router.distance(nav.position,target)
        endpoint=tuple(a+(b-a)*min(1,1/max(distance,.01)) for a,b in zip(nav.position,target))
        if blocked_segment(self.router,nav.position,endpoint):
            return TravelCommand(outcome='route_known_collision')
        x,y=self.router.metric(nav.position); tx,ty=self.router.metric(target)
        error=heading_error(heading,compass_from_delta(tx-x,ty-y,self.convention))
        if abs(error)>math.radians(8):
            if now-self.last_turn<.5 or nav.sequence==self.turn_sequence:
                return TravelCommand()
            self.last_turn,self.settle_until,self.turn_sequence=now,now+.45,nav.sequence
            left=(error>0)==(self.convention=='ccw_west')
            return TravelCommand(tap='left' if left else 'right',hold_s=min(.16,max(.025,abs(error)/math.radians(240))))
        if now<self.settle_until or now-ctx.heading_age()<self.last_turn+.2:
            return TravelCommand()
        duration=.12 if distance<1.3 else .25
        self.pulses+=1
        self.pulse=now,nav.position,nav.sequence,heading,target,duration
        ctx.log_event('quest_model_movement_issued',{'position':nav.position,'target':target,
            'key':'w','hold_s':duration,'waypoints_remaining':len(self.goals),'api_call':False})
        return TravelCommand(tap='w',hold_s=duration)


class ModelDetour:
    def __init__(self, now, nav, heading, router, convention, config, capture, *, deadline, request_factory=RouteRequest):
        self.router,self.convention,self.config=router,convention,config
        self.capture,self.request_factory=capture,request_factory
        self.deadline=deadline
        self.anchor,self.heading=nav,heading
        self.request=self.pose=self.follower=self.turn=None
        self.request_at=None
        self.ready_at=now+.5
        self.calls=self.errors=self.observations=0
        self.history=[]
        self.last_plan=None
        self.closed=False
        self.overhead=OverheadObservation() if config.overhead_observation else None

    def close(self):
        self.closed=True
        if self.request:
            self.request.close()
            self.request=None

    def facts(self, nav, heading, now):
        x,y=self.router.metric(nav.position); tx,ty=self.router.metric(nav.destination)
        relative=math.degrees(heading_error(heading,compass_from_delta(tx-x,ty-y,self.convention)))
        return {'frame_id':uuid4().hex,'quest_id':nav.quest_id,'map_id':nav.map_id,
                'phase':nav.phase.name,'position':nav.position,'destination':nav.destination,
                'goal_identity':npc_identity(nav.quest_id,nav.map_id,nav.phase.name),
                'camera_profile':overhead_profile(nav.camera) if self.overhead else anchor_profile(nav.camera),
                'view_mode':'overhead' if self.overhead else 'low',
                'world_size':nav.world_size,'height':nav.height,'heading_radians':heading,
                'facing_convention':self.convention,'goal_relative_degrees':round(relative,1),
                'goal_distance_yards':round(math.hypot(tx-x,ty-y),3),
                'goal_description':'Currently selected quest NPC, disambiguated by world goal bearing; unknown identity remains unknown.',
                'sequence':nav.sequence,'camera_epoch':nav.camera_epoch,
                'remaining_s':round(self.deadline-now,1),'failed_routes':self.history[-3:],
                'known_collision_cells':{'cell_size_yards':self.router.cell_size,
                    'map_fraction_centers': [((x+.5)*self.router.cell_size/self.router.width,
                                              (y+.5)*self.router.cell_size/self.router.height)
                        for x,y in sorted(self.router.blocked)[:24]]},
                'budget_requests_remaining':self.config.max_requests-self.calls,
                'coordinate_frame':'origin player; forward toward world goal; right clockwise; yards'}

    def step(self, ctx, now, nav, heading):
        if self.closed:
            return TravelCommand(outcome='escape_model_closed')
        if now>=self.deadline:
            self.close()
            return TravelCommand(outcome='escape_model_budget_exhausted')
        if not nav.task & TaskFlags.NPC_SELECTED:
            return TravelCommand(outcome='probe_target_changed')
        if nav.task & TaskFlags.NPC_RANGE_KNOWN and nav.task & TaskFlags.NPC_WITHIN_5_YARDS:
            self.close()
            ctx.log_event('quest_model_range_confirmed',{'position':nav.position,'calls':self.calls})
            return TravelCommand(outcome='detoured')
        if nav.height is not None and self.anchor.height is not None and abs(nav.height-self.anchor.height)>1.5:
            return TravelCommand(outcome='probe_height_drop')
        if self.overhead and not self.follower and not self.request:
            if self.router.distance(self.anchor.position,nav.position)>.15 or abs(heading_error(self.heading,heading))>math.radians(8):
                return TravelCommand(outcome='probe_pose_changed')
            command=self.overhead.step(ctx,now,nav)
            if command is not None:
                return command
        if self.follower:
            command=self.follower.step(ctx,now,nav,heading)
            if not command.outcome or command.outcome.startswith('probe_'):
                return command
            target=self.follower.goals[0] if self.follower.goals else None
            self.history.append({'reason':command.outcome,'position':nav.position,'blocked_target':target,
                'actual_trace':self.follower.trace[-12:],'last_feedback':self.follower.feedback[-3:],
                'previous_intent':self.last_plan.get('intent'),'waypoints':self.last_plan['waypoints']})
            ctx.log_event('quest_model_replan_needed',self.history[-1])
            self.follower=None
            self.anchor,self.heading,self.ready_at=nav,heading,now+.5
            return TravelCommand()
        if self.turn:
            t=self.turn
            if self.router.distance(t['nav'].position,nav.position)>.15 or now-t['at']>6:
                return TravelCommand(outcome='probe_turn_unconfirmed')
            error=heading_error(heading,t['target'])
            if abs(error)<=math.radians(4):
                self.turn=None
                self.anchor,self.heading,self.ready_at=nav,heading,now+.5
                return TravelCommand()
            if now<t['next_at'] or nav.sequence==t['sequence']:
                return TravelCommand()
            duration=max(.025,min(.12,abs(error)/math.radians(240)))
            t['next_at'],t['sequence']=now+duration+.3,nav.sequence
            left=(error>0)==(self.convention=='ccw_west')
            return TravelCommand(tap='left' if left else 'right',hold_s=duration)
        if self.router.distance(self.anchor.position,nav.position)>.15 or abs(heading_error(self.heading,heading))>math.radians(8):
            return TravelCommand(outcome='probe_pose_changed')
        if self.request:
            if nav.camera!=self.pose.camera or nav.camera_epoch!=self.pose.camera_epoch:
                self.close()
                return TravelCommand(outcome='probe_pose_changed')
            if now-self.request_at>min(20,self.config.request_timeout_s):
                self.close()
                return TravelCommand(outcome='escape_model_timeout')
            if not self.request.future.done():
                return TravelCommand()
            request,self.request=self.request,None
            try:
                plan=request.future.result()
                if self.overhead and plan['status']=='route':
                    raise PlannerError('route_overhead_image_valid_projection_unverified')
                goals=world_waypoints(plan,nav.position,nav.destination,self.router) if plan['status']=='route' else []
                for failure in self.history:
                    target=failure.get('blocked_target')
                    if target and failure['reason'] in {'route_collision','route_known_collision','route_no_progress'}:
                        # A new candidate must not immediately repeat a failed near-identical first leg.
                        if goals and self.router.distance(failure['position'],nav.position)<2 and self.router.distance(target,goals[0])<1:
                            raise PlannerError('route_repeats_failed_segment')
            except PlannerError as exc:
                self.errors+=1
                self.history.append({'reason':str(exc),'position':nav.position,
                                     'rejected_plan':getattr(request,'rejected_plan',None)})
                ctx.log_event('quest_model_plan_rejected',{'reason':str(exc),'evidence':str(request.evidence_dir)})
                if self.overhead or str(exc).startswith('deepseek_') or self.errors>=2:
                    return TravelCommand(outcome='escape_'+str(exc))
                self.ready_at=now+.25
                return TravelCommand()
            finally:
                request.close()
            self.last_plan=plan
            ctx.log_event('quest_model_plan_received',{'frame_id':plan['frame_id'],'plan':plan,
                'world_waypoints':goals,'evidence':str(request.evidence_dir),'route_verified':False})
            if plan['status']=='stop':
                return TravelCommand(outcome='escape_model_no_grounded_route')
            if plan['status']=='observe':
                self.observations+=1
                if self.observations>1:
                    return TravelCommand(outcome='escape_model_observation_budget')
                view=plan['observation']
                sign=(1 if view['direction']=='left' else -1)*(1 if self.convention=='ccw_west' else -1)
                self.turn={'at':now,'target':heading+sign*math.radians(view['angle_deg']),
                           'nav':nav,'sequence':None,'next_at':now}
                return TravelCommand()
            self.follower=ModelRouteFollower(now,nav,goals,self.router,self.convention)
            return TravelCommand()
        if now<self.ready_at:
            return TravelCommand()
        if self.calls>=min(3,max(1,self.config.max_requests)):
            return TravelCommand(outcome='escape_model_request_budget')
        if self.deadline-now<min(20,self.config.request_timeout_s)+2:
            return TravelCommand(outcome='escape_model_budget_exhausted')
        if not credentials(self.config):
            return TravelCommand(outcome='escape_deepseek_credentials_missing')
        profile=overhead_profile(nav.camera) if self.overhead else anchor_profile(nav.camera)
        if self.config.staged_grounding and profile is None:
            return TravelCommand(outcome='escape_route_camera_profile_unconfirmed')
        facts=self.facts(nav,heading,now)
        self.request=self.request_factory(self.capture(ctx.capture),facts,self.config)
        self.pose,self.request_at=nav,now
        self.calls+=1
        ctx.log_event('quest_model_plan_requested',{'frame_id':facts['frame_id'],'model':self.config.model,
            'call':self.calls,'evidence':str(self.request.evidence_dir),'remaining_s':round(self.deadline-now,2)})
        return TravelCommand()
