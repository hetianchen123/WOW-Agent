# 魔兽世界正式服插件 API 实用参考

核对日期 **2026-11-14**。本机 `.build.info` 中 `wow` 的激活构建为 **12.1.5.70783**，对应正式服 `## Interface: 120105`。暴雪客户端界面源码的公开镜像也标记为同一构建。范围是正式服游戏内 Lua API 与原生操作入口，覆盖 WowAgent 的任务、战斗、移动和镜头。 [国服版本公告][release] · [同构建 FrameXML 快照][build] · [镜像版本文件][version]

## 可用性怎么判定

| 标记 | 含义 |
| --- | --- |
| **可读** | 当前构建有读取入口。仍需符合单位、地图、页面、缓存等前置条件，并检查实际返回值。 |
| **条件读** | 接口存在，依赖场景、单位或具体字段，可能返回 `nil`、保密值或报错。逐字段判断。 |
| **受保护** | 游戏操作受安全执行规则限制，使用游戏原生键位、玩家触发的受支持宏或预设安全按钮。改绑定、宏与安全按钮属性还需单独检查战斗锁定。 |
| **未核实** | 只有符号、旧资料、项目调用或模拟覆盖，尚缺当前构建实际行为的证据。 |

`InCombatLockdown()` 判断受保护 UI 能否改动。`C_RestrictedActions.GetAddOnRestrictionState(kind)`、`IsAddOnRestrictionActive(kind)` 和 `ADDON_RESTRICTION_STATE_CHANGED` 另行报告具体限制。团本遭遇战、大秘境、PvP 与野外战斗的限制可能不同，脱战时副本限制也可能还在。`SecretArguments` 描述能否传入保密参数，`SecretWhen…`、`SecretReturns` 等才描述输出。`HasRestrictions` 需要结合该函数或事件的规则看待。 [暴雪战斗插件说明][policy] · [受限操作定义][restricted]

读取时先检查接口是否存在，再判断调用成功、值是否可访问、类型是否符合预期。表先检查 `canaccesstable`，取出的每个字段仍要检查 `issecretvalue` 或 `canaccessvalue`。`pcall` 只接住错误。保密值保留为未知，不能进入比较、算术、字符串拼接、SavedVariables 或色条传输。有效的 `false` 和 `0` 要保留。只有 API 确认当前样本没有结果时才记作缺失。 [Lua 保密值检测定义][secrets]

## 获取目标、附近单位和互动对象

| 用途 | API 或事件 | 能力与边界 |
| --- | --- | --- |
| 当前目标与辅助单位 | `UnitExists(unit)`、`UnitIsDead(unit)`、`UnitCanAttack("player", unit)`、`PLAYER_TARGET_CHANGED` | **可读**。常用单位令牌有 `target`、`focus`、`mouseover`、`pet`、`pettarget`、`targettarget`、`partyN`、`raidN`、`bossN`。令牌会换绑，事件到来后重读目标。可攻击状态只表示关系，射程、视线和朝向分别核对。 |
| 单位身份与匹配 | `UnitGUID`、`UnitName`、`UnitIsUnit`、`UnitClassification`、`UnitLevel`、`UNIT_TARGET` | **条件读**。GUID、名称、单位比较各有独立保密条件。野外拿到公开 `Creature-…` GUID 时可识别 NPC 类型，完整 GUID 才区分同类型个体。`UnitIsUnit` 受限时仅记录身份未知。 |
| 附近可见敌人 | `C_NamePlate.GetNamePlates()`、`GetNamePlateForUnit(unit)`、`NAME_PLATE_UNIT_ADDED/REMOVED` | **条件读**。只覆盖当前由客户端提供姓名板的单位。保存令牌时记录增删与新帧，有复用时重读。跳过禁止访问的框体，框体坐标或单位身份不可读时标未知。姓名板中心是 UI 坐标，随镜头、堆叠和缩放变化。 |
| 鼠标下的单位与物件 | `UnitGUID("mouseover")`、`C_TooltipInfo.GetUnit(unit)`、`GetWorldCursor()`、`GetCursorPosition()` | **条件读**。先从姓名板、已知物件或画面得到候选，再验证当前鼠标下对象、提示字段和坐标。`mouseover` 主要指单位，普通场景物件通常没有单位令牌。坐标还需处理 `UIParent:GetEffectiveScale()`、窗口与屏幕坐标系。 |
| 地图上的候选物件 | `C_VignetteInfo.GetVignettes()`、`GetVignetteInfo(guid)`、`GetVignettePosition(guid, uiMapID)` | **条件读**。只覆盖游戏提供的 vignette 标记。可以结合公开名称或 `objectGUID` 筛选，实际对象位置与可互动距离仍需现场复核。没有通用接口枚举附近所有怪和任意世界物件。 |
| 距离与视线 | `UnitDistanceSquared(unit)`、`UnitInRange(unit)`、`CheckInteractDistance(unit, index)`、实际技能射程 | **条件读**。`UnitDistanceSquared` 要求公开的 `checkedDistance=true`。`UnitInRange` 要核对 `checkedRange`，主要看队伍单位。`CheckInteractDistance` 是指定互动类型的阈值测试。`UnitIsVisible` 也无法证明从角色到目标的施法视线。不要把 `UnitPosition("target")` 当作任意敌人坐标接口。 |
| 选中、切换或互动 | Tab、点击原生目标框或姓名板、互动键、支持的宏和 `SecureActionButtonTemplate` | **受保护**。`TargetUnit`、`TargetNearestEnemy`、`InteractUnit` 等动作需要使用合法的安全入口，普通 Lua 或事件回调不能直接代为操作。切目标后重读身份和攻击状态，互动后看对话、拾取或任务事件。 |

[单位 API 与事件][unit] · [姓名板 API][nameplate] · [鼠标提示 API][tooltip] · [地图标记 API][vignette] · [暴雪安全按钮源码][secure]

## 获取任务、接交任务和拾取

| 用途 | API 或事件 | 能力与边界 |
| --- | --- | --- |
| 已接任务 | `C_QuestLog.GetNumQuestLogEntries()`、`GetInfo(index)`、`GetLogIndexForQuestID(questID)`、`IsOnQuest(questID)` | **可读**。用第一个返回值 `numShownEntries` 遍历，跳过 `isHeader`，以 `questID` 做主键。日志索引随接交与排序变化。可从 `GetTitleForQuestID` 读标题。 |
| 目标和任务状态 | `C_QuestLog.GetQuestObjectives(questID)`、`ReadyForTurnIn(questID)`、`IsQuestFlaggedCompleted(questID)` | **可读**。目标使用 `text/type/finished/numFulfilled/numRequired` 等实际字段。区分某项目标达成、可交付、已交付。重复任务仍核对当前轮次、在日志状态与事件。 |
| 异步加载和分类 | `C_QuestLog.RequestLoadQuestByID`、`QUEST_DATA_LOAD_RESULT`、`C_QuestInfoSystem.GetQuestClassification`、任务日志 `campaignID` | **可读**。用相同 ID 核对加载结果。战役归类、游戏标签与项目过滤规则分别记录。任务显示文本有本地化差异。 |
| 附近可接与任务线 | `C_QuestLine.RequestQuestLinesForMap`、`GetAvailableQuestLines(uiMapID)`、`GetQuestLineInfo(questID, uiMapID?, displayableOnly?)`、`GetQuestLineQuests(questLineID)` | **条件读**。用于收集地图任务线候选，受地图、缓存、前置、相位影响。到现场后用 NPC 对话列表或游戏确认状态验证是否可接。 |
| 当前 NPC 的任务 | `C_GossipInfo.GetAvailableQuests()`、`GetActiveQuests()`、`GetOptions()`、`GOSSIP_SHOW`、`QUEST_GREETING` | **可读**。数据只属于当前对话。有些 NPC 直接开 `QUEST_DETAIL` 等任务页，按面板状态读取 `GetQuestID()`。接取前再核对 NPC、当前页和任务 ID。 |
| 路标与追踪 | `C_QuestLog.GetNextWaypoint(questID)`、`GetNextWaypointForMap(questID, uiMapID)`、`GetQuestsOnMap(uiMapID)`、`C_SuperTrack.GetSuperTrackedQuestID()` | **条件读**。`GetNextWaypoint` 给地图 ID 和坐标，`ForMap` 给指定地图上的坐标。图标或寻路箭头可指区域、入口或当前步骤。按返回的地图及楼层使用，通行路径另验。追踪状态只说明 UI 当前在跟踪什么。 |
| 世界任务与区域目标 | `C_TaskQuest.GetQuestsOnMap(uiMapID)`、`GetQuestInfoByQuestID(questID)`、`GetQuestTimeLeftSeconds(questID)`；原生 `QuestPOI` 范围查询 | **条件读**。世界任务和地图任务标记是各自的来源。现有 `QuestArea.lua` 调用 `DrawBlob` 与 `UpdateMouseOverTooltip` 做任务区内点查询，依赖 FrameXML 的控件用法，已模拟验证、待实机确认。查询给范围命中，物件坐标和地形另验。 |
| 接取、提交、领奖 | `C_GossipInfo.SelectAvailableQuest(questID)`、`SelectActiveQuest(questID)`、`AcceptQuest()`、`CompleteQuest()`、`GetQuestReward(choice)` | **依赖界面状态**。这些是当前 NPC/任务页流程入口，选择可接项、打开详情、接取、交付与领取奖励分别核对。现有项目使用已核对的任务按钮和游戏流程。以 `QUEST_ACCEPTED`、`QUEST_TURNED_IN`、`QUEST_LOG_UPDATE` 及相同任务 ID 复查，存在多选奖励时按游戏界面选择。 |
| 拾取、道具和背包 | `LOOT_READY/OPENED/SLOT_CLEARED/CLOSED`、`GetNumLootItems()`、`C_Item.GetItemCount()`、`C_Container.GetContainerItemInfo()` | **条件读**。拾取槽清除只证明该槽变化。任务物品以目标计数、背包变化或明确的游戏回执确认。拾取和使用任务物品走游戏互动入口，施法事件只能确认技能施放。 |

[任务日志][quest] · [任务线][questline] · [NPC 对话][gossip] · [世界任务][task] · [任务区域原生 UI][blob] · [拾取事件][loot]

## 战斗 API 还能用到哪里

| 类别 | 当前入口 | 用法和限制 |
| --- | --- | --- |
| 进战和脱战 | `UnitAffectingCombat("player")`、`PLAYER_REGEN_DISABLED/ENABLED`、`InCombatLockdown()` | **可读**。分别表示单位战斗状态、玩家战斗切换通知、受保护 UI 状态。目标消失或施法结束时仍需单独确认脱战。 |
| 技能、专精与动作栏 | `C_Spell.GetSpellInfo/GetSpellName`、`C_SpellBook.IsSpellKnown/GetSpellBookItemInfo`、`C_ClassTalents.GetActiveConfigID`、`C_Traits.GetConfigInfo`、`GetActionInfo(slot)`、`GetBindingAction(key, true)` | **可读**。技能存在和角色学会是两件事。核对当前实际动作栏页、按键覆盖、宏内容，处理骑乘、载具和形态换页。正式服优先用当前文档的 `C_Spell.*` 返回结构。 |
| 射程和可施放提示 | `C_Spell.IsSpellInRange(spell, unit)`、`C_ActionBar.IsActionInRange(slot, unit)`、`C_Spell.IsSpellUsable(spell)` | **条件读**。射程 `true/false/nil` 分别表示可达、超距、无法判定。`IsSpellUsable` 主要反映已学、资源等条件。仍单独检查冷却、角色朝向、视线、移动限制，并以施法反馈为准。 |
| 冷却与充能 | `C_Spell.GetSpellCooldown(spell)`、`GetSpellCharges(spell)`、`GetSpellCooldownDuration(spell, ignoreGCD)` | **条件读**。前两者返回结构，并有 `SecretWhenCooldownsRestricted`。充能不存在也可能返回 `nil`。持续时间对象可供支持的 UI 显示，能创建对象并不能证明内部数字可参与判断。具体技能可能有豁免。 |
| 生命、资源与仇恨 | `UnitHealth/UnitHealthMax/UnitHealthPercent`、`UnitPower/UnitPowerMax`、`UnitThreatSituation`、`UnitDetailedThreatSituation` | **条件读**。生命百分比同样可能是保密值。玩家、宠物、敌人、不同资源类型与最大值分别检查。目标的目标、玩家仇恨和宠物参与可用时结合判断，仇恨未知不能当成未参战。 |
| 读条、打断与光环 | `UnitCastingInfo`、`UnitChannelInfo`、`UNIT_SPELLCAST_*`、`C_UnitAuras.GetAuraDataByIndex/GetPlayerAuraBySpellID`、`UNIT_AURA` | **条件读**。读条的技能 ID、时间和打断状态，以及事件参数、光环字段均须独立检查。战斗条关联玩家的 `UNIT_SPELLCAST_SUCCEEDED`、公开法术 ID 和当前目标变更序列。技能成功后仍需单独核对伤害、击杀和任务进度。按 ID 查玩家光环有非保密光环要求，空值未必代表 Buff 不存在。读条消失也可能是施法完成或换目标，不能单独确认打断。 |
| 正式服一键辅助 | `C_AssistedCombat.IsAvailable()`、`GetActionSpell()`、`GetNextCastSpell(checkForVisibleButton)`、`GetRotationSpells()` | **可读**，上栏后的施放 **受保护**。`GetActionSpell` 用于识别一键辅助按钮，`GetNextCastSpell` 给出当前辅助推荐或空值，`GetRotationSpells` 提供参与轮转的技能列表。原生一键辅助由玩家触发，结果仍以游戏反馈确认。 |
| 旧战斗日志 | `COMBAT_LOG_EVENT_UNFILTERED`、`COMBAT_LOG_EVENT`、`CombatLogGetCurrentEventInfo` | **受限或旧接口**。两个事件在当前定义中明确有 `HasRestrictions`，可用 `C_CombatLog.IsCombatLogRestricted()` 查询限制。插件设计不要把旧 CLEU 原始流或旧读取函数当作稳定依赖。读得到落盘战斗日志与游戏内插件能收到原始明细是不同能力。 |
| 团本与大秘境 | `ENCOUNTER_START/END`、`C_EncounterTimeline`、`C_DamageMeter`、`C_ChallengeMode.IsChallengeModeActive/GetActiveChallengeMapID/GetChallengeCompletionInfo`、`C_ScenarioInfo.GetCriteriaInfo/GetScenarioStepInfo` | **按用途区分**。副本阶段、钥石活动与结算可按公开信息读取。场景目标、敌方部队进度以该副本实际返回为准。首领时间轴的事件信息可能保密，伤害统计会话的多项读取在战斗中标为保密。可以使用官方支持的界面展示，不能依赖这些接口还原逐条战斗事件。 |
| 施法、打断、移动 | 玩家触发的动作栏、原生键位和游戏支持的安全宏 | **受保护**。`CastSpellByName`、`CastSpellByID`、`UseAction`、`MoveForwardStart` 等保留名字也不能由普通 Lua 计时器自由执行。按键、施法成功、命中、击杀是独立反馈。 |

[技能][spell] · [动作栏][actionbar] · [光环][aura] · [一键辅助][assist] · [战斗日志][cleu] · [首领时间轴][timeline] · [伤害统计][meter] · [钥石][challenge] · [场景目标][scenario]

## 跑图、坐标与角色状态

| 用途 | 当前入口 | 返回和边界 |
| --- | --- | --- |
| 角色所在地图 | `C_Map.GetBestMapForUnit("player")`、`C_Minimap.GetUiMapID()`、`C_Map.GetMapInfo(uiMapID)` | **可读**。玩家地图、小地图和当前打开的世界地图可能不同。换区域或楼层后重读，室内外名称不能替代地图 ID。 |
| 位置和比例 | `C_Map.GetPlayerMapPosition(uiMapID, "player")`、`GetMapWorldSize(uiMapID)`、`GetWorldPosFromMapPos`、`GetMapPosFromWorldPos` | **条件读**。`GetPlayerMapPosition` 给 0–1 的地图坐标向量，适用于玩家和小队成员，可能没有结果。转换前核对大陆 ID 和地图层，二维映射无法解释地形、建筑入口或真实高度。 |
| 世界位置和朝向 | `UnitPosition("player")`、`GetPlayerFacing()`、`GetUnitSpeed("player")` | **条件读**。分别是角色位置、角色水平朝向弧度、速度。`UnitPosition` 的第四返回值是实例/世界地图标识，不能直接和 `uiMapID` 比。目标位置和精确距离受可用范围限制。 |
| 小地图 | `C_Minimap.GetViewRadius()`、`IsRotateMinimapIgnored()`、`C_CVar.GetCVar("rotateMinimap")`、`Minimap` 框体尺寸和缩放 | **条件读**。半径与 UI 几何可用于核对方向和比例。旋转模式、角色箭头、地图标记和实际通路分别判断。 |
| 坐骑、驭空和载具 | `IsMounted()`、`IsFlying()`、`IsFalling()`、`IsSwimming()`、`C_PlayerInfo.GetGlidingInfo()`、`UnitInVehicle`、`UnitInVehicleControlSeat`、`C_MountJournal` | **可读或条件读**。`GetGlidingInfo()` 返回滑翔、可滑翔和前向速度。驭空技能、充能及载具动作按当前动作栏复查。用新帧的飞行和下落状态确认着地。 |
| 跑尸与寻路 | `C_DeathInfo.GetCorpseMapPosition(uiMapID)`、`UnitIsDeadOrGhost("player")` | **条件读**。尸体点可给方向。公开插件 API 未核实有通用的地形射线、碰撞、寻路网格或世界坐标投影接口。通过可见路面、入口、楼层和实际位移验证路线。 |

[地图][map] · [小地图][minimap] · [玩家飞行状态][gliding] · [单位位置与速度][unit]

## 世界镜头 API

| 能力 | 入口 | 读数、限制与验证 |
| --- | --- | --- |
| 读当前缩放距离 | `GetCameraZoom()` | 本机客户端符号与项目调用已核对。读取镜头缩放距离，用于判断第三人称和镜头是否被挤近。距离无法给出镜头俯角或水平朝向。转场、碰撞、坐骑和载具可能改变实际画面。 |
| 相对缩放、持续推拉 | `CameraZoomIn(increment)`、`CameraZoomOut(increment)`；`MoveViewInStart/Stop`、`MoveViewOutStart/Stop` | 前两项是相对操作，用前后 `GetCameraZoom()` 核对。持续推拉的起止符号在本机可见，参数和当前场景行为待单测。每次开始都安排成对停止。引擎限位、墙面、缓动和用户输入可能影响实际缩放。 |
| 保存、恢复及切换视角 | `SaveView(index)`、`SetView(index)`、`ResetView(index)`、`NextView()`、`PrevView()`；原生视角保存/切换绑定 | 可使用原生视角槽。项目让玩家手动调好后保存槽 **5**，未保存时加载槽 **2**。用 `cameraView` 查槽位、`GetCameraZoom()` 看距离，再用画面确认。`ResetView` 会重置预设，槽位归属及跨场景效果应单独核验。 |
| 相对水平旋转和俯仰 | `MoveViewLeftStart/Stop`、`MoveViewRightStart/Stop`、`MoveViewUpStart/Stop`、`MoveViewDownStart/Stop`；`FlipCameraYaw(degrees)` | `MoveView` 是持续相对移动，速度/时长与 `cameraYawMoveSpeed`、`cameraPitchMoveSpeed` 有关。`FlipCameraYaw` 可作为单次相对水平旋转的候选，当前项目无实机验收。用稳定画面核对方向和幅度，停止、失焦或状态切换时结束相对动作。 |
| 鼠标观察、转身与移动 | 游戏绑定的左键拖镜头、右键观察/转向；`IsMouselooking()`，`MouselookStart/Stop`、`CameraOrSelectOrMoveStart/Stop`、`TurnOrActionStart/Stop` | 这些符号出现在本机客户端，动作入口受绑定、鼠标观察模式和安全上下文影响。采用玩家原生操作语义。只转镜头与转动角色分别观察，`GetPlayerFacing()` 只用来核对后者。 |
| 视场、跟随和平滑 | `C_CVar.GetCVar/GetCVarInfo/SetCVar`；`cameraFov`、`cameraDistanceMaxZoomFactor`、`cameraZoomSpeed`、`cameraSmoothStyle`、`cameraViewBlendStyle`、`cameraTerrainTilt`；`GetCameraFOVDefaults()` | `GetCameraFOVDefaults` 返回默认值及玩家设定范围。各项分别影响视场、拉远上限、变焦速度、跟随、切换混合或地形倾斜，不能当实时角度读取。设置受公开、只读与安全 CVar 标记限制，读取现值和允许范围，改后读回，并保留原值。 |
| 动态镜头和偏肩 | `test_cameraOverShoulder`、`test_cameraDynamicPitch`、`test_cameraTargetFocusEnemyEnable`、`test_cameraTargetFocusInteractEnable` | 实验性 CVar，本机符号和插件作者的用法可查。用于偏肩、动态俯仰或聚焦效果。设置可能受游戏选项、当前角色状态或版本限制。效果以新画面为准，常规任务取景仍以可控预设为基线。 |
| 绝对世界镜头角度、位置与屏幕投影 | 当前未核实有通用的公开 API 可读取或设定实时绝对 pitch/yaw，或投影任意世界坐标 | `GetCameraFacing/GetCameraPosition` 在生成文档中属于 `SimpleModel` 的模型控件方法，`GetUICameraInfo` 读 UI 镜头资源。它们不能用于测量场景镜头。`cameraSavedPitch` 等符号的存在也无法证明是可读写的实时角度。 |

[暴雪镜头与 FOV 定义][camera] · [模型控件镜头][model] · [CVar 定义][cvar] · [DynamicCam 作者实现][dynamic] · [LibCamera 作者实现][libcamera]

### WowAgent 镜头用法与实机证据

- **跑图基准**。在安全地面下马脱战，先拉到第三人称，从角色后方调到用户要求的约 20° 浅俯视。当前插件的「保存任务视角」把玩家目测确认的画面保存到槽 5。实际角度由画面判断。
- **按场景调整**。地面跑图同时留脚下、路面和下一个路口。飞行时保留天际线与前方净空，找落点时短时向下看。狭窄处适度缩近并看楼梯或门框，重新到开阔地后恢复预设。远程副本保留目标、脚下和下一站位，近战保留怪的正面、脚下和侧向通路。 [跑图场景](../skills/wow-playbook/references/navigation.md#按跑图场景选镜头) · [战斗镜头](../skills/wow-playbook/references/combat-positioning.md#战斗镜头怎么选)
- **项目实现，0.2.61，待实测**。`QuestCamera.lua` 在安全地面恢复预设，并对保存和默认预设统一验证距离。新保存的基准允许有界相对俯仰，单步约 6° 只是估算。战斗/空中/鬼魂只缩放；游泳、载具、剧情及关键操作让权。导航 v15 报告缩放、场景、请求号和明确结果，主控持有 F8 到完成或取消。受限时不无限重试，地面前后截图供画面验收。实际角度、视野和战斗/飞行调用仍需本机验证，详见[实现对照](camera-adjustment-plan.md#0261-实现对照)。
- **本机日志与玩家反馈**。11 月 13 日旧流程报完成时，玩家看到的是垂直俯视。随后日志记录 `quest_camera_view_reported`，玩家反馈预设偏平。现有日志确认过原生槽位回报，约 20°、相对微调幅度和不同场景复位仍需画面验收。 [已有镜头实机记录](../skills/wow-playbook/references/navigation.md#小地图和画面)

## 本机证据与后续复核

- **已有任务日志**。接取 `90888/90895/90893` 有任务状态变化。拾回 `244677` 后任务计数增长。驭空升空有玩家施法事件和充能 `6 → 5` 的成功记录，也有未获回执的尝试。它们覆盖这些具体任务和角色条件。
- **已知数据缺口**。11 月 13 日野外自卫记录中，敌方生命值不可读。目标选取、攻击发键、脱战确认要分别核对。同一构建下团本、大秘境和 PvP 仍需逐场景确认目标身份、仇恨、读条、光环、冷却及事件字段。
- **复核流程**。在游戏里先用 `/dump GetBuildInfo()` 核对构建与接口号，用 `/api` 查当前客户端生成文档，用 `/etrace` 看事件。针对同一字段分别记录野外脱战、野外战斗、团本进场和遭遇战、大秘境计时前后、PvP。每次记录单位令牌、限制状态、调用结果类型、字段公开性与实际反馈。镜头在静止、跑动、飞行、窄道、复活和载具分别对照槽位、缩放、角色朝向与前后画面。

以下是可手动输入的只读探针。目标探针先手动选中一个单位。最后一条的 `90893` 替换为自己当前日志里的任务 ID。`public` 表示返回了非保密值，类型和具体语义仍以接口定义为准。

```text
/dump GetBuildInfo()
/dump GetCameraZoom(), C_CVar.GetCVar("cameraView"), GetPlayerFacing()
/run local ok,v=pcall(UnitHealth,"target"); print(ok and (issecretvalue(v) and "secret" or (v==nil and "nil" or "public")) or "error")
/run local ok,v=pcall(UnitGUID,"target"); print(ok and (issecretvalue(v) and "secret" or (v==nil and "nil" or "public")) or "error")
/dump C_QuestLog.GetQuestObjectives(90893)
```

项目代码见 [Combat.lua](../addon/WowAgent/Combat.lua)、[Quest.lua](../addon/WowAgent/Quest.lua)、[QuestArea.lua](../addon/WowAgent/QuestArea.lua)、[QuestCamera.lua](../addon/WowAgent/QuestCamera.lua) 和 [Recovery.lua](../addon/WowAgent/Recovery.lua)。本机运行证据来自 `data/logs/bot.log` 与 skill 中的实机记录。本次核对使用本机构建清单、客户端符号、既有运行日志和暴雪界面源码，未新增游戏内实测。

[release]: https://wow.blizzard.cn/news/24307306/index.html
[build]: https://github.com/Gethe/wow-ui-source/commit/297d7aa
[version]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/version.txt
[policy]: https://worldofwarcraft.blizzard.com/de-de/news/24244638/how-midnights-upcoming-game-changes-will-impact-combat-addons
[restricted]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/RestrictedActionsDocumentation.lua
[secrets]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/FrameScriptDocumentation.lua
[unit]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/UnitDocumentation.lua
[nameplate]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/NamePlateDocumentation.lua
[tooltip]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/TooltipInfoDocumentation.lua
[vignette]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/VignetteInfoDocumentation.lua
[secure]: https://github.com/Gethe/wow-ui-source/blob/live/Interface/AddOns/Blizzard_FrameXML/SecureTemplates.lua
[quest]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/QuestLogDocumentation.lua
[questline]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/QuestLineInfoDocumentation.lua
[gossip]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/GossipInfoDocumentation.lua
[task]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/QuestTaskInfoDocumentation.lua
[blob]: https://github.com/Gethe/wow-ui-source/blob/live/Interface/AddOns/Blizzard_SharedMapDataProviders/QuestBlobDataProvider.lua
[loot]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/LootDocumentation.lua
[spell]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/SpellDocumentation.lua
[actionbar]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/ActionBarFrameDocumentation.lua
[aura]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/UnitAuraDocumentation.lua
[assist]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/AssistedCombatDocumentation.lua
[cleu]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/CombatLogDocumentation.lua
[timeline]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/EncounterTimelineDocumentation.lua
[meter]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/DamageMeterDocumentation.lua
[challenge]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/ChallengeModeInfoDocumentation.lua
[scenario]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/ScenarioInfoDocumentation.lua
[map]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/MapDocumentation.lua
[minimap]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/MinimapDocumentation.lua
[gliding]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/PlayerInfoDocumentation.lua
[camera]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/CameraDocumentation.lua
[model]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/SimpleModelAPIDocumentation.lua
[cvar]: https://raw.githubusercontent.com/Gethe/wow-ui-source/live/Interface/AddOns/Blizzard_APIDocumentationGenerated/CVarDocumentation.lua
[dynamic]: https://github.com/Mpstark/DynamicCam/blob/master/Core.lua
[libcamera]: https://github.com/Mpstark/LibCamera
