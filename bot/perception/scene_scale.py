"""One exposure, two scales, one coordinate space (the original scene crop)."""
from dataclasses import dataclass
import math
import json

SCENE_BOUNDS = (.20, .08, .80, .68)
WINDOW_ANCHOR = (.5, .5)


def point(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1000 for v in value))


def box(value):
    return (isinstance(value, list) and len(value) == 4 and point(value[:2]) and point(value[2:])
            and value[0] < value[2] and value[1] < value[3])


def anchor_profile(camera):
    # Only the current saved, centered third-person profile is calibrated.
    from .travel_channel import CameraResult
    if (camera is None or camera.paused or camera.view != 2 or camera.zoom is None
            or camera.zoom < 20 or camera.scene != camera.applied
            or camera.result not in {CameraResult.DISTANCE_VERIFIED, CameraResult.LIMITED}):
        return None
    return {"profile": "centered_view_2", "window_anchor": list(WINDOW_ANCHOR),
            "scene_bounds": list(SCENE_BOUNDS)}


def overhead_profile(camera):
    """Preset receipt is a framing prerequisite, never proof of angle or map scale."""
    from .travel_channel import CameraResult, CameraScene
    if (camera is None or camera.paused or camera.view != 4 or camera.zoom is None
            or not 20 <= camera.zoom <= 39 or camera.applied != CameraScene.OVERHEAD
            or camera.result not in {CameraResult.DISTANCE_VERIFIED, CameraResult.LIMITED}):
        return None
    return {"profile":"native_view_4_candidate", "window_anchor":list(WINDOW_ANCHOR),
            "scene_bounds":list(SCENE_BOUNDS), "projection_calibrated":False}


@dataclass(frozen=True)
class AvatarCrop:
    width: int
    height: int
    pixels: tuple

    @classmethod
    def for_scene(cls, size):
        width, height = size
        left, top, right, bottom = SCENE_BOUNDS
        x = (WINDOW_ANCHOR[0] - left) / (right - left)
        y = (WINDOW_ANCHOR[1] - top) / (bottom - top)
        bounds = (x - .12, y - .22, x + .12, y + .18)
        return cls(width, height, tuple(round(v * n) for v, n in zip(bounds, (width, height, width, height))))

    def to_scene(self, p):
        if not point(p):
            raise ValueError("invalid crop point")
        left, top, right, bottom = self.pixels
        return [round((left + p[0] / 1000 * (right - left)) / self.width * 1000, 3),
                round((top + p[1] / 1000 * (bottom - top)) / self.height * 1000, 3)]

    def to_window(self, p):
        if not point(p):
            raise ValueError("invalid scene point")
        left, top, right, bottom = SCENE_BOUNDS
        return [left * 1000 + p[0] * (right - left), top * 1000 + p[1] * (bottom - top)]

    def avatar(self, value):
        if not isinstance(value, dict):
            raise ValueError("invalid avatar observation")
        b, feet = value.get("box"), value.get("feet")
        evidence = value.get("evidence")
        if (value.get("kind") != "humanoid" or not box(b) or not point(feet)
                or not isinstance(evidence, str) or not evidence.strip()):
            return None
        mapped = self.to_scene(b[:2]) + self.to_scene(b[2:])
        origin = self.to_scene(feet)
        # A center prior narrows the search; it does not invent a character/feet.
        if not (450 <= (mapped[0] + mapped[2]) / 2 <= 550
                and 550 <= mapped[3] <= 880 and 12 <= mapped[3] - mapped[1] <= 300
                and mapped[0] <= origin[0] <= mapped[2]
                and mapped[3] - 15 <= origin[1] <= mapped[3] + 25):
            return None
        return {"box": mapped, "feet": origin, "evidence": evidence}


def merge_scales(avatar, route, parse_observation):
    """Wide terrain must independently agree with the local feet observation."""
    if not isinstance(route, dict):
        raise ValueError("invalid wide observation")
    ground = route.get("origin")
    valid = bool(avatar and point(ground)
                 and math.dist(ground, avatar["feet"]) <= 35)
    fields = {k: route[k] for k in ("front", "left", "right", "next_view", "feature", "landmark", "evidence") if k in route}
    view = parse_observation(json.dumps(fields))
    if not valid:
        view.update(front="unknown", left="unknown", right="unknown", next_view="scan")
        view.pop("landmark", None)
    elif view["front"] == "open":
        endpoint = view.get("landmark")
        if (not point(endpoint) or not 0 < ground[1] - endpoint[1] <= 180
                or abs(endpoint[0] - ground[0]) > 100):
            view["front"] = "unknown"
            view.pop("landmark", None)
    obstacle = route.get("obstacle")
    if isinstance(obstacle, list) and len(obstacle) == 1 and box(obstacle[0]):
        obstacle = obstacle[0]
    rejection = None
    if avatar and box(obstacle) and all(abs(a-b) <= 2 for a, b in zip(obstacle, avatar["box"])):
        # Real replay copied the character box verbatim as a fence. Do not turn
        # that self-obstruction into either a route or a proven collision.
        rejection = "obstacle_is_avatar_box"
        obstacle = None
        view.update(front="unknown", left="unknown", right="unknown", next_view="scan")
        view.pop("landmark", None)
    if box(obstacle) and valid and view["front"] == "open":
        # Sample the short screen segment: visible grass beyond a wooden bar is not a route.
        endpoint = view["landmark"]
        for i in range(1, 11):
            x, y = [a + (b - a) * i / 10 for a, b in zip(ground, endpoint)]
            if obstacle[0] <= x <= obstacle[2] and obstacle[1] <= y <= obstacle[3]:
                view["front"] = "blocked"
                view.pop("landmark", None)
                break
    bypass = route.get("bypass")
    hint = None
    if isinstance(bypass, dict) and bypass.get("side") in {"left", "right"} and point(bypass.get("point")):
        # Screen endpoints authorize only a new stationary view, never world movement.
        hint = {"side": bypass["side"], "point": bypass["point"]}
    return {**view, "schema": 5, "coordinate_space": "scene", "scope": "one_short_step",
            "feet": avatar["feet"] if avatar else None, "player_box": avatar["box"] if avatar else None,
            "ground_origin": ground if valid else None, "origin_verified": valid,
            "origin_basis": "visible_feet" if valid else "unknown",
            "identity_candidate": valid, "identity_basis": "fixed_camera_anchor",
            "identity_reason": "anchor_and_ground_agree" if valid else "anchor_or_ground_unresolved",
            "route_hint": hint if not rejection else None, "obstacle": obstacle if box(obstacle) else None,
            "route_rejection": rejection}
