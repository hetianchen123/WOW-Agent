"""Synthetic contrast pairs. The first declared candidate is the static baseline."""

PAIRS = [
    (
        "gold_route",
        "采集赚金",
        "选择下一个采集区。两条路均可通行，按未来五分钟可采集的节点数选择。",
        {"near_loop": "跑近处环线", "ridge": "去山脊采集区"},
        {"near_loop_nodes": 6, "ridge_nodes": 2},
        {"near_loop_nodes": 1, "ridge_nodes": 7},
    ),
    (
        "gold_bags",
        "采集赚金",
        "选择接下来的一段工作。先保证下一轮预计收获装得下，能装下就继续采集。",
        {"keep_gathering": "继续下一轮采集", "bank_first": "先清理背包再回来"},
        {"free_slots": 12, "next_loop_required_slots": 6},
        {"free_slots": 3, "next_loop_required_slots": 6},
    ),
    (
        "quest_batch",
        "任务清理",
        "两个交付顺序均可行。优先交付主线来解锁后续。若主线尚未完成，先清掉可交付的支线。",
        {"main_first": "先处理主线，完成后交给主线 NPC", "side_first": "先交付已完成的支线"},
        {"main_objective": "8/8", "main_ready_to_turn_in": True, "side_ready_to_turn_in": True},
        {"main_objective": "7/8", "main_ready_to_turn_in": False, "side_ready_to_turn_in": True},
    ),
    (
        "quest_travel",
        "任务清理",
        "两条路线均可到达任务区。根据预估总耗时选择更快的方案，飞行总耗时已包含去飞行点的时间。",
        {"take_flight": "去飞行点乘坐航线", "ride_direct": "骑乘直达任务区"},
        {"flight_total_seconds": 85, "ride_total_seconds": 160},
        {"flight_total_seconds": 170, "ride_total_seconds": 95},
    ),
    (
        "mythic_count",
        "大秘境回放",
        "模拟路线已到最后的可选敌方队伍。优先满足结算所需的敌方部队进度，已达标就前进。",
        {"advance": "跳过可选队伍，前往终点", "add_pack": "先处理可选队伍"},
        {"enemy_forces_pct": 100, "optional_pack_pct": 7, "required_forces_pct": 100},
        {"enemy_forces_pct": 94, "optional_pack_pct": 7, "required_forces_pct": 100},
    ),
    (
        "mythic_recovery",
        "大秘境回放",
        "在模拟回放里决定下一段。全队集合且关键资源可用时继续原路线，否则先在检查点集合。",
        {"resume_route": "继续原路线", "regroup": "先在检查点集合"},
        {"party_nearby": 5, "party_size": 5, "key_resources_ready": True},
        {"party_nearby": 3, "party_size": 5, "key_resources_ready": False},
    ),
]
