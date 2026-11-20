"""Ground-only runtime coordinator. Shadow never emits movement commands."""
from __future__ import annotations
from copy import copy
from dataclasses import asdict, replace
import hashlib
import json
import math
import time
from pathlib import Path
import numpy as np
from .planner import NavigationError, NavMesh, Query
from .follower import WorldPose, WorldRouteFollower
from ..control.navigation import route_advances_toward
from ..control.quest_travel import TravelCommand
from ..perception.nav3d_channel import with_fast_pose


class SurfaceTracker:
    """Use unique support initially, then require short connected motion receipts.

    This is inferred Z, never a native altitude observation. Stacked surfaces
    cannot be initialized from XY alone. Mesh erosion is respected.
    """
    def __init__(self, mesh, *, edge_tolerance=.08):
        self.mesh=mesh;self.triangles=mesh.debug_triangles();self.last=None
        self.support_basis='mesh_triangle'
        self.edge_tolerance=edge_tolerance
        self.edge_projection = None

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

    def update(self, xy, *, max_distance=2):
        self.support_basis='mesh_triangle'
        self.edge_projection = None
        candidates=self.candidates(xy)
        if self.last is not None:
            distance=math.dist(xy,self.last[:2])
            if distance>max_distance:raise NavigationError('nav3d_displacement_discontinuity')
            # A stair transition can rise more than the horizontal displacement.
            # Keep the mesh's agent clearance and the separate short-path check.
            step_limit=self.mesh.agent.max_climb+self.mesh.agent.cell_height/2
            def connected(points, *, exact=False):
                result=[]
                for p in points:
                    change = p[2] - self.last[2]
                    limit = max(.45, step_limit, distance * 1.1)
                    short_descent = exact and limit < -change <= step_limit + 2 * self.mesh.agent.cell_height
                    if abs(change) > limit and not short_descent:
                        continue
                    try:route=self.mesh.plan(Query(self.mesh.frame,self.last,p,horizontal_snap=self.edge_tolerance))
                    except NavigationError:continue
                    path_limit = distance * 1.5 + .3
                    if short_descent:
                        # Tiny, steep ground transitions can have a longer
                        # 3D arc without doubling back or changing floors.
                        horizontal = sum(math.dist(a[:2], b[:2]) for a, b in zip(route.points, route.points[1:]))
                        if horizontal <= path_limit and route.length <= math.dist(self.last, p) * 1.5 + .3:
                            result.append((p, route))
                    elif route.length <= path_limit:
                        result.append((p, route))
                return result
            supports = connected(candidates, exact=True)
            # Exact support has priority. Projection must not introduce adjacent
            # slope heights as extra floors when the pose is already inside.
            if not supports:
                supports = connected(self.candidates(xy, edge_tolerance=self.edge_tolerance))
                self.support_basis='bounded_edge_projection'
                if len(supports) > 1 and supports[-1][0][2] - supports[0][0][2] <= .32:
                    # Two adjacent edge projections can describe one sloping
                    # floor. Each still has to connect to the previous pose.
                    supports = [min(supports, key=lambda support: abs(support[0][2] - self.last[2]))]
                if len(supports) == 1:
                    observed, edge_route = supports[0]
                    projected = edge_route.points[-1]
                    # Use the endpoint of the checked short connection for
                    # Detour, while preserving the observed pose for control.
                    if abs(projected[2] - observed[2]) <= .15:
                        self.edge_projection = projected
            candidates = [observed for observed, _ in supports]
        else:
            # Include nearby support at startup so a tiny edge miss cannot
            # silently initialize to the roof above an unobserved ground floor.
            candidates=self.candidates(xy,edge_tolerance=self.edge_tolerance)
        if len(candidates)!=1:
            raise NavigationError('nav3d_support_ambiguous' if candidates else 'nav3d_support_missing')
        self.last=candidates[0];return self.last


class Nav3DCoordinator:
    def __init__(self, config, *, now, log_event=lambda *_:None, resume_runs=None, scene_phases=(),
                 edge_tolerance=.08, short_steps=False, arrival_radius=None, clearance_route_allowed=None, mesh=None,
                 corridor_control=False, forward_goal=None):
        if config.mode not in {'nav3d','nav3d-shadow'}:raise NavigationError('invalid_nav3d_mode')
        if not 0<config.budget_s<=120 or not 0<=config.max_replans<=3:raise NavigationError('invalid_nav3d_budget')
        self.config=config;self.mesh=mesh if mesh is not None else NavMesh.load(config.mesh)
        self.tracker=SurfaceTracker(self.mesh,edge_tolerance=edge_tolerance)
        self.clearance_route_allowed = clearance_route_allowed
        self.clearance_tracker = None
        self.clearance_checked = False
        self.short_steps=short_steps
        self.corridor_control = corridor_control
        self.forward_goal = forward_goal
        self.arrival_radius = arrival_radius
        self.log=log_event;self.deadline=now+config.budget_s;self.follower=None;self.replans=0
        self.sequence=None;self.changed_at=now;self.identity=None;self.terminal=None;self.route=None
        self.corridor = None
        self.fast_mode = None
        self.base_sequence, self.base_changed_at = None, now
        self.goals=None;self.goal_index=0;self.arrived_at=None
        self.exit_goal=None;self.exit_candidates=();self.exit_phase_released=False
        self.exit_hypotheses=None;self.exit_hypothesis_index=0
        self.resume_runs=resume_runs
        self.scene_phases=frozenset(scene_phases)
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

    def wider_support(self):
        if self.clearance_checked:
            return self.clearance_tracker
        self.clearance_checked = True
        folder = Path(self.config.mesh) / "clearance"
        if not folder.is_dir():
            return None
        try:
            wider = NavMesh.load(folder)
            if (wider.frame != self.mesh.frame or wider.geometry_hash != self.mesh.geometry_hash
                    or wider.sources != self.mesh.sources or wider.origin != self.mesh.origin
                    or wider.tile_cells != self.mesh.tile_cells or wider.agent != self.mesh.clearance_agent):
                raise NavigationError("clearance_mesh_does_not_match_base")
            self.clearance_tracker = SurfaceTracker(wider)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.log("nav3d_clearance_mesh_unavailable", {"reason": str(exc)})
        return self.clearance_tracker

    def route_with_clearance(self, route, corridor, spacing):
        if not self.fast_mode or self.short_steps or not 6 <= route.length <= 35:
            return route
        legs = np.diff(np.asarray(route.points)[:, :2], axis=0)
        scale = np.linalg.norm(legs[:-1], axis=1) * np.linalg.norm(legs[1:], axis=1)
        if not np.any((scale > 1e-6) & (np.sum(legs[:-1] * legs[1:], axis=1) < scale * math.sqrt(.5))):
            return route
        wider = self.wider_support()
        if wider is None:
            return route

        def find_connector(indices):
            previous = None
            distance = 0.
            for i in indices:
                point = route.points[i]
                if previous is not None:
                    distance += math.dist(point, previous)
                if distance > 4:
                    break
                candidates = wider.candidates(point[:2])
                if len(candidates) == 1 and abs(candidates[0][2] - point[2]) <= .18:
                    return i, candidates[0]
                previous = point
            raise NavigationError("clearance_connector_missing")

        try:
            first, origin = find_connector(range(len(route.points)))
            last, goal = find_connector(range(len(route.points) - 1, -1, -1))
            if first >= last or math.dist(origin, goal) < 3:
                raise NavigationError("clearance_interior_too_short")
            middle = wider.mesh.plan(Query(wider.mesh.frame, origin, goal, horizontal_snap=.04,
                                           vertical_snap=.22, spacing=spacing))
            # Keep the original approach, exit and target. Verify the proposed
            # interior independently on the base mesh and on the same floor.
            tracker = copy(self.tracker)
            points = list(route.points[:first + 1])
            tracker.last = points[-1]
            interior = (*middle.points[1:-1], route.points[last])
            bounds = np.asarray(interior)[:, :2]
            horizontal = tracker.triangles[:, :, :2]
            # Retain every triangle that could support a checked point,
            # including the tolerance used by the base tracker at edges.
            pad = tracker.edge_tolerance + .002
            low, high = bounds.min(axis=0) - pad, bounds.max(axis=0) + pad
            intersects = np.all((horizontal.max(axis=1) >= low) & (horizontal.min(axis=1) <= high), axis=1)
            tracker.triangles = tracker.triangles[intersects]
            # Eroding farther into a slope can change the detail triangulation.
            # Bound the reprojected height shift to one climb and still verify
            # every exact base surface and its Detour connection below.
            max_projection_delta = max(.2, min(.4, self.mesh.agent.max_climb))
            max_projected = 0.
            for point in interior:
                supported = tracker.update(point[:2])
                projected = abs(supported[2] - point[2])
                if tracker.support_basis != "mesh_triangle" or projected > max_projection_delta:
                    raise NavigationError("clearance_base_support_mismatch")
                max_projected = max(max_projected, projected)
                points.append(supported)
            points.extend(route.points[last + 1:])
            gaps = [math.dist(a, b) for a, b in zip(points, points[1:])]
            length = sum(gaps)
            if max(gaps) > .6 or length > min(35., route.length * 1.15):
                raise NavigationError("clearance_path_spacing_or_length_exceeded")
            candidate = replace(route, points=tuple(points), length=length)
            if self.clearance_route_allowed and not self.clearance_route_allowed(candidate):
                raise NavigationError("clearance_path_outside_trial_bounds")
            probe = copy(corridor)
            probe.refs = type(corridor.refs).from_buffer_copy(corridor.refs)
            for point in candidate.points:
                probe.advance(point, edge_tolerance=self.tracker.edge_tolerance)
        except NavigationError as exc:
            self.log("nav3d_clearance_route_rejected", {"reason": str(exc)})
            return route
        self.log("nav3d_clearance_route_selected", {"mesh_id": wider.mesh.mesh_id, "length_yards": length,
            "base_length_yards": route.length, "max_projection_delta_yards": max_projected,
            "prefix_waypoints": first, "suffix_waypoints": len(route.points) - last - 1})
        return candidate

    def stop(self,reason):
        if self.terminal is None:self.log('nav3d_stop',{'reason':reason,'reached_in_game':False})
        self.terminal=reason;return TravelCommand(outcome=reason)

    def begin_exit(self,goal_xy,*,now,sample=None,handoff=None):
        """Start a fresh bounded segment to a mesh exit toward goal_xy.

        The 2026-11-18 split policy keeps Nav3D for mesh-covered scenes and
        hands open-field travel back to the legacy stack. An explicit outdoor
        handoff can be inside mesh coverage. Static geometry spans quest phases,
        so a phase label change no longer stops this segment. Arrival emits
        nav3d_exit_reached instead of waiting for a task receipt.
        """
        from .exit import mesh_exit_candidates
        goal=tuple(map(float,goal_xy))
        if len(goal)!=2 or not all(math.isfinite(v) for v in goal):
            raise NavigationError('nav3d_exit_goal_invalid')
        if handoff is None:
            if self.tracker.candidates(goal):
                raise NavigationError('nav3d_exit_goal_on_mesh')
            candidates=mesh_exit_candidates(self.mesh,goal)
        else:
            point=tuple(map(float,handoff))
            if len(point)!=2 or not all(math.isfinite(v) for v in point):
                raise NavigationError('nav3d_exit_handoff_invalid')
            candidates=self.tracker.candidates(point)
            if len(candidates)!=1:
                raise NavigationError('nav3d_exit_handoff_support_unconfirmed')
        if not candidates:raise NavigationError('nav3d_exit_point_unavailable')
        self.exit_goal=goal;self.exit_candidates=tuple(candidates)
        self.terminal=None
        self.deadline=now+self.config.budget_s;self.follower=None;self.route=None
        self.goals=None;self.goal_index=0;self.arrived_at=None;self.replans=0
        self.exit_phase_released=False
        self.exit_hypotheses=None;self.exit_hypothesis_index=0
        seeded=self.seed_exit_support(sample)
        self.log('nav3d_exit_begin',dict(goal_xy=goal,candidates=list(self.exit_candidates),
            budget_s=self.config.budget_s,support_seeded=seeded))

    def seed_exit_support(self,sample, *, interrupted=False, stationary_arrival=None):
        """Seed exit-segment start support from this mesh's tracking records.

        A fresh run starting inside a stacked-XY building cannot initialize Z
        from XY alone; that rule stands for normal starts. The exit may reuse
        the mesh's own recent continuous support record at the same position,
        re-verified against current candidates, mirroring the support-missing
        resume path: inferred continuity, never a native altitude claim. A
        newer run's LAST recorded position more than 2 yards away aborts the
        chain; movement inside the seeding run itself is legitimate tracking.
        A stationary turn-in can also re-use an arrival with no subsequent
        observed movement since that arrival.
        """
        if self.tracker.last is not None or sample is None or sample.position_xy is None:
            return False
        directory=self.resume_runs
        if directory is None:return False
        runs=sorted((p for p in Path(directory).glob('*/events.jsonl')
            if interrupted or (p.parent/'summary.json').is_file()),reverse=True)[:24]
        age_limit = 900 if interrupted else 24 * 3600
        for path in runs:
            try:
                if path.stat().st_size>8_000_000:continue
                raw = path.read_text()
                lines = raw.splitlines()
                try:
                    rows=[json.loads(line) for line in lines]
                except ValueError:
                    if not interrupted or not lines or raw.endswith("\n"):
                        raise
                    rows=[json.loads(line) for line in lines[:-1]]
                if not rows:
                    continue
                if max(r.get('ts',0) for r in rows)<time.time()-age_limit:return False
                chain=self._last_position_xy(rows, interrupted=interrupted)
                if chain is not None and math.dist(chain,sample.position_xy)>(1.6 if interrupted else 2):
                    # The character was last recorded elsewhere after any older
                    # support; older runs cannot seed the current floor.
                    return False
                control=self._mesh_control(rows)
                if stationary_arrival is not None:
                    observations = [r['data']['navigation'] for r in rows
                                    if (control is None or r['event_id'] > control['event_id'])
                                    and r['kind'] == 'quest_observation' and r['data'].get('navigation')
                                    and r['data']['navigation'].get('position')]
                    if any(n['map_id'] != stationary_arrival.map_id or math.hypot(*(
                            (a - b) * size for a, b, size in zip(n['position'],
                                stationary_arrival.position, stationary_arrival.world_size))) > .25
                           for n in observations):
                        return False
                    if control is not None and (
                            control['data'].get('outcome') != 'nav3d_route_arrived' or not observations
                            or (getattr(stationary_arrival, 'addon_build', 0) or 0) >= 344
                            and (observations[0].get('addon_build') or 0) >= 344
                            or math.dist(control['data']['position'][:2], sample.position_xy) > .25):
                        return False
                if control is None:continue
                previous=control['data']['position']
                if interrupted:
                    if time.time() - control['ts'] > age_limit:
                        continue
                    self.tracker.last = tuple(previous)
                    try:
                        if chain is not None:
                            self.tracker.update(chain)
                        self.tracker.update(sample.position_xy)
                    except NavigationError:
                        self.tracker.last = None
                        continue
                    self.log('nav3d_arrival_support_seeded' if stationary_arrival else 'nav3d_exit_support_seeded',
                        dict(source_run=path.parent.name,
                        recorded_position=previous, seeded=self.tracker.last,
                        z_source='previous_inferred_support', native_altitude_verified=False))
                    return True
                if math.dist(previous[:2],sample.position_xy)>2:continue
                match=[p for p in self.tracker.candidates(sample.position_xy,edge_tolerance=self.tracker.edge_tolerance)
                    if abs(p[2]-previous[2])<=.35]
                if len(match)!=1:continue
                self.tracker.last=match[0]
                self.log('nav3d_exit_support_seeded',dict(source_run=path.parent.name,
                    recorded_position=previous,seeded=match[0],
                    z_source='previous_inferred_support',native_altitude_verified=False))
                return True
            except (ValueError,KeyError,TypeError,OSError):continue
        return False

    @staticmethod
    def _last_position_xy(rows, *, interrupted=False):
        """Chronologically last recorded position in a run (pose or encounter)."""
        for row in reversed(rows):
            if interrupted and row['kind'] in {'nav3d_unsafe_state', 'nav3d_support_failure'}:
                xy = row['data'].get('position_xy')
                if xy is not None:
                    return tuple(xy)
            if row['kind']=='nav3d_pose' and row['data'].get('position_xy') is not None:
                return tuple(row['data']['position_xy'])
            if row['kind']=='quest_encounter_stop':
                xy=(row['data'].get('last_state') or {}).get('position_xy')
                if xy is not None:return tuple(xy)
        return None

    def _mesh_control(self,rows):
        """Newest control row whose latest preceding plan matches this mesh."""
        plan_mesh=None;found=None
        for row in rows:
            if row['kind']=='nav3d_plan':
                plan_mesh=row['data']['route'].get('mesh_id')
            elif row['kind']=='nav3d_control' and plan_mesh==self.mesh.mesh_id:
                found=row
        return found

    def step(self,now,sample,navigation,*,goal_override=None,blocked_boxes=(),target_receipt=None,
             travel_only=False,retreating=False,fast_pose=None,task_transit=False):
        if self.terminal:return TravelCommand(outcome=self.terminal)
        try:return self._step(now,sample,navigation,goal_override,blocked_boxes,target_receipt,
                              travel_only,retreating,fast_pose,task_transit)
        except (NavigationError,ValueError,OSError) as exc:return self.stop(str(exc))

    def next_candidate(self,reason):
        self.log('nav3d_candidate_finished',dict(index=self.goal_index,goal=self.goals[self.goal_index],
            reason=reason,reached_in_game=self.arrived_at is not None))
        self.goal_index+=1;self.follower=None;self.route=None;self.arrived_at=None

    def _advance_floor_hypothesis(self,reason,xy):
        """Try the next stacked start floor for the exit segment (user 2026-11-18).

        Current-state-only startup: with no native indoor Z and no prior
        tracking, ambiguous floors are tried bottom-up with live receipts; a
        wrong floor fails through support loss, stuck or deviation and lands
        here. Bounded by the candidate count; logged as a mesh-candidate trial,
        never a native altitude claim.
        """
        if (self.exit_goal is None or not self.exit_hypotheses
                or self.exit_hypothesis_index>=len(self.exit_hypotheses)-1):
            return False
        self.exit_hypothesis_index+=1
        target_z=self.exit_hypotheses[self.exit_hypothesis_index]
        matches=[p for p in self.tracker.candidates(xy,edge_tolerance=self.tracker.edge_tolerance)
            if abs(p[2]-target_z)<1]
        if not matches:
            return False
        self.tracker.last=matches[0]
        self.goals=list(self.exit_candidates);self.goal_index=0
        self.follower=None;self.route=None;self.arrived_at=None
        self.log('nav3d_exit_floor_hypothesis',dict(index=self.exit_hypothesis_index,
            support=matches[0],reason=reason,levels=self.exit_hypotheses,
            z_source='mesh_candidate_trial',native_altitude_verified=False))
        return True

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
            if not any(abs(p[2]-previous[2])<=.15 for p in self.tracker.candidates(
                    previous[:2],edge_tolerance=self.tracker.edge_tolerance)):return
            self.tracker.last=tuple(previous)
            self.log('nav3d_resume_support',dict(source_run=path.parent.name,position=previous,
                stopped_xy=stopped,z_source='previous_inferred_support',native_altitude_verified=False))
        except (ValueError,KeyError,TypeError,OSError):return

    def recover_pose_gap(self, xy, elapsed, sequence_delta):
        follower, previous = self.follower, self.tracker.last
        if (not follower or not follower.commanded_motion or previous is None
                or self.replans >= self.config.max_replans or not .4 <= elapsed <= 1.5
                or not 2 <= sequence_delta <= 8 or math.dist(xy, previous[:2]) > 3):
            return False
        support = self.tracker.candidates(xy, edge_tolerance=self.tracker.edge_tolerance)
        window = self.route.points[max(0, follower.index - 4):follower.index + 20]
        if (len(support) != 1 or abs(support[0][2] - previous[2]) > .75
                or not any(math.dist(point, support[0]) <= .65 for point in window)):
            return False
        try:
            position = self.tracker.update(xy, max_distance=3)
        except NavigationError:
            return False
        self.replans += 1
        self.follower = self.route = None
        self.log('nav3d_pose_gap_recovered', dict(previous=previous, position=position,
            gap_s=round(elapsed, 3), sequence_delta=sequence_delta, candidate=self.goal_index))
        return True

    def _step(self,now,sample,nav,goal_override,blocked_boxes,target_receipt,travel_only,retreating,fast_pose,
              task_transit):
        if now>self.deadline:return self.stop('nav3d_budget_exhausted')
        if sample is None or nav is None:return self.stop('nav3d_channel_unavailable')
        if sample.sequence!=nav.sequence or sample.quest_id!=nav.quest_id or sample.phase!=int(nav.phase) or sample.ui_map_id!=nav.map_id:
            return self.stop('nav3d_incoherent_channels')
        recent_pose = with_fast_pose(sample, fast_pose)
        if self.fast_mode is None:
            self.fast_mode = (recent_pose is not None) if self.corridor_control else fast_pose is not None
        if self.fast_mode:
            if sample.sequence != self.base_sequence:
                self.base_sequence, self.base_changed_at = sample.sequence, now
            elif now - self.base_changed_at > .5:
                return self.stop('nav3d_stale_base_pose')
            if recent_pose is None:
                if not self.corridor_control:
                    return self.stop('nav3d_fast_pose_unavailable')
                if self.follower is not None:
                    self.follower.hold_forward = self.follower.commanded_motion = False
                if now - self.changed_at <= .5:
                    return TravelCommand()
                # Release on the first missing fast frame. Replan from the
                # slower verified pose only if the fast stream stays absent.
                self.fast_mode = False
                self.follower = self.route = self.corridor = self.sequence = None
                self.log('nav3d_fast_pose_fallback', {'position_xy': sample.position_xy})
            else:
                sample = recent_pose
        if self.sequence==sample.sequence:
            if now-self.changed_at>.5:return self.stop('nav3d_stale_pose')
            # Between receipts keep the follower's established hold instead of
            # releasing W into stutter steps; freshness stays bounded above.
            return TravelCommand(keys=frozenset({"w"})) if (
                self.follower is not None and self.follower.hold_forward) else TravelCommand()
        sequence_delta = (sample.sequence - self.sequence) % (256 if self.fast_mode else 128) if self.sequence is not None else 0
        elapsed = now - self.changed_at
        self.sequence=sample.sequence;self.changed_at=now
        frame=self.mesh.frame
        phase_id=f'quest:{sample.quest_id}:{sample.phase}'
        if phase_id in self.scene_phases:
            phase_id=frame.phase_id
        if frame.phase_id!=phase_id and self.exit_goal is not None:
            # Static collision geometry is unchanged across quest phases and
            # quest labels; the mesh's scene tag never binds an exit segment.
            # Released once per exit segment and logged; world and build
            # checks below stay strict.
            if not self.exit_phase_released:
                self.exit_phase_released=True
                self.log('nav3d_exit_phase_released',dict(mesh_phase=frame.phase_id,current_phase_id=phase_id))
            phase_id=frame.phase_id
        if (sample.world_id!=frame.world_id or str(sample.client_build)!=frame.client_build.rsplit('.',1)[-1]
                or frame.phase_id!=phase_id):
            return self.stop('nav3d_world_frame_changed')
        if sample.position_xy is None or sample.heading is None:return self.stop('nav3d_position_unavailable')
        movement_in_combat = travel_only and (task_transit or retreating and self.exit_goal is not None)
        if not sample.grounded or not sample.bindings_verified or (sample.in_combat and not movement_in_combat):
            self.log('nav3d_unsafe_state', dict(grounded=sample.grounded,
                bindings_verified=sample.bindings_verified,in_combat=sample.in_combat,
                travel_flags=sample.travel_flags,position_xy=sample.position_xy,sequence=sample.sequence))
            return self.stop('nav3d_state_unsafe')
        if self.exit_goal is None:
            if goal_override is not None and tuple(goal_override)!=nav.destination:
                return self.stop('nav3d_goal_override_requires_world_conversion')
            if sample.goal_xy is None or sample.goal_world_id!=sample.world_id:
                return self.stop('nav3d_goal_world_mismatch')
            identity=(sample.world_id,sample.ui_map_id,sample.quest_id,sample.phase,sample.goal_world_id,sample.goal_xy)
            if self.identity is not None and self.identity!=identity:return self.stop('nav3d_goal_changed')
            self.identity=identity
        self.log('nav3d_pose',dict(sequence=sample.sequence,position_xy=sample.position_xy,
            heading=sample.heading,public_z=sample.public_z,elapsed=self.config.budget_s-(self.deadline-now),
            exit=self.exit_goal is not None,retreating=retreating and sample.in_combat,
            task_transit=task_transit and sample.in_combat))
        if self.exit_goal is not None and self.tracker.last is None:
            # Self-sufficient startup (user 2026-11-18): stacked floors are
            # tried bottom-up with live receipts; run-history seeding above is
            # only a fast path. A wrong floor fails through support loss, stuck
            # or deviation and advances; this is a mesh-candidate trial, never
            # a native altitude claim.
            levels=sorted({p[2] for p in self.tracker.candidates(
                sample.position_xy,edge_tolerance=self.tracker.edge_tolerance)})
            if len(levels)>1:
                self.exit_hypotheses=levels;self.exit_hypothesis_index=0
                self.tracker.last=(*sample.position_xy,levels[0])
                self.log('nav3d_exit_floor_hypothesis',dict(index=0,support=self.tracker.last,
                    reason='stacked_start',levels=levels,z_source='mesh_candidate_trial',
                    native_altitude_verified=False))
        if self.tracker.last is None:self.resume_support(sample)
        try:position=self.tracker.update(sample.position_xy)
        except NavigationError as exc:
            if (travel_only and not retreating and str(exc) == 'nav3d_displacement_discontinuity'
                    and self.recover_pose_gap(sample.position_xy, elapsed, sequence_delta)):
                return TravelCommand()
            if self._advance_floor_hypothesis(str(exc),sample.position_xy):
                return TravelCommand()
            self.log('nav3d_support_failure',dict(reason=str(exc),previous=self.tracker.last,
                position_xy=sample.position_xy,exact=self.tracker.candidates(sample.position_xy),
                nearby=self.tracker.candidates(sample.position_xy,edge_tolerance=self.tracker.edge_tolerance)))
            raise
        if target_receipt and self.config.mode=='nav3d' and self.exit_goal is None:
            self.log('nav3d_target_receipt',target_receipt)
            return self.stop('nav3d_target_confirmed')
        pose=WorldPose(frame,position,sample.heading,sample.sequence,now,sample.grounded,sample.bindings_verified,sample.in_combat)
        revision=hashlib.sha256(json.dumps(blocked_boxes).encode()).hexdigest()
        if self.goals is None:
            self.goals=(list(self.exit_candidates) if self.exit_goal is not None
                else sorted(self.tracker.candidates(sample.goal_xy),key=lambda p:p[2]))
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
            self.route=None
            self.corridor = None
            while self.route is None:
                while self.goal_index<len(self.goals):
                    goal=self.goals[self.goal_index]
                    try:
                        query = Query(frame, position, goal, blocked_boxes=blocked_boxes)
                        if ((self.mesh.tile_cells or self.corridor_control) and not blocked_boxes
                                and self.config.mode == 'nav3d'):
                            self.route, self.corridor = self.mesh.plan_corridor(query)
                            self.route = self.route_with_clearance(self.route, self.corridor, query.spacing)
                        else:
                            self.route = self.mesh.plan(query)
                    except NavigationError as exc:
                        self.next_candidate(str(exc));continue
                    if self.forward_goal is not None and not route_advances_toward(
                            position, self.route.points, self.forward_goal):
                        self.next_candidate('nav3d_route_away_from_objective')
                        continue
                    break
                if self.route is None and not self._advance_floor_hypothesis('exit_candidates_exhausted',position[:2]):
                    return self.stop('nav3d_exit_unreachable' if self.exit_goal is not None else 'nav3d_candidates_exhausted')
            self.log('nav3d_plan',dict(route=self.route.to_dict(),
                mode='exit' if self.exit_goal is not None else self.config.mode,
                z_source='inferred_mesh_support',public_z=sample.public_z,
                corridor_polygons=self.corridor.count if self.corridor else None))
            self.follower=WorldRouteFollower(self.route,now=now,mesh_id=self.mesh.mesh_id,obstacle_revision=revision,
                alignment_verified=self.alignment,supervised_trial=self.supervised_trial,
                budget=max(.001,self.deadline-now),short_steps=self.short_steps,arrival_radius=self.arrival_radius,
                feedback_paced=self.fast_mode or self.corridor_control)
        if self.config.mode=='nav3d-shadow':
            return TravelCommand(outcome='nav3d_shadow_complete')
        if self.corridor is not None:
            try:
                self.corridor.advance(self.tracker.edge_projection or position,
                                      edge_tolerance=self.tracker.edge_tolerance)
            except NavigationError as exc:
                self.log('nav3d_corridor_lost', dict(position=position, reason=str(exc)))
                self.corridor = None
        forward_clearance = None
        if self.fast_mode or self.corridor_control:
            # A ray from projected XY cannot clear a cruise hold from raw XY.
            if self.corridor is not None and self.tracker.support_basis == 'mesh_triangle':
                forward_clearance = self.corridor.forward_clearance
            elif self.corridor_control or self.corridor is not None:
                forward_clearance = lambda *_: 0.
        command=self.follower.step(now,pose,mesh_id=self.mesh.mesh_id,obstacle_revision=revision,
            retreating=movement_in_combat, forward_clearance=forward_clearance, forward_goal=self.forward_goal)
        control = dict(sequence=sample.sequence,position=position,candidate=self.goal_index,
            support_basis=self.tracker.support_basis,waypoint=self.follower.index,
            tap=command.tap,hold_s=command.hold_s,outcome=command.outcome)
        if self.corridor is not None and self.tracker.edge_projection is not None:
            control['corridor_edge_projection'] = self.tracker.edge_projection
        self.log('nav3d_control', control)
        if command.outcome=='nav3d_route_deviation':
            # Drift off the dense polyline: replan to the SAME goal from the
            # current verified support. Rotating to a different exit corner
            # here is what spun the character through ~360 degrees in run
            # 20261118T142539 before exhausting the replan budget.
            if self._advance_floor_hypothesis(command.outcome,position[:2]):
                return TravelCommand()
            self.replans+=1
            if self.corridor is not None and self.replans <= self.config.max_replans:
                try:
                    route = self.corridor.remaining_route()
                    if self.forward_goal is not None and not route_advances_toward(
                            position, route.points, self.forward_goal):
                        raise NavigationError('nav3d_route_away_from_objective')
                    self.follower.resume_corridor(route, now=now)
                except NavigationError as exc:
                    self.log('nav3d_corridor_lost', dict(position=position, reason=str(exc)))
                else:
                    self.route = route
                    self.log('nav3d_corridor_repaired', dict(position=position, goal=route.requested_goal,
                        remaining_length=route.length, polygons=self.corridor.count, candidate=self.goal_index))
                    return TravelCommand()
            self.follower=None;self.route=None
            self.log('nav3d_candidate_replanned',dict(index=self.goal_index,
                goal=self.goals[self.goal_index],reason=command.outcome))
            return TravelCommand()
        if command.outcome in {'nav3d_stuck_replan_required','nav3d_replan_required'}:
            # Deviation from the dense polyline is recoverable from verified
            # support by replanning; the replan budget still bounds it. During
            # an unconfirmed start-floor trial, switch floors first instead.
            if self._advance_floor_hypothesis(command.outcome,position[:2]):
                return TravelCommand()
            self.replans+=1
            self.next_candidate(command.outcome)
            return TravelCommand()
        if command.outcome=='nav3d_route_arrived' and self.exit_goal is not None:
            self.log('nav3d_exit_arrived',dict(exit=self.goals[self.goal_index],position=position,
                support_basis=self.tracker.support_basis,goal_xy=self.exit_goal))
            return self.stop('nav3d_exit_reached')
        if command.outcome=='nav3d_route_arrived' and not travel_only:
            self.arrived_at=now
            self.log('nav3d_candidate_arrived',dict(index=self.goal_index,goal=self.goals[self.goal_index],
                task_receipt_confirmed=False))
            return TravelCommand()
        if command.outcome:return self.stop(command.outcome)
        return command
