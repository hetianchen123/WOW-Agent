"""Ground-only runtime coordinator. Shadow never emits movement commands."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
import math
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

    def candidates(self, xy):
        p=np.asarray(xy,float);t=self.triangles
        a=t[:,0,:2];u=t[:,1,:2]-a;v=t[:,2,:2]-a;w=p-a
        den=u[:,0]*v[:,1]-u[:,1]*v[:,0]
        valid=np.abs(den)>1e-10
        b=np.divide(w[:,0]*v[:,1]-w[:,1]*v[:,0],den,out=np.zeros_like(den),where=valid)
        c=np.divide(u[:,0]*w[:,1]-u[:,1]*w[:,0],den,out=np.zeros_like(den),where=valid)
        valid &= (b>=-1e-6)&(c>=-1e-6)&(b+c<=1+1e-6)
        z=t[:,0,2]+b*(t[:,1,2]-t[:,0,2])+c*(t[:,2,2]-t[:,0,2])
        heights=[]
        for h in sorted(z[valid]):
            if not heights or abs(h-heights[-1])>.15:heights.append(float(h))
        return [(*map(float,xy),h) for h in heights]

    def update(self, xy):
        candidates=self.candidates(xy)
        if self.last is not None:
            distance=math.dist(xy,self.last[:2])
            if distance>2:raise NavigationError('nav3d_displacement_discontinuity')
            connected=[]
            for p in candidates:
                if abs(p[2]-self.last[2])>max(.45,distance*1.1):continue
                try:route=self.mesh.plan(Query(self.mesh.frame,self.last,p))
                except NavigationError:continue
                if route.length<=distance*1.5+.3:connected.append(p)
            candidates=connected
        if len(candidates)!=1:
            raise NavigationError('nav3d_support_ambiguous' if candidates else 'nav3d_support_missing')
        self.last=candidates[0];return self.last


class Nav3DCoordinator:
    def __init__(self, config, *, now, log_event=lambda *_:None):
        if config.mode not in {'nav3d','nav3d-shadow'}:raise NavigationError('invalid_nav3d_mode')
        if not 0<config.budget_s<=120 or not 0<=config.max_replans<=3:raise NavigationError('invalid_nav3d_budget')
        self.config=config;self.mesh=NavMesh.load(config.mesh);self.tracker=SurfaceTracker(self.mesh)
        self.log=log_event;self.deadline=now+config.budget_s;self.follower=None;self.replans=0
        self.sequence=None;self.changed_at=now;self.identity=None;self.terminal=None;self.route=None
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

    def step(self,now,sample,navigation,*,goal_override=None,blocked_boxes=()):
        if self.terminal:return TravelCommand(outcome=self.terminal)
        try:return self._step(now,sample,navigation,goal_override,blocked_boxes)
        except (NavigationError,ValueError,OSError) as exc:return self.stop(str(exc))

    def _step(self,now,sample,nav,goal_override,blocked_boxes):
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
        if not sample.grounded or not sample.bindings_verified or sample.in_combat:return self.stop('nav3d_state_unsafe')
        if goal_override is not None and tuple(goal_override)!=nav.destination:
            return self.stop('nav3d_goal_override_requires_world_conversion')
        if sample.goal_xy is None or sample.goal_world_id!=sample.world_id:
            return self.stop('nav3d_goal_world_mismatch')
        identity=(sample.world_id,sample.ui_map_id,sample.quest_id,sample.phase,sample.goal_world_id,sample.goal_xy)
        if self.identity is not None and self.identity!=identity:return self.stop('nav3d_goal_changed')
        self.identity=identity
        position=self.tracker.update(sample.position_xy)
        pose=WorldPose(frame,position,sample.heading,sample.sequence,now,sample.grounded,sample.bindings_verified,sample.in_combat)
        revision=hashlib.sha256(json.dumps(blocked_boxes).encode()).hexdigest()
        if self.route and revision!=self.route.obstacle_revision:self.follower=None
        if self.follower is None:
            if self.replans>self.config.max_replans:return self.stop('nav3d_replan_budget_exhausted')
            goals=self.tracker.candidates(sample.goal_xy)
            paths=[]
            for goal in goals:
                try:paths.append(self.mesh.plan(Query(frame,position,goal,blocked_boxes=blocked_boxes)))
                except NavigationError:pass
            self.log('nav3d_support', dict(position=position, goal_candidates=goals, complete_paths=len(paths),
                z_source='inferred_mesh_support', public_z=sample.public_z))
            # Reachability alone doesn't establish which floor the task needs.
            if len(goals)!=1:return self.stop('nav3d_goal_support_ambiguous' if goals else 'nav3d_goal_support_missing')
            if not paths:return self.stop('nav3d_no_complete_path')
            self.route=paths[0];self.replans+=1
            self.log('nav3d_plan',dict(route=self.route.to_dict(),mode=self.config.mode,z_source='inferred_mesh_support',public_z=sample.public_z))
            self.follower=WorldRouteFollower(self.route,now=now,mesh_id=self.mesh.mesh_id,obstacle_revision=revision,
                alignment_verified=self.alignment,budget=max(.001,self.deadline-now))
        if self.config.mode=='nav3d-shadow':
            return TravelCommand(outcome='nav3d_shadow_complete')
        command=self.follower.step(now,pose,mesh_id=self.mesh.mesh_id,obstacle_revision=revision)
        if command.outcome in {'nav3d_stuck_replan_required','nav3d_replan_required'}:
            self.follower=None;return TravelCommand()
        if command.outcome:return self.stop(command.outcome)
        return command
