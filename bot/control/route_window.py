"""Bounded world-space prefix following; every pulse needs a new native receipt."""
import math


class RouteWindow:
    def __init__(self,result,world_size,captured_at):
        contract=result['execution_window']
        probing=result.get('approach',{}).get('requires_probe')
        self.limit=.75 if probing is True else 2.
        self.max_pulses=4 if probing is True else 8
        if (type(probing) is not bool or contract!={
                'schema':1,'requires_probe':probing,'cross_track_yards':.15,
                'max_age_s':12,'max_pulses':self.max_pulses}
                or result['movement_budget_yards']>self.limit):
            raise ValueError('segmentation_window_invalid')
        self.limit=result['movement_budget_yards']
        self.points=[tuple(p) for p in result['next_segment_world']]
        self.size=world_size
        self.expires_at=captured_at+12
        self.index=1
        self.pulses=0
        self.traveled=0.

    def distance(self,a,b):
        return math.hypot(*[(y-x)*s for x,y,s in zip(a,b,self.size)])

    def target(self,position):
        while self.index<len(self.points) and self.distance(position,self.points[self.index])<=.06:
            self.index+=1
        return self.points[self.index] if self.index<len(self.points) else None

    def feedback(self,before,after):
        a,b=self.points[self.index-1:self.index+1]
        vx,vy=[(y-x)*s for x,y,s in zip(a,b,self.size)]
        px,py=[(y-x)*s for x,y,s in zip(a,after,self.size)]
        length=math.hypot(vx,vy)
        if length<=1e-8:raise ValueError('segmentation_window_invalid')
        along=(px*vx+py*vy)/length
        cross=abs(px*vy-py*vx)/length
        if cross>.15 or along<-.06 or along>length+.06:
            raise ValueError('segmentation_route_deviation')
        self.traveled+=self.distance(before,after)
        if self.traveled>self.limit+.03:raise ValueError('segmentation_window_overshoot')

    def remaining(self):
        return max(0.,self.limit-self.traveled)

    def reusable(self,position,now):
        return (now<self.expires_at and self.pulses<self.max_pulses
                and self.remaining()>.08 and self.target(position) is not None)
