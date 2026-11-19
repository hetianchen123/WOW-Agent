# 无限服练级与回归

## 用户要求

在 `codex/forever-leveling` 维护独立分支。使用当前无限服角色，由脚本完成跑图、交互、战斗、技能学习和加点。开发者操作插件一键准备、重载与授权启动入口，用户处理系统权限。练级目标是从当前 1 级到 15 级，以游戏的等级回执为准。

## 客户端与寻路

- 实机确认客户端位于 `_forever_`，版本 `1.60.1.70959`，界面版本 `16001`。自动准备入口应核对已加载插件版本、任务导航、输出键和无限服战斗条后才发输入。
- 室外继续使用地图坐标和位移反馈。北郡修道院地图 1429、世界 0 的任务 783 交付采用同版本客户端碰撞。原生 Z 经实测返回 0，校验使用同帧原生 XY 和网格连续支撑。
- 本机构建 `Map.db2` 无法通过严格读取。用已知的 Azeroth WDT 文件编号 775971 读取同一安装的无限服资源。网格仅存在本地忽略目录。退出网格前仍应保留原生对话、目标身份与真实任务交付回执。文件编号来源见 [WoW FileDataID 清单](https://github.com/wowdev/wow-listfile)。

```sh
.venv/bin/python -m bot.nav3d_cli extract-client \
  --client '/Volumes/Mac SSD1/Applications/World of Warcraft' \
  --product wowf --world-id 0 --wdt-file 775971 \
  --library data/nav3d/deps/CascLib-build/casc.framework/casc \
  --listfile data/nav3d/deps/community-listfile.csv \
  --phase quest:783:3 --roi 113 -8950 191 -8868 --max-climb .8 \
  --output data/nav3d/forever-northshire-abbey
```

## 职业成长

- 训练师坐标用于接近。选中训练师 GUID、训练窗口事件、技能 ID、可学状态和铜币充足都要核验。发出一次学习后等技能书新技能或同项服务变为已学，再发下一次。日志记录 `SPELL_LEARNED`、`TRAINER_DONE` 或 `ERROR`。初版按偶数等级检查，使用北郡和闪金镇的猎人训练师候选。 [无限服训练师原生界面](https://github.com/Gethe/wow-ui-source/blob/forever/Interface/AddOns/Blizzard_TrainerUI/Mainline/Blizzard_TrainerUI.lua)
- 猎人暂按兽王开局，从 10 级起先安排致命守护，计划 5 级后加一阶强化复活宠物。依有效配置、候选节点对应技能和可购买状态决定动作。存在其他暂存变更时等原生状态。点数以已生效等级上升且配置没有暂存变更为准，日志记录 `TALENT_SPENT`。 [无限服天赋 API](https://github.com/Gethe/wow-ui-source/blob/forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/SharedTraitsDocumentation.lua)
- 猎人输出宏只使用角色技能书已学技能，重复输出用 `!自动射击` 保持自动射击开启。升级刷新动作栏时先核对 WowAgent 宏的原内容，等待写入后重读宏体和 4 键。已有其他动作时保留。

## 从北郡接到闪金镇

- **代码已接入，实机待测**：原生任务 54 完成且满足最低等级后，候选任务包括 47 金砂交易、60 狗头人的蜡烛、40 鱼人的威胁。40 完成后接 35，走到艾尔文森林东桥向卫兵托马斯交付。开任务之前先核对 NPC 和对话任务编号。 [金砂交易](https://bindonforever.com/quest/47) · [狗头人的蜡烛](https://bindonforever.com/quest/60) · [鱼人的威胁](https://bindonforever.com/quest/40) · [进一步的忧虑](https://bindonforever.com/quest/35)
- **离线回归**：47 和 60 使用原生任务物品计数。只选择已配置的狗头人矿工与地穴工做候选，尸体拾取后看物品计数。拾取普通掉落后计数未变时继续找怪，每次计数上涨会重置连续无掉落预算。 [狗头人矿工](https://bindonforever.com/npc/40) · [狗头人地穴工](https://bindonforever.com/npc/475)
- **离线网格**：闪金镇旅店已从同版客户端构建。入口从治安官杜汉附近到威廉·匹斯特候选坐标规划 44.52 码路线。出口规划反向回到室外支撑，再交回室外寻路。进门、NPC 对话和交付仍待实机；本地网格复现沿用前述命令，替换为 `--phase quest:60:1 --roi -104 -9516 10 -9429 --output data/nav3d/forever-goldshire-inn`。

## 验证记录

- **实测**：`20261119T143418-9adceb58` 脚本接取 783，任务日志确认 `ACTIVE`。最初的直线接近撞墙。
- **实测**：`20261119T150642-37101238` 从碰撞点退回网格支撑，Nav3D 规划 39.5 码室内路线。途中自动镜头暂停导致采样不连续。已经关闭网格阶段的自动镜头动作。
- **实测**：`20261119T151623-ffaa9201` 脚本走完入口和门阶，进屋选中治安官，收到 783 的 `quest_done` 和 7 的 `quest_accept_confirmed`。门阶的原生水平位移与网格高度变化通过同一网格的短程连通校验。
- **实测**：`20261119T152258-4b9b0f14` 脚本沿网格从修道院主厅走到室外，收到 `nav3d_exit_reached`，然后交回室外导航。在距战斗图钉约 90 码提前搜怪，选到狗头人后无法接近。无限服缺原生任务区域时，将首次搜怪限制为图钉周围 28 码，等待复验。
- **实测**：`20261119T152622-861ae5bb` 收窄后走到目标区，自动射击击杀一只狗头人，收到 1/10 的任务计数。第二只近身后进入射击盲区并触发朝向失败。新版为已学猛禽一击生成独立的近战宏，仅在原生近战射程和按键均已核对的情况下切换到 5 键。 [暴雪猎人职业说明](https://wow.blizzard.cn/news/24301515/)
- **实测**：`20261119T153146-c883c88d` 加载 0.2.92 后，主控在近战射程内使用绑定已核对的 5 键。该目标死亡后任务计数保持 2/10。新版跳过被他人标记的目标，对已确认死亡但没有加计数的情况做有限换目标，不把死亡当作本方击杀。
- **代码已接入，学习与加点仍需实机**：Lua 回归覆盖训练师身份、训练对话选项、铜币不足、学习等待和天赋提交后的有效等级确认。训练师对话只接受客户端文件编号 132058 对应的选项，普通可接任务图标不会触发学习。
