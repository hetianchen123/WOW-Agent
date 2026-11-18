"""Persistent local coverage of native quest-blob samples or a bounded fallback."""
import math

from .pathfinding import GridRouter


def inside_quest_search_area(nav):
    router = GridRouter(*nav.world_size)
    if nav.search_area is not None:
        return any(router.distance(nav.position, point) <= 22 for point in nav.search_area)
    return router.distance(nav.position, nav.destination) <= 90


class QuestAreaSearch:
    def __init__(self, nav, *, center=None):
        self.quest_id, self.map_id = nav.quest_id, nav.map_id
        self.router = GridRouter(*nav.world_size)
        self.center = center if center is not None else nav.destination
        self.visited = []
        self.steps = 0
        self.native = nav.search_area is not None
        if self.native:
            self.points = nav.search_area
        else:
            w, h = nav.world_size
            self.points = tuple((self.center[0] + x*30/w, self.center[1] + y*30/h)
                                for y in range(-3, 4) for x in range(-3, 4)
                                if 0 < x*x + y*y <= 9 and
                                all(0 <= p <= 1 for p in (self.center[0]+x*30/w, self.center[1]+y*30/h)))

    def next(self, position, heading=None, convention="ccw_west", avoid=()):
        if self.steps >= 32:
            return None
        self.visited.append(position)
        choices = [p for p in self.points if all(self.router.distance(p, old) > 20 for old in self.visited)
                   and all(self.router.distance(p, point) >= 20 for point in avoid)]
        if not choices:
            return None
        def cost(point):
            distance = self.router.distance(position, point)
            if heading is None:
                return distance
            dx, dy = (point[0]-position[0])*self.router.width, (point[1]-position[1])*self.router.height
            east = math.sin(heading) * (-1 if convention == "ccw_west" else 1)
            forward = (dx*east - dy*math.cos(heading)) / max(distance, .01)
            return distance + (1-forward)*12
        point = min(choices, key=cost)
        # Also excludes an unreachable attempted point; do not keep routing back to it.
        self.visited.append(point)
        self.steps += 1
        return point
