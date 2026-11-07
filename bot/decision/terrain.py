"""Bounded terrain decisions from a local Qwen observation and StartLux."""

from .startlux import LocalStartLux

DIRECTIONS = ("front", "left", "right")
STUCK_AFTER = 2
VIEW_NAMES = {"front": "正面", "left": "左侧", "right": "右侧", "back": "背后"}


def decide_multiview(client: LocalStartLux, views: dict, failed: frozenset[str] = frozenset()) -> dict:
    criteria = {"stop": "没有可靠的通路，停止探查。"}
    state = {}
    for direction, observation in views.items():
        front = observation["front"]
        if direction not in VIEW_NAMES or front not in ("open", "blocked", "unknown"):
            raise ValueError("invalid multiview terrain observation")
        state[direction] = {"front": front, "failed_movement": direction in failed}
        if front == "open" and direction not in failed:
            criteria[direction] = f"转向{VIEW_NAMES[direction]}，该视角的正前方可见连续地面，再拍照确认。"
    if len(criteria) == 1:
        return {"recommendation": "stop", "reason": "no_open_view"}
    return client.decide(
        {"views": state, "same_ground_position": True}, criteria,
        instructions="比较同一位置的多个朝向。优先选择正前方可见连续地面的视角。"
        "已经短步试过却没有位移的方向不能再选。选择方向只表示需要转过去重新确认。",
    )


def decide_terrain(client: LocalStartLux, observation: dict, scans: tuple[str, ...] = ()) -> dict:
    terrain = {direction: observation[direction] for direction in DIRECTIONS}
    if any(value not in ("open", "blocked", "unknown") for value in terrain.values()):
        raise ValueError("invalid terrain observation")
    criteria = {"stop": "角色不适合探查，或没有可执行的观察与移动选项时停止。"}
    if terrain["front"] == "open":
        criteria["advance"] = "沿画面正前方清楚可见的连续地面短走一步，再核对位移。"
    else:
        suggested = observation.get("next_view")
        sides = ("left", "right")
        open_sides = [side for side in sides if terrain[side] == "open"]
        if suggested in open_sides:
            open_sides.remove(suggested)
            open_sides.insert(0, suggested)
        ordered = (*open_sides, suggested, *sides)
        for side in ordered:
            if side not in sides or len(scans) >= 3:
                continue
            # Repeating a turn is useful when the new view still shows a
            # passage on that side. A third turn needs that new evidence.
            follows_passage = bool(scans and scans[-1] == side and terrain[side] == "open")
            if follows_passage or (side not in scans and len(scans) < 2):
                criteria[f"scan_{side}"] = f"角色原地向{'左' if side == 'left' else '右'}转一点，完全不走动，获取新视角。"
                break
    if len(criteria) == 1:
        return {"recommendation": "stop", "reason": "views_exhausted"}
    result = client.decide(
        {"terrain": terrain, "already_scanned": list(scans), "grounded_and_stationary": True}, criteria,
        instructions=(
            "选择一次局部地形探查动作。前方 open 时选短步试探。当前画面 blocked 或 unknown 时，"
            "如果提供了原地转向观察，选择它来收集新证据，即使画面里所有方向都堵住。"
            "原地转向不需要可通行地面。没有可用的观察或移动动作才停止。"
        ),
    )
    if result.get("reason") == "low_confidence" and terrain["front"] != "open":
        scan = next(action for action in criteria if action.startswith("scan_"))
        return {**result, "recommendation": scan, "fallback": "stationary_scan_only"}
    return result
