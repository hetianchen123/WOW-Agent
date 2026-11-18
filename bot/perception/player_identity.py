"""Separate avatar identity evidence from ground-point geometry."""
import math

# Initial live scope is the current Troll hunter. Unknown races remain unsupported,
# rather than guessing race IDs or treating a model's own assertion as a receipt.
RACES = {8: "troll"}


def profile_from_sample(sample):
    if sample is None or sample.race_id not in RACES:
        return None
    return {"race_id": sample.race_id, "race": RACES[sample.race_id], "class_id": sample.class_id}


def identity_candidate(box, observed_race, evidence, profile):
    if not profile or observed_race != profile.get("race"):
        return False, "race_unknown_or_mismatch"
    if not isinstance(evidence, str) or not evidence.strip():
        return False, "appearance_evidence_missing"
    if (not isinstance(box, list) or len(box) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)):
        return False, "player_box_missing"
    x, y, right, bottom = box
    # A necessary check for the saved centered camera, never proof by itself.
    if not (0 < right - x <= 350 and 0 < bottom - y <= 650
            and 420 <= (x + right) / 2 <= 580 and 300 <= bottom <= 1000):
        return False, "outside_avatar_region"
    return True, "needs_second_view"


class PlayerIdentityGate:
    def __init__(self):
        self.clear()

    def clear(self):
        self.references = []

    def review(self, view, pose):
        result = dict(view)
        if view.get("schema") == 5:
            verified = (pose.get("anchor_profile") == "centered_view_2"
                        and view.get("coordinate_space") == "scene"
                        and view.get("identity_basis") == "fixed_camera_anchor"
                        and view.get("identity_candidate") is True and view.get("origin_verified") is True)
            result["identity_verified"] = verified
            if not verified:
                result.update(front="unknown", left="unknown", right="unknown", next_view="scan")
                result.pop("landmark", None)
            return result
        at, heading = pose["at"], pose["heading"]
        self.references = [r for r in self.references if 0 <= at - r["at"] <= 20]
        verified = False
        if view.get("identity_candidate") is True:
            box = view["player_box"]
            for ref in self.references:
                if ref.get("map_id") != pose.get("map_id"):
                    continue
                if pose.get("position") and ref.get("position") and pose.get("world_size"):
                    distance = math.hypot(*[(a - b) * size for a, b, size in zip(
                        pose["position"], ref["position"], pose["world_size"])])
                    if distance > 3:
                        continue
                delta = abs((heading - ref["heading"] + math.pi) % (2 * math.pi) - math.pi)
                old = ref["box"]
                if (math.radians(20) <= delta <= math.radians(160)
                        and abs((box[0] + box[2] - old[0] - old[2]) / 2) <= 60
                        and .5 <= (box[3] - box[1]) / (old[3] - old[1]) <= 2):
                    verified = True
                    break
            self.references.append({"at": at, "heading": heading, "box": box,
                                    "map_id": pose.get("map_id"), "position": pose.get("position")})
            self.references = self.references[-6:]
        result["identity_verified"] = verified
        result["identity_reason"] = ("race_and_cross_view_anchor" if verified else
                                     view.get("identity_reason", "identity_evidence_missing"))
        if not verified:
            result.update(front="unknown", left="unknown", right="unknown", next_view="scan")
            result.pop("landmark", None)
        return result
