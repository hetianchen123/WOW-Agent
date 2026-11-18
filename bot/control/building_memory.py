"""Task-local regions connected by measured portal crossings, not guessed floors.

No region is merged by XY, height, outdoors state or a similar-looking picture.
An explored branch is left only through its measured parent passage, after a
fresh route and another native crossing receipt. Nothing here authorizes input.
"""
from copy import deepcopy
import math

from ..perception.ground_projection import finite_pair
from .navigation import compass_from_delta, heading_error
from .quest_travel import TravelCommand


class BuildingRouteMemory:
    def __init__(self, world_size):
        self.world_size = tuple(world_size)
        self.current = 0
        self.areas = {0: dict(visited=[], blocked=[], exhausted=False)}
        self.passages = []

    def distance(self, a, b):
        return math.hypot(*[(y-x)*s for x,y,s in zip(a,b,self.world_size)])

    @property
    def visited(self):
        return self.areas[self.current]['visited']

    @property
    def blocked(self):
        return self.areas[self.current]['blocked']

    def return_edge(self):
        return next((p for p in reversed(self.passages)
                     if p['to_area'] == self.current and not p['returned']), None)

    def incident(self):
        return [p for p in self.passages if self.current in (p['from_area'],p['to_area'])]

    def snapshot(self):
        return deepcopy(dict(schema=1, current_area=self.current, level_id=None, room_id=None,
                             area_basis='verified_portal_search_context',
                             passages=self.incident(), return_edge=self.return_edge()))

    def crossed(self, receipt, *, return_id=None):
        start,end=receipt.get('threshold_position'),receipt.get('position')
        if (not finite_pair(start) or not finite_pair(end)
                or any(not 0<=v<=1 for v in (*start,*end))
                or not .3<=self.distance(start,end)<=6
                or receipt.get('basis')!='measured_door_floor_and_native_movement'
                or not receipt.get('source_frame')
                or receipt.get('before_sequence')==receipt.get('after_sequence')):
            raise ValueError('building_passage_receipt_invalid')
        previous=self.current
        if return_id is not None:
            edge=self.return_edge()
            if not edge or edge['passage_id']!=return_id:
                raise ValueError('building_return_identity_changed')
            # The new crossing must actually regain the previous side, not
            # merely reach the middle of the same door from the current room.
            a,b=edge['threshold_position'],edge['position']
            if (min(self.distance(start,p) for p in (a,b))>1.5
                    or self.distance(end,a)+.15>self.distance(end,b)):
                raise ValueError('building_return_side_unconfirmed')
            self.areas[previous]['exhausted']=True
            edge['returned']=True
            edge['return_receipt']=deepcopy(receipt)
            self.current=edge['from_area']
            passage_id=edge['passage_id']
        else:
            self.current=len(self.areas)
            self.areas[self.current]=dict(visited=[],blocked=[],exhausted=False)
            passage_id=len(self.passages)
            self.passages.append(dict(deepcopy(receipt),passage_id=passage_id,
                from_area=previous,to_area=self.current,returned=False,level_id=None))
        self.areas[previous]['visited'].append(tuple(start))
        self.visited.extend([tuple(start),tuple(end)])
        return dict(from_area=previous,to_area=self.current,passage_id=passage_id,
                    returning=return_id is not None,level_id=None,task_arrival_verified=False)


def same_camera_controls(first,second):
    # Collision with nearby scenery can change the native zoom while walking.
    return first==second or (first is not None and second is not None and
        all(getattr(first,key,None)==getattr(second,key,None)
            for key in ('view','scene','applied','request','result','paused','calibrated')))


class NativeTurn:
    """Turn in place and wait for a fresh, settled native heading."""
    def __init__(self, now, nav, desired, convention, *, turnaround=False):
        self.started=now;self.position=nav.position;self.height=nav.height
        self.camera,self.epoch=nav.camera,nav.camera_epoch
        self.convention,self.desired=convention,desired
        self.turnaround=turnaround
        self.pending=None;self.settle=None;self.pulses=0

    def step(self,now,nav,heading):
        if now-self.started>=12 or self.pulses>=24:
            return TravelCommand(outcome='building_return_look_budget')
        if (math.hypot(*[(a-b)*s for a,b,s in zip(nav.position,self.position,nav.world_size)])>.15
                or abs(nav.height-self.height)>.25 or nav.camera_epoch!=self.epoch
                or not (same_camera_controls(nav.camera,self.camera) if self.turnaround else nav.camera==self.camera)
                or getattr(nav.camera,'paused',False)):
            return TravelCommand(outcome='probe_pose_changed')
        if self.pending:
            at,seq,angle=self.pending
            if now-at<.45 or nav.sequence==seq:return TravelCommand()
            if abs(heading_error(angle,heading))<.005:
                return TravelCommand(outcome='building_return_turn_no_progress')
            self.pending=None
        error=heading_error(heading,self.desired)
        if abs(error)<=math.radians(5):
            if self.settle is None:self.settle=(now,nav.sequence)
            at,seq=self.settle
            return None if now-at>=.5 and nav.sequence!=seq else TravelCommand()
        self.settle=None;self.pending=(now,nav.sequence,heading);self.pulses+=1
        left=error>0 if self.convention=='ccw_west' else error<0
        # Use the existing task turn envelope; shorten the last correction so
        # a full pulse cannot oscillate across the five-degree settling band.
        duration=min(.11 if self.turnaround else .06,max(.025,abs(error)/math.radians(240)))
        return TravelCommand(tap='left' if left else 'right',hold_s=duration,precise=True)


class PassageLook(NativeTurn):
    """One stationary look toward a native passage endpoint; no translation."""
    def __init__(self, now, nav, edge, convention):
        a,b=edge['threshold_position'],edge['position']
        delta=[(x-y)*s for x,y,s in zip(a,nav.position,nav.world_size)]
        if math.hypot(*delta)<.2:
            delta=[(x-y)*s for x,y,s in zip(a,b,nav.world_size)]
        super().__init__(now,nav,compass_from_delta(*delta,convention),convention)
