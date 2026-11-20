"""Find bounded entry and exit points on a map-local ground road network."""
from dataclasses import dataclass
import heapq
import math


@dataclass(frozen=True)
class RoadTrace:
    points: tuple
    mode: str = "map"
    bidirectional: bool = True


def projection(point, start, stop):
    dx, dy = stop[0] - start[0], stop[1] - start[1]
    denominator = dx * dx + dy * dy
    if not denominator:
        return start, 0.0
    t = max(0., min(1., ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator))
    return (start[0] + t * dx, start[1] + t * dy), t


def preferred_road(router, position, destination, traces, mode):
    direct = router.distance(position, destination)
    if direct < 65 or mode not in {"walk", "mount"}:
        return []
    max_access = min(95, max(25, direct * .28))
    max_length = direct * 1.65 + 15
    start_xy, end_xy = router.metric(position), router.metric(destination)
    graph, exits = {}, {}
    start = None

    def connect(a, b, road=False, reverse=False):
        if a == b:
            return
        physical_a = position if a is None else a
        if router.blocked and not router.clear_segment(physical_a, b):
            return
        length = router.distance(physical_a, b)
        cost = length * (.65 if road else 1.7)
        graph.setdefault(a, []).append((b, cost, length if road else 0, length))
        if reverse:
            graph.setdefault(b, []).append((a, cost, length, length))

    for trace in traces:
        if trace.mode not in {"map", "ground" if mode == "mount" else "walk"}:
            continue
        for a, b in zip(trace.points, trace.points[1:]):
            a_xy, b_xy = router.metric(a), router.metric(b)
            if a == b or math.dist(start_xy, projection(start_xy, a_xy, b_xy)[0]) > max_length:
                continue
            splits = [(0., a), (1., b)]
            anchors = []
            for location in (start_xy, end_xy):
                xy, t = projection(location, a_xy, b_xy)
                point = a if t == 0 else b if t == 1 else (xy[0] / router.width, xy[1] / router.height)
                if math.dist(location, xy) <= max_access:
                    splits.append((t, point))
                    anchors.append(point)
                else:
                    anchors.append(None)
            entry, exit_point = anchors
            if entry is not None:
                connect(start, entry)
            if exit_point is not None and (not router.blocked or router.clear_segment(exit_point, destination)):
                exits[exit_point] = router.distance(exit_point, destination)
            ordered = [point for _, point in sorted(set(splits))]
            for first, second in zip(ordered, ordered[1:]):
                connect(first, second, road=True, reverse=trace.bidirectional)

    if not graph.get(start):
        return []
    best = None
    partial = None
    frontier = [(0., 0, start, 0., 0.)]
    count = 0
    costs, parents = {start: 0.}, {}
    while frontier:
        cost, _, node, on_road, length = heapq.heappop(frontier)
        if cost != costs[node]:
            continue
        if best and cost >= best[0]:
            break
        if node in exits:
            total = length + exits[node]
            total_cost = cost + exits[node] * 1.7
            if on_road >= max(35, total * .5) and total <= max_length and (not best or total_cost < best[0]):
                best = total_cost, node
        elif node is not start and direct >= 160:
            remaining = router.distance(node, destination)
            total = length + remaining
            if (remaining <= direct * .75 and total <= max_length
                    and on_road >= max(50, direct * .24, length * .65)):
                rank = cost + remaining * 1.7
                if not partial or rank < partial[0]:
                    partial = rank, node
        for adjacent, weight, road_length, edge_length in graph.get(node, ()):
            candidate = cost + weight
            if candidate < costs.get(adjacent, math.inf) and length + edge_length <= max_length:
                costs[adjacent], parents[adjacent] = candidate, node
                count += 1
                heapq.heappush(frontier, (candidate, count, adjacent, on_road + road_length, length + edge_length))
    answer = best or partial
    if not answer:
        return []
    path = []
    node = answer[1]
    while node is not start:
        path.append(node)
        node = parents[node]
    path.reverse()
    if best and path[-1] != destination:
        path.append(destination)
    return path
