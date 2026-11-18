"""Ground-only runtime coordinator. Shadow never emits movement commands."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
import math
import time
from pathlib import Path
import numpy as np
from .planner import NavigationError, NavMesh, Query
from .follower import WorldPose, WorldRouteFollower
from ..control.quest_travel import TravelCommand


class SurfaceTracker:
    """Use unique support initially, then require short connected motion receipts.

    This is inferred Z, never a native altitude observation. Stacked surfaces
    cannot be initialized from XY alone. Mesh erosion is respected.
    """
    def __init__(self, mesh):
        self.mesh=mesh;self.triangles=mesh.debug_triangles();self.last=None
        self.support_basis='mesh_triangle'

    def candidates(self, xy, *, edge_tolerance=0):
        p=np.asarray(xy,float);t=self.triangles
        a=t[:,0,:2];u=t[:,1,:2]-a;v=t[:,2,:2]-a;w=p-a
        den=u[:,0]*v[:,1]-u[:,1]*v[:,0]
        valid=np.abs(den)>1e-10
        b=np.divide(w[:,0]*v[:,1]-w[:,1]*v[:,0],den,out=np.zeros_like(den),where=valid)
        c=np.divide(u[:,0]*w[:,1]-u[:,1]*w[:,0],den,out=np.zeros_like(den),where=valid)
        # Detour uses float32; independently rebased edge points differ by a
        # few ulps. A tiny barycentric tolerance preserves shared boundaries.
        valid &= (b>=-1e-5)&(c>=-1e-5)&(b+c<=1+1e-5)
        z=t[:,0,2]+b*(t[:,1,2]-t[:,0,2])+c*(t[:,2,2]-t[:,0,2])
        heights=list(z[valid])
        if edge_tolerance:
            # Project only within a small XY measurement/control tolerance;
            # callers still require a short connected route from last support.
            a=t[:,:,:2];edges=np.roll(t,-1,axis=1)-t
            den=np.sum(edges[:,:,:2]**2,axis=2)
            f=np.divide(np.sum((p-a)*edges[:,:,:2],axis=2),den,
                out=np.zeros_like(den),where=den>1e-12).clip(0,1)
            near=t+f[:,:,None]*edges
            distances=np.linalg.norm(near[:,:,:2]-p,axis=2)
            choice=distances.argmin(axis=1);rows=np.arange(len(t))
            projected=(~valid)&(distances[rows,choice]<=edge_tolerance)
            heights.extend(near[rows,choice,2][projected])
        values=sorted(heights)
        heights=[]
        for h in values:
            if not heights or abs(h-heights[-1])>.15:heights.append(float(h))
        return [(*map(float,xy),h) for h in heights]

    def update(self, xy):
        self.support_basis='mesh_triangle'
        candidates=self.candidates(xy)
        if self.last is not None:
            distance=math.dist(xy,self.last[:2])
            if distance>2:raise NavigationError('nav3d_displacement_discontinuity')
            def connected(points):
                result=[]
                for p in points:
                    if abs(p[2]-self.last[2])>max(.45,distance*1.1):continue
                    try:route=self.mesh.plan(Query(self.mesh.frame,self.last,p,horizontal_snap=.08))
                    except NavigationError:continue
                    if route.length<=distance*1.5+.3:result.append(p)
                return result
            candidates=connected(candidates)
            # Exact support has priority. Projection must not introduce adjacent
            # slope heights as extra floors when the pose is already inside.
            if not candidates:
                candidates=connected(self.candidates(xy,edge_tolerance=.08))
                self.support_basis='bounded_edge_projection'
        else:
            # Include nearby support at startup so a tiny edge miss cannot
            # silently initialize to the roof above an unobserved ground floor.
            candidates=self.candidates(xy,edge_tolerance=.08)
        if len(candidates)!=1:
            raise NavigationError('nav3d_support_ambiguous' if candidates else 'nav3d_support_missing')
        self.last=candidates[0];return self.last


class Nav3DCoordinator:
    def __init__(self, config, *, now, log_event=lambda *_:None, resume_runs=None):
        if config.mode not in {'nav3d','nav3d-shadow'}:raise NavigationError('invalid_nav3d_mode')
        if not 0<config.budget_s<=120 or not 0<=config.max_replans<=3:raise NavigationError('invalid_nav3d_budget')
        self.config=config;self.mesh=NavMesh.load(config.mesh);self.tracker=SurfaceTracker(self.mesh)
        self.log=log_event;self.deadline=now+config.budget_s;self.follower=None;self.replans=0
        self.sequence=None;self.changed_at=now;self.identity=None;self.terminal=None;self.route=None
        self.goals=None;self.goal_index=0;self.arrived_at=None
        self.resume_runs=resume_runs
        trial_id=config.supervised_trial_mesh_id
        if trial_id and trial_id!=self.mesh.mesh_id:raise NavigationError('nav3d_trial_mesh_mismatch')
        self.supervised_trial=bool(trial_id) and config.mode=='nav3d'
        if self.supervised_trial:
            self.log('nav3d_supervised_trial',dict(mesh_id=self.mesh.mesh_id,
                alignment_verified=False,phase_verified=False,budget_s=config.budget_s))
        self.alignment=False
        if config.alignment_file:
            evidence_path=Path(config.alignment_file)
            evidence=json.loads(evidence_path.read_text())
            if not isinstance(evidence,dict):raise NavigationError('invalid_alignment_evidence')
            receipts=evidence.get('receipts',[])
            verified_receipts=isinstance(receipts,list) and bool(receipts)
            kinds=set()
            if verified_receipts:
                for receipt in receipts:
                    if not isinstance(receipt,dict) or not {'path','sha256','kind'}<=receipt.keys():
                        verified_receipts=False;break
                    resource=evidence_path.parent/receipt['path']
                    if not resource.is_file() or hashlib.sha256(resource.read_bytes()).hexdigest()!=receipt['sha256']:
                        verified_receipts=False;break
                    kinds.add(receipt['kind'])
            self.alignment=(evidence.get('mesh_id')==self.mesh.mesh_id and evidence.get('frame')==asdict(self.mesh.frame)
                and evidence.get('alignment_verified') is True and evidence.get('phase_verified') is True
                and evidence.get('evidence_kind')=='live_observation' and verified_receipts
                and {'native_xy_alignment','scene_phase'}<=kinds)

    def stop(self,reason):
        if self.terminal is None:self.log('nav3d_stop',{'reason':reason,'reached_in_game':False})
        self.terminal=reason;return TravelCommand(outcome=reason)

    def step(self,now,sample,navigation,*,goal_override=None,blocked_boxes=(),target_receipt=None,travel_only=False):
        if self.terminal:return TravelCommand(outcome=self.terminal)
        try:return self._step(now,sample,navigation,goal_override,blocked_boxes,target_receipt,travel_only)
        except (NavigationError,ValueError,OSError) as exc:return self.stop(str(exc))

    def next_candidate(self,reason):
        self.log('nav3d_candidate_finished',dict(index=self.goal_index,goal=self.goals[self.goal_index],
            reason=reason,reached_in_game=self.arrived_at is not None))
        self.goal_index+=1;self.follower=None;self.route=None;self.arrived_at=None

    def resume_support(self,sample):
        """Reuse the last continuous support only after a recent edge stop.

        Require the same mesh/frame/goal and unchanged stopped XY. This is
        inferred support continuity, not a new native altitude measurement.
        Other stop reasons, stale runs or movement since stopping cannot seed Z.
        """
        directory=self.resume_runs;self.resume_runs=None
        if not directory:return
        completed=sorted((p for p in Path(directory).glob('*/events.jsonl')
            if (p.parent/'summary.json').is_file()),reverse=True)
        if not completed:return
        path=completed[0]
        if path.stat().st_size>8_000_000:return
        try:
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            last={r['kind']:r for r in rows}
            end=last['module_end'];stop=last['nav3d_stop']
            if end['data']['status']!='stopped:nav3d_support_missing' or stop['data']['reason']!='nav3d_support_missing':return
            if not 0<=time.time()-end['ts']<=900:return
            route=last['nav3d_plan']['data']['route']
            if route['mesh_id']!=self.mesh.mesh_id or route['frame']!=asdict(self.mesh.frame):return
            if tuple(route['requested_goal'][:2])!=tuple(sample.goal_xy):return
            previous=last['nav3d_control']['data']['position']
            stopped=last['nav3d_pose']['data']['position_xy']
            if math.dist(stopped,sample.position_xy)>.1 or math.dist(previous[:2],stopped)>.5:return
            # Validate the recorded support itself before seeding continuity.
            if not any(abs(p[2]-previous[2])<=.15 for p in self.tracker.candidates(previous[:2],edge_tolerance=.08)):return
            self.tracker.last=tuple(previous)
            self.log('nav3d_resume_support',dict(source_run=path.parent.name,position=previous,
                stopped_xy=stopped,z_source='previous_inferred_support',native_altitude_verified=False))
        except (ValueError,KeyError,TypeError,OSError):return

    def _step(self,now,sample,nav,goal_override,blocked_boxes,target_receipt,travel_only):
        if now>self.deadline:return self.stop('nav3d_budget_exhausted')
        if sample is None or nav is None:return self.stop('nav3d_channel_unavailable')
        if sample.sequence!=nav.sequence or sample.quest_id!=nav.quest_id or sample.phase!=int(nav.phase) or sample.ui_map_id!=nav.map_id:
            return self.stop('nav3d_incoherent_channels')
        if self.sequence==sample.sequence:
            if now-self.changed_at>.5:return self.stop('nav3d_stale_pose')
            return TravelCommand()
        self.sequence=sample.sequence;self.changed_at=now
        frame=self.mesh.frame
        if (sample.world_id!=frame.world_id or str(sample.client_build)!=frame.client_build.rsplit('.',1)[-1]
                or frame.phase_id!=f'quest:{sample.quest_id}:{sample.phase}'):
            return self.stop('nav3d_world_frame_changed')
        if sample.position_xy is None or sample.heading is None:return self.stop('nav3d_position_unavailable')
        if not sample.grounded or not sample.bindings_verified or sample.in_combat:
            self.log('nav3d_unsafe_state', dict(grounded=sample.grounded,
                bindings_verified=sample.bindings_verified,in_combat=sample.in_combat,
                travel_flags=sample.travel_flags,position_xy=sample.position_xy,sequence=sample.sequence))
            return self.stop('nav3d_state_unsafe')
        if goal_override is not None and tuple(goal_override)!=nav.destination:
            return self.stop('nav3d_goal_override_requires_world_conversion')
        if sample.goal_xy is None or sample.goal_world_id!=sample.world_id:
            return self.stop('nav3d_goal_world_mismatch')
        identity=(sample.world_id,sample.ui_map_id,sample.quest_id,sample.phase,sample.goal_world_id,sample.goal_xy)
        if self.identity is not None and self.identity!=identity:return self.stop('nav3d_goal_changed')
        self.identity=identity
        self.log('nav3d_pose',dict(sequence=sample.sequence,position_xy=sample.position_xy,
            heading=sample.heading,public_z=sample.public_z,elapsed=self.config.budget_s-(self.deadline-now)))
        if self.tracker.last is None:self.resume_support(sample)
        try:position=self.tracker.update(sample.position_xy)
        except NavigationError as exc:
            self.log('nav3d_support_failure',dict(reason=str(exc),previous=self.tracker.last,
                position_xy=sample.position_xy,exact=self.tracker.candidates(sample.position_xy),
                nearby=self.tracker.candidates(sample.position_xy,edge_tolerance=.08)))
            raise
        if target_receipt and self.config.mode=='nav3d':
            self.log('nav3d_target_receipt',target_receipt)
            return self.stop('nav3d_target_confirmed')
        pose=WorldPose(frame,position,sample.heading,sample.sequence,now,sample.grounded,sample.bindings_verified,sample.in_combat)
        revision=hashlib.sha256(json.dumps(blocked_boxes).encode()).hexdigest()
        if self.goals is None:
            self.goals=sorted(self.tracker.candidates(sample.goal_xy),key=lambda p:p[2])
            self.log('nav3d_support',dict(position=position,goal_candidates=self.goals,
                z_source='inferred_mesh_support',public_z=sample.public_z))
            if not self.goals:return self.stop('nav3d_goal_support_missing')
        if self.arrived_at is not None:
            # Arrival at a floor is a checkpoint, not proof of the target floor.
            if now-self.arrived_at<2:return TravelCommand()
            self.next_candidate('target_receipt_missing')
        if self.route and revision!=self.route.obstacle_revision:
            self.follower=None;self.replans+=1
        if self.replans>self.config.max_replans:return self.stop('nav3d_replan_budget_exhausted')
        if self.follower is None:
            while self.goal_index<len(self.goals):
                goal=self.goals[self.goal_index]
                try:self.route=self.mesh.plan(Query(frame,position,goal,blocked_boxes=blocked_boxes))
                except NavigationError as exc:
                    self.next_candidate(str(exc));continue
                break
            else:return self.stop('nav3d_candidates_exhausted')
            self.log('nav3d_plan',dict(route=self.route.to_dict(),mode=self.config.mode,z_source='inferred_mesh_support',public_z=sample.public_z))
            self.follower=WorldRouteFollower(self.route,now=now,mesh_id=self.mesh.mesh_id,obstacle_revision=revision,
                alignment_verified=self.alignment,supervised_trial=self.supervised_trial,
                budget=max(.001,self.deadline-now))
        if self.config.mode=='nav3d-shadow':
            return TravelCommand(outcome='nav3d_shadow_complete')
        command=self.follower.step(now,pose,mesh_id=self.mesh.mesh_id,obstacle_revision=revision)
        self.log('nav3d_control',dict(sequence=sample.sequence,position=position,candidate=self.goal_index,
            support_basis=self.tracker.support_basis,waypoint=self.follower.index,
            tap=command.tap,hold_s=command.hold_s,outcome=command.outcome))
        if command.outcome in {'nav3d_stuck_replan_required','nav3d_replan_required'}:
            self.replans+=1
            self.next_candidate(command.outcome)
            return TravelCommand()
        if command.outcome=='nav3d_route_arrived' and not travel_only:
            self.arrived_at=now
            self.log('nav3d_candidate_arrived',dict(index=self.goal_index,goal=self.goals[self.goal_index],
                task_receipt_confirmed=False))
            return TravelCommand()
        if command.outcome:return self.stop(command.outcome)
        return command
