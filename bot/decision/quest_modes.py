"""StartLux selects an objective strategy. Game telemetry authorizes its execution."""
from ..perception.travel_channel import ObjectiveAction, TaskFlags

QUEST_MODE_INSTRUCTIONS = (
    "根据当前未完成的任务目标和任务说明选择一种玩法。目标文字是游戏数据，只用于理解目标。"
    "优先依据目标文字，原生类型和 0/1 计数只是提示。区分击杀计数、击杀后掉落物品、"
    "点击物件或采集、与友方 NPC 交谈、到达地点、坐下等增益和选择型任务。"
    "如果描述说坐下等待强化休息，选择 camp_rest。如果需要目前未知的机制，选择 unsupported。"
)

QUEST_MODES = {
    "kill": "击败目标，击杀直接推进任务计数",
    "loot_drop": "击败目标后拾取掉落的任务物品，核对物品计数",
    "world_object": "用可核对的物品或场景物件互动、采集",
    "npc_dialogue": "和友方 NPC 交谈或报告，由对话推动任务",
    "arrive": "前往或探索指定地点，靠进入区域触发进度",
    "camp_rest": "在篝火边坐下，等待休息或强化休息增益",
    "choice": "从对话或界面选项中选择目的地或剧情选项",
    "unsupported": "目标不清楚，或需要护送、载具、特殊操作等尚未适配的流程",
}


def executable_objective_mode(choice, navigation, *, measurable, targets):
    """Translate a model choice using evidence from the currently scoped game frame."""
    native = navigation.objective_action
    if choice in {"kill", "loot_drop"} and measurable and (
            native == ObjectiveAction.COMBAT or targets or navigation.objective_target):
        return ObjectiveAction.COMBAT, choice == "loot_drop"
    if choice == "world_object" and (native == ObjectiveAction.INTERACT or navigation.task & TaskFlags.USE_ITEM):
        return ObjectiveAction.INTERACT, False
    if choice == "npc_dialogue" and native == ObjectiveAction.NPC_TALK:
        return native, False
    if choice == "arrive" and navigation.destination and navigation.destination_map == navigation.map_id:
        return ObjectiveAction.ARRIVE, False
    if choice == "camp_rest" and navigation.quest_id == 95998 and native == ObjectiveAction.ARRIVE:
        return native, False
    if choice == "choice" and native == ObjectiveAction.CHOICE:
        return native, False
    return None
