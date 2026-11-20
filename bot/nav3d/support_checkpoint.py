"""Save and validate the last observed support across controller restarts."""
import json
import math
import os
from tempfile import NamedTemporaryFile

from .planner import NavigationError


class SupportCheckpoint:
    def __init__(self, path):
        self.path = path
        self.saved = None

    def save(self, identity, mesh_id, pose, support):
        record = {
            "schema": 1,
            "identity": list(identity),
            "mesh_id": mesh_id,
            "scene": [pose.client_build, pose.world_id, pose.ui_map_id],
            "position": list(support),
        }
        if record == self.saved:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, mode="w", encoding="utf-8", delete=False) as stream:
                temporary = stream.name
                json.dump(record, stream, allow_nan=False)
            os.replace(temporary, self.path)
            self.saved = record
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def restore(self, identity, tracker, pose):
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if (record["schema"] != 1 or record["identity"] != list(identity)
                    or record["mesh_id"] != tracker.mesh.mesh_id
                    or record["scene"] != [pose.client_build, pose.world_id, pose.ui_map_id]):
                return None
            x, y, z = record["position"]
            if not all(math.isfinite(v) for v in (x, y, z)) or math.dist((x, y), pose.position_xy) > .18:
                return None
            tracker.last = (x, y, z)
            return tracker.update(pose.position_xy)
        except (KeyError, TypeError, ValueError, OSError, NavigationError):
            tracker.last = None
            return None
