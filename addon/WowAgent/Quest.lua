-- Quest telemetry and one-step dialogue action. Protocol mirrored in quest_channel.py.
local K = Enum and Enum.QuestClassification
if not K or not C_QuestInfoSystem or not C_QuestLine then return end
local print = WowAgent.Report or print
local OFF, OFFER, TURN_IN, DETAIL, PROGRESS, REWARD, BLOCKED = 0, 1, 2, 3, 4, 5, 6
local GOSSIP, GREETING = "GOSSIP_SHOW", "QUEST_GREETING"
local tracked, trackSet = {}, {}
local panel, sequence, cursor, elapsed = nil, 0, 0, 0
local screen
local rows = {}
local secret = issecretvalue or function() return false end

local function QuestKind(id, info, mapID)
    local classification = info and info.questClassification or C_QuestInfoSystem.GetQuestClassification(id)
    if classification == K.WorldQuest or C_QuestLog.IsWorldQuest(id) then return 4 end
    if classification == K.Recurring or classification == K.Calling
        or C_QuestLog.IsRepeatableQuest(id) then return 5 end
    local line = C_QuestLine.GetQuestLineInfo(id, mapID)
    if classification == K.Campaign or (info and (info.campaignID or 0) > 0)
        or (line and line.isCampaign) then return 1 end
    if classification == K.Legendary or classification == K.Meta
        or classification == K.BonusObjective or classification == K.Threat then return 7 end
    if info and info.isStory and not (line and line.isLocalStory) then return 2 end
    if classification == K.Important or (line and line.isImportant) then return 6 end
    if line and line.isLocalStory then return 3 end
    if classification == K.Normal then
        local title = info and info.title or C_QuestLog.GetTitleForQuestID(id)
        if title and title ~= "" then return 3 end
    end
    return 0
end

-- Independent navigation strip. Main quest/combat message layouts stay compatible.
local navFrame, navRows, navSequence, navElapsed = nil, {}, 0, 0
local navigation
local flyingTypes = {[242]=true, [247]=true, [248]=true, [402]=true, [424]=true, [436]=true}
local waterTypes = {[231]=true, [232]=true, [254]=true}
local travelKeys = {6, 7, 8, 5}
local function Public(value)
    if secret(value) then return nil end
    return value
end
local function Coordinate(value)
    return type(value) == "number" and value == value and value >= 0 and value <= 1
end
local function Mount(id)
    local api = C_MountJournal
    if not api or not api.GetMountInfoByID or not id or id == 0 then return end
    local name, spell, _, active, usable, _, favorite, _, _, hidden, collected = api.GetMountInfoByID(id)
    if not name or not collected or hidden then return end
    local _, _, _, _, mountType = api.GetMountInfoExtraByID(id)
    return {id = id, name = name, spell = spell, active = active, usable = usable,
            favorite = favorite, flying = flyingTypes[mountType] == true, water = waterTypes[mountType] == true}
end

function WowAgent.GetNavigationMounts(kind)
    local result, api = {}, C_MountJournal
    if not api or not api.GetMountIDs or not api.GetMountInfoExtraByID then return result end
    for _, id in ipairs(api.GetMountIDs()) do
        local mount = Mount(id)
        if mount and not mount.water and (kind ~= "fly" or mount.flying) then
            result[#result + 1] = {value = tostring(id), label = mount.name, favorite = mount.favorite}
        end
    end
    table.sort(result, function(a, b)
        if a.favorite ~= b.favorite then return a.favorite == true end
        return a.label < b.label
    end)
    return result
end

local function ChooseMount(kind, id)
    if id and id > 0 then
        local mount = Mount(id)
        if mount and mount.usable and not mount.water and (kind ~= "fly" or mount.flying) then return id end
        return nil
    end
    for _, entry in ipairs(WowAgent.GetNavigationMounts(kind)) do
        local mount = Mount(tonumber(entry.value))
        if mount.usable then return mount.id end
    end
end

function WowAgent.TravelMacro(kind)
    if kind == 3 then return "WATLand", "/dismount [mounted,noflying]" end
    local spell, name
    if kind == 4 then
        spell, name = 436854, "WATSteady"
    elseif navigation and (kind == 1 or kind == 2) then
        local mount = Mount(kind == 1 and navigation.fly or navigation.ground)
        spell, name = mount and mount.spell, kind == 1 and "WATFly" or "WATRide"
    end
    local spellName = spell and C_Spell and C_Spell.GetSpellName(spell)
    if not spellName then return end
    return name, "#showtooltip " .. spellName .. "\n/cast "
        .. (kind == 4 and "[nocombat,noflying] " or "[nocombat,nomounted] ") .. spellName
end

local function QuestDestination(id, mapID)
    local api = C_QuestLog
    if api.GetNextWaypointForMap then
        local x, y = api.GetNextWaypointForMap(id, mapID)
        if Coordinate(x) and Coordinate(y) then return mapID, x, y end
    end
    if api.GetNextWaypoint then
        local targetMap, x, y = api.GetNextWaypoint(id)
        if targetMap and Coordinate(x) and Coordinate(y) then return targetMap, x, y end
    end
    if QuestPOIGetIconInfo then
        local _, x, y = QuestPOIGetIconInfo(id)
        if Coordinate(x) and Coordinate(y) then return mapID, x, y end
    end
end

local function NavigationKeys()
    local support = WowAgent.LayoutSupport
    if not support or not support.ActionMatches then return false end
    for kind, key in ipairs(travelKeys) do
        local _, body = WowAgent.TravelMacro(kind)
        if body and not support.ActionMatches(key, nil, body) then return false end
    end
    return GetBindingAction("W", true) == "MOVEFORWARD"
        and GetBindingAction("SPACE", true) == "JUMP"
        and GetBindingAction("X", true) == "SITORSTAND"
end

local function UpdateNavigation()
    if not navigation then return end
    local id, mapID = navigation.quest, C_Map.GetBestMapForUnit("player") or 0
    local active, phase = C_QuestLog.IsOnQuest(id), 1
    if active then phase = C_QuestLog.ReadyForTurnIn(id) and 3 or 2
    elseif C_QuestLog.IsQuestFlaggedCompleted(id) then phase = 4 end
    local targetMap, gx, gy = QuestDestination(id, mapID)
    local position = C_Map.GetPlayerMapPosition and C_Map.GetPlayerMapPosition(mapID, "player")
    local px, py
    if position then px, py = position:GetXY() end
    px, py, gx, gy = Public(px), Public(py), Public(gx), Public(gy)
    local _, _, height
    if UnitPosition then _, _, height = UnitPosition("player") end
    height = Public(height)
    local width, depth
    if C_Map.GetMapWorldSize then width, depth = C_Map.GetMapWorldSize(mapID) end
    width, depth = Public(width), Public(depth)
    local flags, meta = 0, 1 + phase * 16
    local outdoors = IsOutdoors and IsOutdoors()
    local flyable = IsFlyableArea and IsFlyableArea()
    local fly, ground = Mount(navigation.fly), Mount(navigation.ground)
    if outdoors then flags = flags + 1 end
    if flyable then flags = flags + 2 end
    if IsMounted() then flags = flags + 4 end
    if IsFlying() then flags = flags + 8 end
    if IsFalling and IsFalling() then flags = flags + 16 end
    if IsSwimming and IsSwimming() then flags = flags + 32 end
    if UnitAffectingCombat and UnitAffectingCombat("player") then flags = flags + 64 end
    if UnitCastingInfo and Public(UnitCastingInfo("player")) then flags = flags + 128 end
    if outdoors and flyable and fly and fly.usable then flags = flags + 256 end
    if outdoors and ground and ground.usable then flags = flags + 512 end
    if C_PlayerInfo and C_PlayerInfo.GetGlidingInfo then
        local _, canGlide = C_PlayerInfo.GetGlidingInfo()
        if Public(canGlide) then flags = flags + 1024 end
    end
    if C_Spell and C_Spell.GetSpellName(436854) then flags = flags + 2048 end
    if NavigationKeys() then flags = flags + 4096 end
    if Coordinate(gx) and Coordinate(gy) then meta = meta + 128 else gx, gy = 0, 0 end
    if type(height) == "number" and height == height and height > -52428 and height < 52428 then
        meta = meta + 256
    else height = 0 end
    if width and depth and width > 0 and depth > 0 and width < 104857 and depth < 104857 then
        meta = meta + 512
    else width, depth = 0, 0 end
    if Coordinate(px) and Coordinate(py) then meta = meta + 1024 else px, py = 0, 0 end
    navSequence = (navSequence + 1) % 128
    local values = {id, targetMap or 0, math.floor(gx * 1048575 + .5), math.floor(gy * 1048575 + .5),
                    flags + navSequence * 8192, math.floor(height * 10 + 524288 + .5),
                    math.floor(width * 10 + .5), math.floor(depth * 10 + .5), meta,
                    math.floor(px * 1048575 + .5), math.floor(py * 1048575 + .5), mapID}
    for row, value in ipairs(values) do navRows[row](row + 3, value >= 0 and value <= 1048575 and value or 0) end
end

function WowAgent.PrepareQuestNavigation(id, fly, ground)
    if InCombatLockdown() or IsFlying() then return print("WowAgent：请落地脱战后准备寻路。") end
    if not id or not C_QuestLog.IsOnQuest(id) then return print("WowAgent：请先选择任务日志中已接取的任务。") end
    local support = WowAgent.LayoutSupport
    if not support or not support.SetupAction then return print("WowAgent：动作配置模块尚未加载。") end
    navigation = {quest = id, fly = ChooseMount("fly", fly), ground = ChooseMount("ground", ground)}
    navFrame:Hide()
    for kind, key in ipairs(travelKeys) do
        local name = WowAgent.TravelMacro(kind)
        if name and not support.SetupAction("travel", kind, key) then return end
    end
    if not support.SetupAction("quest", id, 9) or not support.SetupAction("assist", nil, 1) then return end
    if not NavigationKeys() then return print("WowAgent：请确认 W 为前进、空格为跳跃、X 为坐下/下降。") end
    if C_QuestLog.AddQuestWatch then C_QuestLog.AddQuestWatch(id) end
    WowAgent.QuestCommand("track " .. id)
    UpdateNavigation()
    navFrame:Show()
    local mapID = C_Map.GetBestMapForUnit("player")
    local targetMap, x, y = QuestDestination(id, mapID)
    print("WowAgent：寻路已准备，优先飞行，其次坐骑，最后步行。从本地主 App 运行 navigate-quest。")
    if targetMap and x and y then
        print(string.format("WowAgent：任务目标地图 %d，坐标 %.2f, %.2f。", targetMap, x * 100, y * 100))
    else print("WowAgent：任务目标坐标尚未返回，脚本会等待坐标。") end
end

function WowAgent.StopQuestNavigation()
    if navFrame then navFrame:Hide() end
end

navFrame = CreateFrame("Frame", nil, UIParent)
navFrame:SetSize(142, 192)
navFrame:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 144, 0)
navFrame:SetFrameStrata("TOOLTIP")
for row = 1, 12 do navRows[row] = WowAgent.NewPixelRow(navFrame, (row - 1) * 16) end
navFrame:SetScript("OnUpdate", function(_, dt)
    navElapsed = navElapsed + dt
    if navElapsed < .20 then return end
    navElapsed = 0
    UpdateNavigation()
end)
navFrame:Hide()

local function DialogueQuests()
    local candidates = {}
    local function Add(id, phase, index)
        if id and id > 0 then
            candidates[#candidates + 1] = { id = id, phase = phase, index = index }
        end
    end
    if panel == GOSSIP then
        for _, q in ipairs(C_GossipInfo.GetAvailableQuests()) do Add(q.questID, OFFER) end
        for _, q in ipairs(C_GossipInfo.GetActiveQuests()) do
            if q.isComplete then Add(q.questID, TURN_IN) end
        end
    elseif panel == GREETING then
        for i = 1, GetNumAvailableQuests() do
            local _, _, _, _, id = GetAvailableQuestInfo(i)
            Add(id, OFFER, i)
        end
        for i = 1, GetNumActiveQuests() do
            local _, complete = GetActiveTitle(i)
            if complete then Add(GetActiveQuestID(i), TURN_IN, i) end
        end
    end
    return candidates
end

local function Dialogue()
    if panel == GOSSIP or panel == GREETING then
        local match
        for _, q in ipairs(DialogueQuests()) do
            if trackSet[q.id] then
                if match then return 0, BLOCKED end
                match = q
            end
        end
        if match then return match.id, match.phase, match.index end
        return 0, BLOCKED
    elseif panel then
        local id = GetQuestID() or 0
        if id == 0 then return 0, BLOCKED end
        if panel == "QUEST_DETAIL" then return id, DETAIL end
        if panel == "QUEST_PROGRESS" and IsQuestCompletable() then return id, PROGRESS end
        if panel == "QUEST_COMPLETE" and GetNumQuestChoices() <= 1 then return id, REWARD end
        return id, BLOCKED
    end
    return 0, OFF
end

local events = CreateFrame("Frame")
for _, event in ipairs({GOSSIP, GREETING, "QUEST_DETAIL", "QUEST_PROGRESS", "QUEST_COMPLETE",
                        "GOSSIP_CLOSED", "QUEST_FINISHED", "PLAYER_ENTERING_WORLD"}) do
    events:RegisterEvent(event)
end
events:SetScript("OnEvent", function(_, event)
    if event == "GOSSIP_CLOSED" then
        if panel == GOSSIP then panel = nil end
    elseif event == "QUEST_FINISHED" or event == "PLAYER_ENTERING_WORLD" then
        panel = nil
    else
        panel = event
    end
end)

-- Invoked by an action-bar macro with a fixed quest ID.
function WowAgent.QuestAction(expectedID)
    if not screen:IsShown() or InCombatLockdown() then return end
    local id, phase, index = Dialogue()
    if id ~= expectedID or not trackSet[id] then return end
    if phase == OFFER then
        if index then SelectAvailableQuest(index) else C_GossipInfo.SelectAvailableQuest(id) end
    elseif phase == TURN_IN then
        if index then SelectActiveQuest(index) else C_GossipInfo.SelectActiveQuest(id) end
    elseif phase == DETAIL then
        if not QuestGetAutoAccept() then AcceptQuest() end
    elseif phase == PROGRESS then
        CompleteQuest()
    elseif phase == REWARD then
        GetQuestReward(1)
    end
end

local function QuestIDs()
    if #tracked > 0 then return tracked end
    local ids, seen = {}, {}
    local function Add(id)
        if id and id > 0 and not seen[id] then
            ids[#ids + 1], seen[id] = id, true
        end
    end
    for _, quest in ipairs(DialogueQuests()) do Add(quest.id) end
    if panel and panel ~= GOSSIP and panel ~= GREETING then Add(GetQuestID()) end
    for i = 1, C_QuestLog.GetNumQuestLogEntries() do
        local info = C_QuestLog.GetInfo(i)
        if info and not info.isHeader and not info.isHidden then Add(info.questID) end
    end
    return ids
end

local function Objective(id, active)
    if not active then return 0, 0 end
    local objectives = C_QuestLog.GetQuestObjectives(id)
    if not objectives then return 0, 0 end
    if #objectives == 0 then return 1, 0 end
    if #objectives ~= 1 then return 0, 0 end
    local done, required = objectives[1].numFulfilled, objectives[1].numRequired
    if secret(done) or secret(required) or not done or not required then return 0, 0 end
    if required <= 0 then return 3, 0 end
    if done < 0 or required > 1023 or done > 1023 then return 0, 0 end
    return 2, math.floor(done) * 1024 + math.floor(required)
end

local function UpdateRows()
    local ids = QuestIDs()
    cursor = #ids > 0 and cursor % #ids + 1 or 0
    local id = ids[cursor] or 0
    local mapID = C_Map.GetBestMapForUnit("player") or 0
    local dialogID, phase = Dialogue()
    local kind, state, objective, progress = 0, 0, 0, 0
    if id > 0 then
        local index = C_QuestLog.GetLogIndexForQuestID(id)
        local info = index and C_QuestLog.GetInfo(index)
        kind = QuestKind(id, info, mapID)
        local active = C_QuestLog.IsOnQuest(id)
        if active then
            state = C_QuestLog.ReadyForTurnIn(id) and 3 or 2
        elseif kind ~= 4 and kind ~= 5 and C_QuestLog.IsQuestFlaggedCompleted(id) then
            state = 4
        else
            state = 1
        end
        objective, progress = Objective(id, active)
    end
    sequence = (sequence + 1) % 128
    local info = kind + state * 16 + phase * 128 + objective * 1024 + sequence * 4096
    if #tracked > 0 then info = info + 524288 end
    local values = {id, info, progress, mapID, dialogID}
    for i, value in ipairs(values) do
        rows[i](i + 9, value >= 0 and value <= 1048575 and value or 0)
    end
end

screen = CreateFrame("Frame", nil, UIParent)
screen:SetSize(142, 80)
screen:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT")
screen:SetFrameStrata("TOOLTIP")
for i = 1, 5 do rows[i] = WowAgent.NewPixelRow(screen, (i - 1) * 16) end
screen:SetScript("OnUpdate", function(_, dt)
    elapsed = elapsed + dt
    if elapsed < 0.20 then return end
    elapsed = 0
    UpdateRows()
end)
screen:Hide()

function WowAgent.QuestCommand(args)
    local ids, seen = {}, {}
    if args == "prepare" or args:match("^prepare %d+$") then
        local id = tonumber(args:match("^prepare (%d+)$"))
        local available = DialogueQuests()
        if not id then
            if panel == "QUEST_DETAIL" then id = GetQuestID()
            else
                for _, quest in ipairs(available) do
                    if quest.phase == OFFER then
                        if id then
                            print("WowAgent：NPC 提供多个任务，请先从任务列表选择一项。")
                            return
                        end
                        id = quest.id
                    end
                end
            end
        end
        local offered = id and panel == "QUEST_DETAIL" and GetQuestID() == id
        for _, quest in ipairs(available) do
            if quest.id == id and quest.phase == OFFER then offered = true end
        end
        if not offered or not id or id < 1 or id > 1048575 or C_QuestLog.IsOnQuest(id) then
            print("WowAgent：请先打开 NPC 的未接任务对话，再准备接任务。")
            return
        end
        local support = WowAgent.LayoutSupport
        if not support or not support.SetupAction then
            print("WowAgent：动作配置模块尚未加载，请重载插件。")
            return
        end
        if not support.SetupAction("quest", id, 9) then return end
        tracked, trackSet, cursor = {id}, {[id] = true}, 0
        C_QuestLog.RequestLoadQuestByID(id)
        UpdateRows()
        screen:Show()
        print("WowAgent：接任务已准备，数字键 9，任务 " .. id .. "。从主 App 运行 accept-quest。")
    elseif args == "off" then
        screen:Hide()
    elseif args == "scan" then
        tracked, trackSet, cursor = {}, {}, 0
        UpdateRows()
        screen:Show()
    elseif args:match("^track %d[%d ]*$") then
        for value in args:sub(7):gmatch("%d+") do
            local id = tonumber(value)
            if id < 1 or id > 1048575 or seen[id] or #ids == 8 then
                print("WowAgent quest: provide 1 to 8 distinct, valid quest IDs")
                return
            end
            ids[#ids + 1], seen[id] = id, true
        end
        tracked, trackSet, cursor = ids, seen, 0
        for _, id in ipairs(ids) do C_QuestLog.RequestLoadQuestByID(id) end
        UpdateRows()
        screen:Show()
        print("WowAgent tracking quests: " .. table.concat(ids, ", "))
    else
        print("/wagent quest scan | track <id ...> | prepare [id] | off")
    end
end

