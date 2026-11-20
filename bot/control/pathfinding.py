"""Metric grid routing around obstacles discovered by the movement feedback loop."""
import heapq
import math

from .navigation import advances_toward

KNOWN_PASSAGE_COST = .7


class GridRouter:
    def __init__(self, width: float, height: float, cell_size: float = 6):
        self.width, self.height, self.cell_size = width, height, cell_size
        self.blocked: set[tuple[int, int]] = set()
        self.passages: set[frozenset[tuple[int, int]]] = set()

    def metric(self, position):
        return position[0] * self.width, position[1] * self.height

    def distance(self, first, second):
        a, b = self.metric(first), self.metric(second)
        return math.dist(a, b)

    def cell(self, position):
        x, y = self.metric(position)
        return math.floor(x / self.cell_size), math.floor(y / self.cell_size)

    def block_ahead(self, position, toward):
        x, y = self.metric(position)
        tx, ty = self.metric(toward)
        length = math.hypot(tx - x, ty - y)
        if length:
            contact = self.cell(((x + (tx - x) / length * self.cell_size) / self.width,
                                 (y + (ty - y) / length * self.cell_size) / self.height))
            if contact not in self.blocked:
                self.blocked.add(contact)
                return contact

    def remember_passage(self, trace):
        """Record adjacent cells crossed during a confirmed local detour."""
        cells = [self.cell(point) for point in trace]
        for first, second in zip(cells, cells[1:]):
            if first != second and max(abs(first[0] - second[0]), abs(first[1] - second[1])) == 1:
                self.passages.add(frozenset((first, second)))

    def route(self, position, destination, *, smooth=False, forward_goal=None):
        if not self.blocked:
            return [destination] if forward_goal is None or advances_toward(
                self.metric(position), self.metric(destination), self.metric(forward_goal)) else []
        start, goal = self.cell(position), self.cell(destination)
        max_x, max_y = math.floor(self.width / self.cell_size), math.floor(self.height / self.cell_size)
        # A shortest route can use each discounted edge at most once.
        max_discount = (1 - KNOWN_PASSAGE_COST) * math.sqrt(2) * len(self.passages)

        def cell_point(cell):
            if cell == goal:
                return destination
            if cell == start:
                return position
            x, y = cell
            return (min(1., (x + .5) * self.cell_size / self.width),
                    min(1., (y + .5) * self.cell_size / self.height))

        forward_point = self.metric(forward_goal) if forward_goal is not None else None

        def estimate(cell):
            distance = math.dist(cell, goal)
            return max(distance * KNOWN_PASSAGE_COST, distance - max_discount)

        frontier = [(estimate(start), 0.0, start)]
        costs, previous = {start: 0.0}, {}
        visited = 0
        while frontier and visited < 20000:
            _, cost, current = heapq.heappop(frontier)
            if cost != costs[current]:
                continue
            visited += 1
            if current == goal:
                if forward_point is not None and start == goal and not advances_toward(
                        self.metric(position), self.metric(destination), forward_point):
                    return []
                cells = []
                while current != start:
                    cells.append(current)
                    current = previous[current]
                cells.reverse()
                points = [cell_point(cell) for cell in cells[:-1]]
                points.append(destination)
                return self.smooth_path(position, points) if smooth else points
            x, y = current
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                neighbor = x + dx, y + dy
                if not 0 <= neighbor[0] <= max_x or not 0 <= neighbor[1] <= max_y or neighbor in self.blocked:
                    continue
                if dx and dy and ((x + dx, y) in self.blocked or (x, y + dy) in self.blocked):
                    continue
                if forward_point is not None and not advances_toward(
                        self.metric(cell_point(current)), self.metric(cell_point(neighbor)), forward_point):
                    continue
                weight = KNOWN_PASSAGE_COST if frozenset((current, neighbor)) in self.passages else 1.0
                candidate = cost + math.hypot(dx, dy) * weight
                if candidate < costs.get(neighbor, math.inf):
                    costs[neighbor], previous[neighbor] = candidate, current
                    heapq.heappush(frontier, (candidate + estimate(neighbor), candidate, neighbor))
        return []

    def smooth_path(self, position, points):
        """Combine short grid legs when they do not cross a known obstruction."""
        path = []
        first = 0
        while first < len(points):
            end = min(first + 6, len(points) - 1)
            while end > first and not self.clear_segment(position, points[end]):
                end -= 1
            position = points[end]
            path.append(position)
            first = end + 1
        return path

    def clear_segment(self, start, end):
        x, y = start
        dx, dy = end[0] - x, end[1] - y
        count = max(1, math.ceil(self.distance(start, end) / (self.cell_size / 4)))
        previous = self.cell(start)
        for step in range(1, count + 1):
            cell = self.cell((x + dx * step / count, y + dy * step / count))
            if cell == previous:
                continue
            if cell in self.blocked or (cell[0] != previous[0] and cell[1] != previous[1]
                    and ((cell[0], previous[1]) in self.blocked or (previous[0], cell[1]) in self.blocked)):
                return False
            previous = cell
        return True
