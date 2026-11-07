"""Metric grid routing around obstacles discovered by the movement feedback loop."""
import heapq
import math


class GridRouter:
    def __init__(self, width: float, height: float, cell_size: float = 6):
        self.width, self.height, self.cell_size = width, height, cell_size
        self.blocked: set[tuple[int, int]] = set()

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
            self.blocked.add(self.cell(((x + (tx - x) / length * self.cell_size) / self.width,
                                        (y + (ty - y) / length * self.cell_size) / self.height)))

    def route(self, position, destination):
        if not self.blocked:
            return [destination]
        start, goal = self.cell(position), self.cell(destination)
        max_x, max_y = math.floor(self.width / self.cell_size), math.floor(self.height / self.cell_size)
        frontier = [(math.dist(start, goal), 0.0, start)]
        costs, previous = {start: 0.0}, {}
        visited = 0
        while frontier and visited < 20000:
            _, cost, current = heapq.heappop(frontier)
            if cost != costs[current]:
                continue
            visited += 1
            if current == goal:
                cells = []
                while current != start:
                    cells.append(current)
                    current = previous[current]
                cells.reverse()
                return [(min(1.0, (x + .5) * self.cell_size / self.width),
                         min(1.0, (y + .5) * self.cell_size / self.height)) for x, y in cells[:-1]] + [destination]
            x, y = current
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                neighbor = x + dx, y + dy
                if not 0 <= neighbor[0] <= max_x or not 0 <= neighbor[1] <= max_y or neighbor in self.blocked:
                    continue
                if dx and dy and ((x + dx, y) in self.blocked or (x, y + dy) in self.blocked):
                    continue
                candidate = cost + math.hypot(dx, dy)
                if candidate < costs.get(neighbor, math.inf):
                    costs[neighbor], previous[neighbor] = candidate, current
                    heapq.heappush(frontier, (candidate + math.dist(neighbor, goal), candidate, neighbor))
        return []
