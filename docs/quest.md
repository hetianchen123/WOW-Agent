# 任务模块

插件从任务日志和当前 NPC 对话读取任务 ID、分类、可交付状态和单目标计数。执行器按计划里的顺序推进，重新启动时会跳过服务器已标记完成的任务。任务色条独立于原有色条，默认隐藏。

| 读取结果 | 分类依据 |
| --- | --- |
| `CAMPAIGN` 战役 | 游戏分类、任务日志的战役 ID 或任务线的战役标记 |
| `MAINLINE` 主线候选 | 任务日志标为剧情任务，且任务线未标为地区故事 |
| `SIDE` 支线候选 | 地区故事，或已有标题或日志记录的普通任务 |
| `WORLD`、`REPEATABLE` | 世界任务、重复任务和召唤分别归入对应类别 |
| `IMPORTANT`、`SPECIAL`、`UNKNOWN` | 重要任务、特殊任务，以及分类信息不足的任务分别显示 |

主线和支线是项目根据游戏元数据推断的分类。老资料片和特殊任务可能需要任务 ID 白名单来消除歧义。任务计划本身就是可执行 ID 白名单，分类筛选会再检查一遍。[暴雪任务日志 API](https://github.com/Gethe/wow-ui-source/blob/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/QuestLogDocumentation.lua) · [暴雪任务线 API](https://github.com/Gethe/wow-ui-source/blob/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/QuestLineInfoDocumentation.lua)

## 查看当前任务

1. 更新 `WowAgent` 插件后在游戏里执行 `/reload`。
2. 执行 `/wagent quest scan`。它会开启五行任务色条，轮询任务日志和当前 NPC 的任务列表。
3. 保持游戏画面和两块色条可见，运行 `uv run python -m bot.cli probe-quests --duration 20`。观察输出中的 `id`、`kind`、`state` 和 `progress`。
4. 执行 `/wagent quest off` 可关闭任务色条。

## 本地自动接任务测试

任务运行由脚本读取色条，向 `127.0.0.1:8090` 上的本地 StartLux 提交结构化状态和候选，取得阶段选择后执行既定路线或按键。模型调用在独立线程中运行，脚本持续检查色条、游戏焦点和停止条件。模型低置信度、超时、服务不可用或返回时状态过期都会停止本次执行。

1. 在游戏中打开 NPC 的未接任务对话，打开 WowAgent 的「任务与色条」页。只有一个可接任务时可留空任务输入，有多个时从列表选择一个。
2. 点「准备自动接任务」。插件把该任务的操作宏放到数字键 9 对应的主栏空位，并开启该任务色条。
3. 在「WoW 键鼠」菜单启用一次会话，然后双击 `macos/quest-accept.command`。启动器加载已安装的本地模型并打开主 App，脚本会自动切回游戏。保持 NPC 对话打开；系统未自动切换时，在 12 秒内手动切回。
4. 本地 StartLux 选择接取后，脚本按键推进任务详情和接受。只有任务日志实际变为已接取或可交付，才报告 `quest_accept_confirmed` 和 `completed`。

主 App 也可直接运行 `accept-quest`，此时需先按 [本地模型启动方法](startlux-poc.md) 启动服务。接任务测试结束后可关闭主控窗口，双击启动器会结束自己启动的模型进程。

2026-11-07 在正式服实测任务 90882「若是那豺狼来了」接取成功。地图为 2451，插件分类为 CAMPAIGN。脚本读取任务详情后请求本地 StartLux，模型选择接取，随后按数字键 9。任务色条从 PENDING / DETAIL 变为 ACTIVE / CLOSED，目标进度为 0/10。会话结果为 `completed`、`quests_accepted=1`，总用时 1.7 秒，其中模型请求约 784 ms。完整记录存于 `data/reports/quest-90882-live-accept.json`。目标击杀和交付继续单独验收。

准备失败时，面板会分别报告键位未绑定、格子占用、宏位已满、游戏拒绝创建宏或宏内容核对失败。宏位已满会显示角色和账号两类容量。角色宏位满而账号仍有空位时，插件会改用账号宏位。

## 执行一条已配置的任务线

支持 macOS 上的同地图地面任务。需要为任务提供实测的归一化航点和明确的交互键。路线适合对话、到达、有明确交互对象的单目标任务，以及配置了目标白名单的计数击杀任务。遇到多目标、计划外战斗、意外对话、多个可选奖励、色条失联或游戏失焦时，脚本会停止或等待后停止。

以下文件只是格式示例。将 ID、地图、航点和按键换成实测值。

```yaml
map_id: 2248
quests:
  - id: 12345
    dialog_key: "9"
    pickup: [[0.4000, 0.5000]]
    objective: [[0.4010, 0.5010]]
    objective_key: "8"
    turn_in: [[0.4020, 0.5020]]
```

- 每个任务的 `dialog_key` 绑定一条固定 ID 的游戏宏，例如 `/run WowAgent.QuestAction(12345)`。插件按键时再次核对对话里的任务 ID。一个对话列出多个已跟踪的候选时，先人工选中正确任务。
- 游戏里的 `f` 绑定交互键，`w` 用于前进。可用 `objective_key` 在目标航点触发明确的交互。纯到达目标可以省略该键。接取后直接变为可交付的任务可以省略目标路线。
- 在游戏里执行 `/wagent quest track 12345`。每个计划最多跟踪 8 个任务。重载会清除当前跟踪和操作状态。
- 接取、目标执行和交付阶段开始前，由本地 StartLux 选择继续或等待。路线跟随、战斗按键与进度核对使用现有脚本。主 App 运行前应先启动本地模型服务。
- 在主 App 中先运行 `run --module quest --quest-plan /path/to/plan.yaml --dry-run --max-duration 45`。干跑记录拟执行的步骤，需要手工推进才能看到后续阶段。实机执行前在键鼠 App 中启用会话，省略 `--dry-run`，启动后在 12 秒内回到游戏前台。可以用 `--quest-kinds campaign mainline` 只允许战役与主线候选。

## 计数击杀任务

在任务条目中用 `combat_targets` 列出可攻击的 NPC ID，并把 `objective` 的最后一个航点设为可以选中目标的位置。击杀任务使用 `combat_targets` 字段，对象交互任务使用 `objective_key` 字段。

- 游戏里把 `Tab` 绑定为选取最近敌人。在主动作栏绑定一键辅助或已生成的单技能宏，用 `/wagent combat arm 4 assist` 或 `/wagent combat arm 4 <技能ID>` 核对绑定。战斗键必须与计划中的对话和交互键分开。
- 到达目标点后，脚本在脱战时尝试选敌，只有确认目标 ID 在白名单且动作栏绑定有效时才输出。目标死亡或离开选中后，脚本等任务计数增加再继续。任务可交付时先等角色脱战，然后走交付路线。
- 如果缺少路线数据，可以在主 App 运行 `probe-quests --waypoints --duration 30`，走到任务接取点、战斗点和交付点并记录 `map=` 与 `waypoint=` 输出。开放世界的短距离地面路线需实测。当前实现会在目标点附近选敌，超出攻击范围或色条读不到目标时会停。

任务 `90882`「若是那豺狼来了」的可计数目标包括 `244669` 至 `244672`。配置时应按角色所在阵营和场景记录实际航点。[任务目标和 NPC 链接](https://www.wowhead.com/cn/quest=90882)
