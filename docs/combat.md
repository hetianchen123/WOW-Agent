# 战斗数据与操作手册

## 数据库覆盖

`data/combat.sqlite3` 按客户端构建号隔离。同一个技能 ID 在不同分支可以有不同内容。入库保留 Wago 原表字段、来源 URL 和原始 CSV 的 SHA-256。职业、种族、技能线、法术名与描述、效果、冷却、消耗、距离和天赋都可按版本查询。统计包含 NPC 和系统法术等客户端条目。

| 分支 | 构建号 | 法术名条目 | 天赋 | 输出资料 |
| --- | --- | ---: | --- | --- |
| 正式服 | `12.1.5.70783` | 416,286 | 11,878 条现代天赋节点 | 40 份游戏内一键辅助配置，693 条优先级步骤，3,116 条规则 |
| 时光服 | `3.80.2.70522` | 50,412 | 33 条旧式天赋树，909 条天赋条目 | 冰法、惩戒骑、敏锐贼、增强萨的官方历史改动摘要 |
| 无限服 | `1.60.1.70790` | 32,087 | 559 条现代节点，另保留客户端旧式天赋表 | 69 份社区模拟器优先级参考，含不同装备与天赋变体 |

时光服和无限服各有独立职业改动，客户端数据按自己的构建入库。时光服的公告记录用于了解改动背景。无限服的 APL 固定在提交 `8a956cdd4edb51560c02c3a330f93e4b9b2459f7`，存于独立的 `rotation_reference` 表，未验证是否适用上线构建。正式服的优先级由游戏内一键辅助执行。数据库的条件字段保留原样供查询。

[Wago 正式服一键辅助数据](https://wago.tools/db2/AssistedCombatStep/csv?build=12.1.5.70783) · [暴雪正式服更新说明](https://worldofwarcraft.blizzard.com/en-us/news/24246290/midnight-pre-expansion-content-update-notes) · [时光服阶段公告](https://wow.blizzard.cn/news/trr/20261010/43039_1315082.html) · [无限服上线公告](https://wow.blizzard.cn/news/24303862/index.html) · [无限服模拟器](https://github.com/gunba/wow-forever-sim) · [模拟器上游](https://github.com/wowsims/classic)

```bash
.venv/bin/python -m bot.cli combat coverage
.venv/bin/python -m bot.cli combat build-db
.venv/bin/python -m bot.cli combat class --edition titan --id 8
.venv/bin/python -m bot.cli combat ability --edition forever --id 133
.venv/bin/python -m bot.cli combat lookup --edition retail --table ChrRaces --id 1
.venv/bin/python -m bot.cli combat rotation --edition retail --spec 63
.venv/bin/python -m bot.cli combat rotation --edition titan
.venv/bin/python -m bot.cli combat rotation --edition forever
.venv/bin/python -m bot.cli combat rotation --edition forever --apl ui/balance_druid/apls/balance.apl.json
```

`build-db` 读取 `data/combat-sources/` 中已经下载的源文件，完成完整性检查后替换数据库。`class_mask_spells` 只列出技能线明确标注职业掩码的技能。角色实际可用技能以登录后的法术书快照为准。静态表保留导出的文本语言，宏使用客户端当前语言。

## 正式服专精与天赋方案

正式服构建 `12.1.5.70783` 的专精和预设可按名称查询。将客户端的 `ChrSpecialization` 与 `TraitTreeLoadout`、`TraitTreeLoadoutEntry` 关联后，有 40 个职业专精、76 条预设和 4,522 条预设明细。[客户端天赋预设表](https://wago.tools/db2/TraitTreeLoadout?build=12.1.5.70783)

WowAgent 的「战斗准备」从当前角色读取专精与已保存配置，用名称下拉选择。「天赋推荐」读取独立维护的 `config/retail-talent-builds.json`。首批覆盖满级猎人三个专精，各含四个 PvE 场景。生存猎人做单人内容可参考哨兵的地下堡方案。攻略版本和角色等级都匹配时才提供导入码，最终由游戏窗口确认。[野兽控制攻略](https://www.icy-veins.com/wow/beast-mastery-hunter-pve-dps-spec-builds-talents) · [射击攻略](https://www.icy-veins.com/wow/marksmanship-hunter-pve-dps-spec-builds-talents) · [生存攻略](https://www.icy-veins.com/wow/survival-hunter-pve-dps-spec-builds-talents)

```bash
.venv/bin/python -m bot.cli combat talents
.venv/bin/python -m bot.cli combat talents --spec 生存
.venv/bin/python -m bot.cli combat talents --export-addon addon/WowAgent/RetailTalents.lua
```

## 角色与动作配置

1. 重载插件后，在脱战状态下输入 `/wagent combat profile`，然后 `/reload` 写入当前账号的 `WTF/Account/<账号>/SavedVariables/WowAgent.lua`。
2. 用 `combat profile --savedvars <文件>` 查看职业、种族、构建号、专精、已选天赋及点数、已保存配置和法术书。现代客户端优先读取天赋节点，旧接口可用时读取旧式天赋页与点数。
3. `/wagent combat spec <专精ID>` 请求切换可用专精。`/wagent combat loadout <配置ID>` 请求切换当前专精的已保存天赋，支持旧天赋组 API 的客户端也可传组号。切换后重新采集角色快照和确认动作。
4. `/wagent combat macro <技能ID>` 用角色已学技能的本地名称创建专用宏 `WAS<技能ID>`。将宏拖到主动作栏并绑定数字键 1 至 9，使用 `/wagent combat arm <键> <技能ID>` 确认。正式服可把游戏内的一键辅助拖到主栏，然后用 `/wagent combat arm <键> assist`。

## 脱战准备和单项验收

在「战斗准备」或「动作栏布局」点击「清空主栏」，可清空当前主动作栏页的 12 格并保留原动作的恢复备份。插件沿用当前主栏的按键，可按格子重新摆放技能。命令入口为 `/wagent combat layout clear`。游戏拒绝清空时会尝试回滚，备份恢复入口为「恢复备份」。已有布局备份时，先恢复，或在动作栏布局页点「采用布局」后继续。

更新插件后在游戏里执行 `/reload`。以下命令都在聊天框输入。宏优先建在角色专用宏页，角色宏位已满时使用可用的账号宏位。配置命令会放入空闲主栏格子。遇到同名但内容不同的宏时，插件会自动创建或复用带编号的宏，例如 `WASAssist1`，再继续放置和核对按键。内容核对兼容末尾换行及换行格式差异。已有宏的内容保留，动作栏备份仍可恢复。

施法和物品宏的动作栏编号可能是法术或物品编号。0.2.7 根据动作栏文本查回宏，再核对宏内容。账号和角色页存在同名不同内容的宏时会拒绝确认。该读取逻辑同时用于战斗绑定和动作栏备份。[接口返回值问题的原始报告](https://github.com/Stanzilla/WoWUIBugs/issues/495)

| 功能 | 命令 | 游戏内确认 |
| --- | --- | --- |
| 查看准备状态 | `/wagent combat prep` | 查看当前专精、保存的天赋、装备套装、生命、资源与上马状态 |
| 正式服一键输出 | `/wagent combat macro assist` | 把 `WASAssist` 拖到主动作栏，在训练假人上按一次。可用 `/wagent combat arm 4 assist` 核对数字键 4，也支持绑定原生一键辅助按钮 |
| 地面上马 | `/wagent combat mounts` 后用 `/wagent combat macro mount <坐骑ID>` | 先把测试坐骑设为收藏，找到 ID，拖出 `WAM<ID>` 并在户外脱战试按 |
| 飞行坐骑 | `/wagent combat macro fly <坐骑ID>` | 选已收集的飞行坐骑。在允许飞行的户外试按 `WAF<ID>`，上马后手动起飞确认 |
| 切换专精与天赋 | `/wagent combat spec <专精ID>`，`/wagent combat loadout <配置ID>` | 用 `prep` 查看当前专精下的配置 ID，切换后再核对 |
| 换装 | `/wagent combat gear`，`/wagent combat gear <套装ID>` | 先用游戏装备管理器保存套装。换装后重查，确认目标套装显示「已穿戴」 |
| 补状态 | `/wagent combat macro buff <技能ID>`，`/wagent combat macro supply <物品ID>` | 用已学的自用技能生成 `WAB<ID>`，用背包中已有的食物、饮品等生成 `WAI<ID>`，按键后检查增益或资源变化 |

这组命令由玩家分别触发。换装按已保存的套装进行，装备缺件或被锁定时会提示。飞行宏会核对坐骑是否已收集，并在允许飞行的区域尝试上马。坐骑 ID 应由玩家从已知可飞行的收藏中选择。正式服的一键辅助以游戏功能为基础，旧客户端可继续使用单技能宏。

[暴雪一键辅助说明](https://worldofwarcraft.blizzard.com/es-mx/news/24263354) · [坐骑接口](https://github.com/Gethe/wow-ui-source/blob/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/MountJournalDocumentation.lua) · [装备套装接口](https://github.com/Gethe/wow-ui-source/blob/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/EquipmentManagerDocumentation.lua)

需要一次完成建宏和放置时，在脱战后的普通动作栏执行 `/wagent combat setup assist`。它会优先复用已就位的一键辅助，或在数字键 1–9 对应的主动作栏空位放置宏，再核对绑定。可以在命令末尾指定数字键，如 `/wagent combat setup assist 6`。已有动作的格子会保留。

- `/wagent combat setup mount <坐骑ID> [数字键]` 也可将 `mount` 替换成 `fly`、`buff`、`supply` 或 `spell`，自动创建并放置对应的宏。
- `/wagent combat setup loadout <配置ID> [数字键]` 会切换当前专精下已保存的天赋。客户端确认后，插件再配置一键输出。跨专精时先用 `/wagent combat spec <专精ID>` 切换，再用 `prep` 查该专精下的配置 ID。等待期间使用 `/wagent combat off` 可取消后续动作栏配置。
- 如果客户端拒绝自动放置，已创建的宏可以从 `/macro` 面板拖到动作栏。

## 重排整套动作栏

安装布局模块后需要完全退出并重开游戏。先脱战，用 `plan` 预览范围和要放的动作，再用 `apply` 执行。计划会按角色保留，可以在确认采用或恢复上一次布局后再次运行。

```text
/wagent combat layout plan main keep 1=assist
/wagent combat layout apply
/wagent combat layout restore
```

这个最小例子会在主栏第 1 格放一键辅助，另外 11 格留空。`main` 处理当前主栏页，`visible` 还处理当前可见的标准副栏。副栏格子用 `bl.1`、`br.1`、`r.1`、`l.1`、`b5.1`、`b6.1` 或 `b7.1` 指定。格号跟随现有按键绑定。把 `keep` 换成当前专精下保存的天赋配置 ID，可以先切天赋再重排。

动作可以写成 `assist`、`spell:<技能ID>` 或 `macro:<现有宏编号>`。也可以用 `cast:<技能ID>`、`mount:<坐骑ID>`、`fly:<坐骑ID>`、`buff:<技能ID>` 和 `supply:<物品ID>` 生成对应的宏。预览列出的范围内，未指定的格子会留空。

执行时先确认天赋，再备份即将改写的栏位并试拿取原动作与新动作。遇到无法恢复的动作会在清空前停止。若修改中途失败，插件会尝试恢复备份。用 `restore` 恢复栏位和之前的天赋，用 `accept` 确认采用新布局并删除本次备份。等待客户端切天赋时可用 `cancel` 取消后续步骤。备份在游戏重载或正常退出时写入磁盘。

## 试跑边界

显示器位置改变后，运行 `calibrate-channel` 扫描并保存主色条坐标及显示器编号。游戏内输入 `/wagent combat scan` 显示战斗色条后，再用 `combat scan` 核对客户端、角色、目标和绑定。macOS 需要为发起测试的终端或运行器启用屏幕录制，实机按键还需要辅助功能权限。

`combat fight --target <NPC ID>` 默认模拟输入。开始前必须手动选中目标并确认按键。执行器逐帧核对已确认的主栏动作、目标和角色状态。遇到目标切换、资料失效、急停、超时或实机失焦时结束。正式服可使用游戏内一键辅助，时光服和无限服当前只支持手动挑选单个技能宏进行试跑。包含受限目标信息的场景会停止执行。

已验证数据库完整性和 Lua、像素编码、Python 解码及执行状态机。实机角色识别、专精切换和施法仍需在对应客户端验收。任务接战和大秘境留待实机验收后接入。
