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
local function Public(value)
    if secret(value) then return nil end
    return value
end
local function QuestObjectives(id)
    local entries = Public(C_QuestLog.GetQuestObjectives(id))
    if type(entries) == "table" and (not canaccesstable or canaccesstable(entries)) then return entries end
end
local forever = WowAgent.Forever
local retail = forever and forever.questData or WowAgent.RetailQuestData
local FindFollowup

local function QuestKind(id, info, mapID)
    if forever and retail.quests[id] and (retail.quests[id].map or retail.map) == mapID then return 3 end
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

local function IsStoryQuest(id, mapID)
    local kind = QuestKind(id, nil, mapID)
    return kind >= 1 and kind <= 3
end

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
    if QuestFrameDetailPanel and QuestFrameDetailPanel:IsVisible() then
        local id = GetQuestID()
        if id and id > 0 then return id, DETAIL end
    end
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
        if panel == "QUEST_COMPLETE" then return id, GetNumQuestChoices() <= 1 and REWARD or 7 end
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
        if QuestFlagsPVP and QuestFlagsPVP() then return end
        if QuestGetAutoAccept() then
            if AcknowledgeAutoAcceptQuest then AcknowledgeAutoAcceptQuest() end
        else
            AcceptQuest()
        end
    elseif phase == PROGRESS then
        CompleteQuest()
    elseif phase == REWARD then
        GetQuestReward(1)
    end
end

function WowAgent.OpenQuestOfferID()
    if panel == "QUEST_DETAIL" or (QuestFrameDetailPanel and QuestFrameDetailPanel:IsVisible()) then
        local id = GetQuestID()
        if id and id > 0 and not C_QuestLog.IsOnQuest(id) and not C_QuestLog.IsQuestFlaggedCompleted(id) then
            return id
        end
    end
    local best, mapID = nil, C_Map.GetBestMapForUnit("player")
    for _, quest in ipairs(DialogueQuests()) do
        if quest.phase == OFFER and (not best or quest.id < best) and IsStoryQuest(quest.id, mapID) then
            best = quest.id
        end
    end
    return best
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

local function IsResourceObjective(id, objectives, index)
    local profile = retail and retail.quests[id]
    if profile and profile.resource and (not objectives or #objectives <= 1) then return true end
    local objective = objectives and index and Public(objectives[index])
    if type(objective) == "table" and (not canaccesstable or canaccesstable(objective))
        and Public(objective.type) == "progressbar" then return true end
    if objectives and #objectives > 1 then return false end
    -- The native tracker uses leaderboards; not every client preserves the
    -- progressbar type in GetQuestObjectives.
    local index = Public(C_QuestLog.GetLogIndexForQuestID(id))
    if index and GetNumQuestLeaderBoards and GetQuestLogLeaderBoard then
        for i = 1, Public(GetNumQuestLeaderBoards(index)) or 0 do
            local _, kind = GetQuestLogLeaderBoard(i, index, true)
            if Public(kind) == "progressbar" then return true end
        end
    end
    return false
end

local function ActiveObjective(objectives)
    if type(objectives) ~= "table" then return end
    for index, entry in ipairs(objectives) do
        entry = Public(entry)
        if type(entry) ~= "table" or (canaccesstable and not canaccesstable(entry)) or secret(entry.finished) then return end
        local finished = Public(entry.finished)
        if finished == nil then
            local done, required = Public(entry.numFulfilled), Public(entry.numRequired)
            finished = type(done) == "number" and type(required) == "number" and required > 0 and done >= required
        end
        if not finished then return entry, index end
    end
    return nil, 0, #objectives > 0
end

local function Objective(id, active, objectives)
    if not active then return 0, 0 end
    if objectives == nil then objectives = QuestObjectives(id) end
    local entry, index, finished = ActiveObjective(objectives)
    if type(objectives) == "table" and #objectives > 0 and index == nil then return 0, 0 end
    if finished and #objectives > 1 then return 1, 0, 0, true end
    if IsResourceObjective(id, objectives, index) then
        if GetQuestProgressBarPercent then
            local ok, value = pcall(GetQuestProgressBarPercent, id)
            local percent = ok and Public(value)
            if type(percent) == "number" and percent >= 0 and percent <= 100 then
                -- Wire code 3 with a denominator of 1000 means tenths of a percent.
                return 3, math.floor(percent * 10) * 1024 + 1000, index or 1
            end
        end
        return 0, 0
    end
    if not objectives then return 0, 0 end
    if #objectives == 0 then return 1, 0 end
    entry = entry or (finished and #objectives == 1 and Public(objectives[1]))
    if not entry then return 0, 0 end
    local done, required = Public(entry.numFulfilled), Public(entry.numRequired)
    if type(done) ~= "number" or type(required) ~= "number" or required <= 0 then return 3, 0, index end
    if done < 0 or required > 1023 or done > 1023 then return 0, 0, index end
    return 2, math.floor(done) * 1024 + math.floor(required), index
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
    if state == 4 and FindFollowup and (phase == OFF or phase == BLOCKED or phase == DETAIL) then
        local candidate, candidatePhase = FindFollowup(phase, dialogID)
        dialogID, phase = candidate or 0, candidatePhase or phase
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
    if args:match("^recovery") and WowAgent.ConfigureRecovery then
        return WowAgent.ConfigureRecovery(args)
    end
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
        if WowAgent.StopQuestNavigation then WowAgent.StopQuestNavigation() end
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

-- Independent navigation strip. Main quest/combat message layouts stay compatible.
local navFrame, writeSky, writeTask, writeClick, writeRound, writeMinimap, writeRecovery, writeArea, writeLoot
local writeObjective, writeObjectiveProgress
local writeCamera, writeCameraState, writeBuild, writeBuildSchema, writeVisualUI
local writeSkySkills, writeSkyMotion
local lootEpoch = 0
local navSequence, navElapsed = 0, 0
local navRows = {}
local navigation
local PrepareTutorialMount
local skyButton
local flyingTypes = {[242]=true, [247]=true, [248]=true, [402]=true, [424]=true, [436]=true}
local waterTypes = {[231]=true, [232]=true, [254]=true}
local travelKeys = {6, 7, 8, 5}
local function Coordinate(value)
    return not secret(value) and type(value) == "number" and value == value and value >= 0 and value <= 1
end
local function Mount(id)
    local api = C_MountJournal
    if not api or not api.GetMountInfoByID or not api.GetMountInfoExtraByID or not id or id == 0 then return end
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
        if mount and not mount.water and (kind ~= "fly" or mount.flying) then return id end
        return nil
    end
    local fallback
    for _, entry in ipairs(WowAgent.GetNavigationMounts(kind)) do
        local mount = Mount(tonumber(entry.value))
        -- A skyriding mount can replace the normal action bar even on the ground.
        if mount and (kind ~= "ground" or not mount.flying) then
            if mount.usable then return mount.id end
            fallback = fallback or mount.id
        end
    end
    return fallback
end

function WowAgent.TravelMacro(kind)
    if kind == 3 then return "WATLand", "/dismount [mounted,noflying]" end
    local spell, name
    if kind == 4 and navigation and navigation.fly then
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
    if not mapID or mapID <= 0 then return end
    if api.GetNextWaypointForMap then
        local x, y = api.GetNextWaypointForMap(id, mapID)
        if Coordinate(x) and Coordinate(y) then return mapID, x, y end
    end
    if api.GetQuestsOnMap then
        for _, quest in ipairs(api.GetQuestsOnMap(mapID) or {}) do
            if quest.questID == id and Coordinate(quest.x) and Coordinate(quest.y) then
                return mapID, quest.x, quest.y
            end
        end
    end
    if api.GetNextWaypoint then
        local targetMap, x, y = api.GetNextWaypoint(id)
        targetMap = Public(targetMap)
        if type(targetMap) == "number" and targetMap > 0 and Coordinate(x) and Coordinate(y) then return targetMap, x, y end
    end
    if QuestPOIGetIconInfo and api.GetMapForQuestPOIs and api.GetMapForQuestPOIs() == mapID then
        local _, x, y = QuestPOIGetIconInfo(id)
        if Coordinate(x) and Coordinate(y) then return mapID, x, y end
    end
end

local movementBindings = {
    {"W", "MOVEFORWARD", "前进"}, {"S", "MOVEBACKWARD", "后退"},
    {"LEFT", "TURNLEFT", "左转"}, {"RIGHT", "TURNRIGHT", "右转"},
    {"SPACE", "JUMP", "跳跃"},
}

local taskBindings = {
    {"TAB", "TARGETNEARESTENEMY", "选敌"}, {"F", "INTERACTTARGET", "与目标互动"},
    {"1", "ACTIONBUTTON1", "一键输出"}, {"5", "ACTIONBUTTON5", "切换飞行风格"},
    {"6", "ACTIONBUTTON6", "飞行坐骑"}, {"7", "ACTIONBUTTON7", "地面坐骑"},
    {"8", "ACTIONBUTTON8", "下马"}, {"9", "ACTIONBUTTON9", "任务对话"},
    {"Q", "STRAFELEFT", "左横移"}, {"E", "STRAFERIGHT", "右横移"},
}
for _, binding in ipairs(movementBindings) do taskBindings[#taskBindings + 1] = binding end
local taskBindingOwner
local taskTargetButton, taskItemButton, taskDialogueButton, taskFetchButton, taskSweepButton
local taskDialogueBody = "/run WowAgent.RunPreparedQuestAction()"
local fetchSpell = 125050
local camera = WowAgent.QuestCamera

local pickupNPCs = {
    [90882] = {zhCN = {Horde = "萨尔"}, zhTW = {Horde = "索爾"}, enUS = {Horde = "Thrall"}},
    [90883] = {zhCN = {Horde = "萨尔", Alliance = "吉安娜·普罗德摩尔"},
        zhTW = {Horde = "索爾", Alliance = "珍娜·普勞德摩爾"},
        enUS = {Horde = "Thrall", Alliance = "Lady Jaina Proudmoore"}},
}

local function RetailQuest(id, map)
    local data = retail and retail.quests[id]
    return data and map == (data.map or retail.map) and data
end

local function IsFollowup(id, previous, map)
    local data = RetailQuest(id, map)
    return data and data.after and data.after[previous]
end

local function RetailNPCPoint(id, role, map)
    local data = RetailQuest(id, map)
    local npc = data and data[role]
    local coords = data and retail.NPCPoint(data, npc, role)
    if coords then return {id = id, map = map, x = coords[1] / 100, y = coords[2] / 100, npc = npc} end
end

local function PickupNPC(id)
    local names = pickupNPCs[id]
    local locale = GetLocale and GetLocale()
    local localized = names and (names[locale] or (locale == "enGB" and names.enUS))
    local name = localized and UnitFactionGroup and localized[UnitFactionGroup("player")]
    local data = RetailQuest(id, C_Map.GetBestMapForUnit("player"))
    return name or (data and data.pickup)
end

local function TurnInNPC(id, override)
    if override then return override end
    if id == 90882 then return PickupNPC(id) end
    local data = RetailQuest(id, C_Map.GetBestMapForUnit("player"))
    return id == 90883 and 244729 or (data and data.turnIn)
end

local function QuestArea()
    local map = C_Map.GetBestMapForUnit("player")
    if not map or not C_Map.GetMapWorldSize then return end
    local position = C_Map.GetPlayerMapPosition(map, "player")
    if not position then return end
    local px, py = position:GetXY()
    local width, height = C_Map.GetMapWorldSize(map)
    px, py, width, height = Public(px), Public(py), Public(width), Public(height)
    if not Coordinate(px) or not Coordinate(py) or not width or not height or width <= 0 or height <= 0 then return end
    return {map = map, x = px, y = py, width = width, height = height}
end

local function InQuestArea(point, area)
    return point and area and point.map == area.map
        and ((point.x - area.x) * area.width)^2 + ((point.y - area.y) * area.height)^2 < 400^2
end

local function NearbyQuest(expectedID, area, afterID, suspended)
    local player = QuestArea()
    if not player or not C_QuestLine.GetAvailableQuestLines then return end
    local map = player.map
    area = area or player
    local nearest, best = nil, math.huge
    for _, quest in ipairs(C_QuestLine.GetAvailableQuestLines(map) or {}) do
        local id, startMap = Public(quest.questID), Public(quest.startMapID)
        local x, y = Public(quest.x), Public(quest.y)
        if id and not (suspended and suspended[id]) and (not expectedID or expectedID == id)
            and (not afterID or IsFollowup(id, afterID, map))
            and Public(quest.isQuestStart) == true and Public(quest.isHidden) == false
            and Public(quest.isDaily) ~= true and Public(quest.isMeta) ~= true
            and startMap == map
            and (not Enum.QuestLineFloorLocation or Public(quest.floorLocation) == Enum.QuestLineFloorLocation.Same)
            and Coordinate(x) and Coordinate(y)
            and not C_QuestLog.IsOnQuest(id) and not C_QuestLog.IsQuestFlaggedCompleted(id) then
            local point = RetailNPCPoint(id, "pickup", map) or {id = id, map = map, x = x, y = y}
            local distance = ((point.x - player.x) * player.width)^2 + ((point.y - player.y) * player.height)^2
            if distance < best and InQuestArea(point, area) and (PickupNPC(id) or Public(quest.isCampaign) == true
                or Public(quest.isLocalStory) == true or IsStoryQuest(id, map)) then
                nearest, best = point, distance
            end
        end
    end
    return nearest
end

-- The map API can omit an offer that is visible on its NPC. A completed
-- prerequisite only authorizes visiting the known giver to inspect dialogue.
local function KnownFollowup(expectedID, area, afterID, suspended)
    local player = QuestArea()
    if not player or not retail or player.map ~= retail.map then return end
    area = area or player
    local nearest, best = nil, math.huge
    for id, data in pairs(retail.quests) do
        if not (suspended and suspended[id]) and (not expectedID or id == expectedID)
            and (data.starter or (data.after and next(data.after)))
            and (not afterID or (data.after and data.after[afterID]))
            and (not forever or (UnitFactionGroup("player") == "Alliance" and UnitLevel("player") >= data.level))
            and Public(C_QuestLog.IsOnQuest(id)) == false
            and Public(C_QuestLog.IsQuestFlaggedCompleted(id)) == false then
            local ready = true
            for prerequisite in pairs(data.after or {}) do
                if Public(C_QuestLog.IsQuestFlaggedCompleted(prerequisite)) ~= true then
                    ready = false
                    break
                end
            end
            local point = ready and RetailNPCPoint(id, "pickup", player.map)
            if point and InQuestArea(point, area) then
                local distance = ((point.x - player.x) * player.width)^2 + ((point.y - player.y) * player.height)^2
                if distance < best or (distance == best and id < nearest.id) then
                    point.verifyOffer = true
                    nearest, best = point, distance
                end
            end
        end
    end
    return nearest
end

function WowAgent.GetNearbyQuestID()
    local map = C_Map.GetBestMapForUnit("player")
    if map and C_QuestLine.RequestQuestLinesForMap then C_QuestLine.RequestQuestLinesForMap(map) end
    local found = NearbyQuest() or KnownFollowup()
    return found and found.id
end

function WowAgent.HasQuestTurnInProfile(id)
    return TurnInNPC(id) ~= nil
end

function WowAgent.GetActiveSupportedQuestID()
    local tracked = C_SuperTrack and C_SuperTrack.GetSuperTrackedQuestID
        and Public(C_SuperTrack.GetSuperTrackedQuestID())
    local map = C_Map.GetBestMapForUnit("player")
    if tracked and tracked > 0 and C_QuestLog.IsOnQuest(tracked) and IsStoryQuest(tracked, map) then return tracked end
    for _, id in ipairs({90883, 90882}) do
        if WowAgent.HasQuestTurnInProfile(id) and C_QuestLog.IsOnQuest(id) then return id end
    end
    local found
    for i = 1, C_QuestLog.GetNumQuestLogEntries() do
        local info = C_QuestLog.GetInfo(i)
        if info and not info.isHeader and not info.isHidden and IsStoryQuest(info.questID, map) then
            if found then return end
            found = info.questID
        end
    end
    return found
end

local function AwaitingFollowup(plan)
    return plan.automatic and plan.rewardAt and GetTime() - plan.rewardAt < 8
end

local function QuestGiverGUID()
    return UnitGUID and (Public(UnitGUID("questnpc")) or Public(UnitGUID("npc")))
end

FindFollowup = function(phase, id)
    local plan = navigation
    if not plan or plan.round or not AwaitingFollowup(plan) or not C_QuestLog.IsQuestFlaggedCompleted(plan.quest) then return end
    local sameNPC = plan.rewardNPC and QuestGiverGUID() == plan.rewardNPC
    if phase == DETAIL then
        local map = C_Map.GetBestMapForUnit("player")
        if IsFollowup(id, plan.quest, map) or (sameNPC and IsStoryQuest(id, map)) then
            return id, DETAIL
        end
        return
    end
    if phase == BLOCKED then
        if sameNPC then
            local offer = WowAgent.OpenQuestOfferID()
            if offer then return offer, OFFER end
        end
        return
    end
    local candidate = NearbyQuest(nil, nil, plan.quest) or NearbyQuest() or KnownFollowup(nil, nil, plan.quest)
    plan.nextPickupID = candidate and candidate.id
    return candidate and candidate.id, OFF
end

local ROUND_PICKUP, ROUND_OBJECTIVES, ROUND_TURN_IN, ROUND_DONE = 1, 2, 3, 4

local function QuestPhase(id)
    if C_QuestLog.IsOnQuest(id) then return C_QuestLog.ReadyForTurnIn(id) and 3 or 2 end
    return C_QuestLog.IsQuestFlaggedCompleted(id) and 4 or 1
end

local function AddRoundQuest(round, id)
    for _, member in ipairs(round.jobs) do if member == id then return end end
    round.jobs[#round.jobs + 1] = id
end

local function RoundQuest(round, phase)
    for _, id in ipairs(round.jobs) do
        if not (round.suspended and round.suspended[id]) and QuestPhase(id) == phase then return id end
    end
end

local function ReadyRoundQuest(round)
    for _, offer in ipairs(DialogueQuests()) do
        if offer.phase == TURN_IN and not (round.suspended and round.suspended[offer.id])
            and QuestPhase(offer.id) == 3 then
            for _, id in ipairs(round.jobs) do if id == offer.id then return id end end
        end
    end
    return RoundQuest(round, 3)
end

local function SavedRound()
    local character = UnitGUID and Public(UnitGUID("player"))
    if type(character) ~= "string" or not character:match("^Player%-") then return end
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.questRounds = WowAgentDB.questRounds or {}
    return WowAgentDB.questRounds, character
end

local function ReconcileRoundSuspensions(round)
    local count = 0
    for id, phase in pairs(round.suspended or {}) do
        if QuestPhase(id) ~= phase then
            round.suspended[id] = nil
        else
            count = count + 1
        end
    end
    return count
end

local function HasOpenQuestOffer(id)
    if WowAgent.OpenQuestOfferID() == id then return true end
    for _, offered in ipairs(DialogueQuests()) do
        if offered.id == id and offered.phase == OFFER then return true end
    end
    return false
end

local function QuestPickup(id, round)
    local anchor = round and round.anchor
    return NearbyQuest(id, anchor)
        or (round and round.offers and InQuestArea(round.offers[id], anchor) and round.offers[id])
        or KnownFollowup(id, anchor)
end

function WowAgent.GetQuestRoundResumeID()
    local saved, character = SavedRound()
    local round = saved and saved[character]
    if not round or not round.anchor or round.anchor.map ~= C_Map.GetBestMapForUnit("player") then return end
    ReconcileRoundSuspensions(round)
    if round.stage == ROUND_DONE then return end
    local current = not (round.suspended and round.suspended[round.current]) and round.current
    if current then
        local phase = QuestPhase(current)
        if round.immediate and phase >= 3 then return current end
        if round.stage == ROUND_PICKUP then
            if phase == 2 or phase == 3
                or (phase == 1 and (HasOpenQuestOffer(current) or QuestPickup(current, round))) then return current end
            current = nil
        elseif phase == 1 then
            current = nil
        end
    end
    if round.stage == ROUND_TURN_IN then return RoundQuest(round, 3) or current end
    return RoundQuest(round, 2) or RoundQuest(round, 3) or current
end

local function AddNearbyActiveQuests(round)
    local anchor = round.anchor
    for i = 1, C_QuestLog.GetNumQuestLogEntries() do
        local info = C_QuestLog.GetInfo(i)
        if info and not info.isHeader and not info.isHidden and info.questID and C_QuestLog.IsOnQuest(info.questID)
            and IsStoryQuest(info.questID, anchor.map) then
            local map, x, y = QuestDestination(info.questID, anchor.map)
            if map and Coordinate(x) and Coordinate(y) and InQuestArea({map = map, x = x, y = y}, anchor) then
                AddRoundQuest(round, info.questID)
            end
        end
    end
end

function WowAgent.GetNearbyActiveQuestID()
    local anchor = QuestArea()
    if not anchor then return end
    local round = {anchor = anchor, jobs = {}}
    AddNearbyActiveQuests(round)
    return round.jobs[1]
end

local function PrepareRound(id)
    local anchor = QuestArea()
    if not anchor then return end
    local saved, character = SavedRound()
    local round = saved and saved[character]
    if round and round.anchor and round.anchor.map == anchor.map then
        local resume = WowAgent.GetQuestRoundResumeID()
        if round.stage == ROUND_DONE or (round.suspended and round.suspended[id] and not resume) then
            round.stage, round.immediate = ROUND_PICKUP, nil
            -- Explicit preparation after exhaustion starts a new attempt for
            -- ALL unfinished jobs. Resuming an unfinished round keeps its skips.
            round.suspended = {}
            local unfinished = {}
            for _, member in ipairs(round.jobs) do
                if C_QuestLog.IsOnQuest(member) then unfinished[#unfinished + 1] = member end
            end
            round.jobs = unfinished
            if C_QuestLog.IsOnQuest(id) then AddRoundQuest(round, id) end
            AddNearbyActiveQuests(round)
        elseif round.stage ~= ROUND_PICKUP or round.immediate
            or (round.suspended and (round.suspended[round.current] or round.suspended[id])) then
            id = resume or id
            if round.stage ~= ROUND_PICKUP and QuestPhase(id) == 3 and not RoundQuest(round, 2) then
                round.stage = ROUND_TURN_IN
            end
        end
        if id ~= round.current then round.immediate, round.acceptingID = nil, nil end
        round.current, round.next, round.waitSince, round.scanRetried = id, nil, nil, nil
        round.offers = round.offers or {}
        round.options = round.options or {}
        return round, id
    end
    round = {stage = ROUND_PICKUP, anchor = anchor, jobs = {}, offers = {}, options = {}, current = id}
    if C_QuestLog.IsOnQuest(id) then AddRoundQuest(round, id) end
    AddNearbyActiveQuests(round)
    return round, id
end

local function RememberRoundOffers(round)
    local point = QuestArea()
    if not point then return end
    local giver = QuestGiverGUID()
    local npcID = giver and tonumber(giver:match("^Creature%-%d+%-%d+%-%d+%-%d+%-(%d+)%-"))
    local name
    if npcID and UnitName then
        for _, unit in ipairs({"questnpc", "npc"}) do
            if Public(UnitGUID(unit)) == giver then
                name = Public(UnitName(unit))
                if type(name) == "string" and name ~= "" then break end
            end
        end
    end
    local function Remember(id)
        if QuestPhase(id) == 1 and (PickupNPC(id) or IsStoryQuest(id, point.map)) then
            round.offers[id] = {id = id, map = point.map, x = point.x, y = point.y, npc = npcID, name = name}
        end
    end
    local id, phase = Dialogue()
    if phase == DETAIL then Remember(id) end
    for _, quest in ipairs(DialogueQuests()) do
        if quest.phase == OFFER then Remember(quest.id) end
    end
end

local function RoundOffer(round, area, previous)
    local suspended = round.suspended or {}
    local visible = WowAgent.OpenQuestOfferID()
    if visible and not suspended[visible] and InQuestArea(round.offers[visible], area) then return round.offers[visible] end
    local player = QuestArea()
    if not player then return end
    if previous then
        local followup = NearbyQuest(nil, area, previous, suspended)
        if followup then return followup end
    end
    local nextOffer = NearbyQuest(nil, area, nil, suspended)
    local function Distance(offer)
        return ((offer.x - player.x) * player.width)^2 + ((offer.y - player.y) * player.height)^2
    end
    local best = nextOffer and Distance(nextOffer) or math.huge
    for id, offer in pairs(round.offers) do
        if QuestPhase(id) ~= 1 then
            round.offers[id] = nil
        elseif not suspended[id] and InQuestArea(offer, area) then
            local distance = Distance(offer)
            if distance < best or (distance == best and id < nextOffer.id) then nextOffer, best = offer, distance end
        end
    end
    return nextOffer or KnownFollowup(nil, area, previous, suspended)
end

local function RoundNeedsAdvance(round, phase)
    return (round.suspended and round.suspended[round.current])
        or (round.stage == ROUND_PICKUP and phase ~= 1 and not (round.immediate and phase == 3))
        or (round.stage == ROUND_OBJECTIVES and phase ~= 2)
        or (round.stage == ROUND_TURN_IN and phase == 4)
end

local function NextRoundAction(plan, phase)
    local round = plan.round
    ReconcileRoundSuspensions(round)
    RememberRoundOffers(round)
    if round.stage == ROUND_PICKUP then
        if phase == 2 or phase == 3 then AddRoundQuest(round, plan.quest) end
        if round.acceptingID == plan.quest and phase == 3 then round.immediate = true end
        if phase == 2 then round.immediate = nil end
    end
    if (round.stage >= ROUND_OBJECTIVES and RoundQuest(round, 1))
        or (round.stage == ROUND_TURN_IN and phase == 2
            and not (round.suspended and round.suspended[plan.quest])) then return {blocked = true} end
    if not RoundNeedsAdvance(round, phase) then
        round.waitSince, round.scanRetried = nil, nil
        return
    end
    if not round.waitSince then
        round.waitSince = GetTime()
        local area = QuestArea()
        if area and C_QuestLine.RequestQuestLinesForMap then C_QuestLine.RequestQuestLinesForMap(area.map) end
    end
    -- The quest log and nearby quest markers update separately after accepting or turning in.
    if GetTime() - round.waitSince < 2 then return end
    if round.stage == ROUND_PICKUP then
        local pickup = RoundOffer(round, round.anchor, phase == 4 and plan.quest or nil)
        if pickup then return {stage = ROUND_PICKUP, id = pickup.id, pickup = pickup} end
    end
    if round.stage <= ROUND_OBJECTIVES then
        local active, ready = RoundQuest(round, 2), ReadyRoundQuest(round)
        if active then return {stage = ROUND_OBJECTIVES, id = active} end
        if ready then return {stage = ROUND_TURN_IN, id = ready} end
    else
        local ready = ReadyRoundQuest(round)
        if ready then return {stage = ROUND_TURN_IN, id = ready} end
        if RoundQuest(round, 2) then return {blocked = true} end
    end
    local area = QuestArea()
    local pickup = area and (RoundOffer(round, area, phase == 4 and plan.quest or nil)
        or KnownFollowup(nil, area, nil, round.suspended))
    if pickup then return {stage = ROUND_PICKUP, id = pickup.id, pickup = pickup, anchor = area} end
    if area and C_QuestLine.RequestQuestLinesForMap then
        if not round.scanRetried then
            round.scanRetried = true
            C_QuestLine.RequestQuestLinesForMap(area.map)
        end
        if GetTime() - round.waitSince < 6 then return end
    end
    return {stage = ROUND_DONE}
end

local function CreatureID(guid)
    return type(guid) == "string" and tonumber(guid:match("^Creature%-%d+%-%d+%-%d+%-%d+%-(%d+)%-")) or nil
end

local function MatchesNPC(unit, guid, expected)
    if not guid or not guid:match("^Creature%-") or Public(UnitIsDead(unit)) ~= false then return false end
    if type(expected) == "number" then return CreatureID(guid) == expected end
    return expected and UnitName and Public(UnitName(unit)) == expected
end

local function SafeTaskTargetName(name)
    -- Lua patterns operate on bytes. Reject macro syntax and ASCII controls
    -- without treating bytes inside a localized UTF-8 name as controls.
    if type(name) == "string" and name ~= "" and not name:find("[%z\1-\31\127%[%];]") then return name end
end

local function KnownTaskTargetName(expected)
    if type(expected) == "string" then return SafeTaskTargetName(expected) end
    if not retail or not retail.npcNames then return end
    local names = retail.npcNames[expected]
    local locale = GetLocale and GetLocale()
    return names and SafeTaskTargetName(names[locale == "enGB" and "enUS" or locale])
end

local function TaskClickMatches(key, name)
    local action = Public(GetBindingAction(key, true))
    return type(action) == "string" and action:lower() == ("CLICK " .. name .. ":LeftButton"):lower()
end

local function GameTaskOptions(enable)
    local flags = Enum and Enum.SoftTargetEnableFlags
    if not flags or not C_CVar or not C_CVar.GetCVar then return false end
    if enable and C_CVar.SetCVar then
        pcall(C_CVar.SetCVar, "softTargetInteract", flags.Any)
        pcall(C_CVar.SetCVar, "autoLootDefault", 1)
        pcall(C_CVar.SetCVar, "nameplateShowEnemies", 1)
    end
    return tonumber(C_CVar.GetCVar("softTargetInteract")) == flags.Any
        and C_CVar.GetCVar("autoLootDefault") == "1"
end

local function QuestItemID(id)
    local index = C_QuestLog.GetLogIndexForQuestID(id)
    local link = index and GetQuestLogSpecialItemInfo and Public(GetQuestLogSpecialItemInfo(index))
    return type(link) == "string" and tonumber(link:match("item:(%d+)")) or nil
end

local function KnowsFetch()
    local api = C_SpellBook and C_SpellBook.IsSpellKnown or IsPlayerSpell
    return api and Public(api(fetchSpell)) == true
end

local function SetupTaskClicks(plan)
    if not SetOverrideBindingClick then return false end
    if not taskTargetButton then
        taskTargetButton = CreateFrame("Button", "WowAgentTaskTarget", UIParent, "SecureActionButtonTemplate")
        taskTargetButton:RegisterForClicks("AnyDown", "AnyUp")
        taskTargetButton:SetAttribute("type", "target")
        taskTargetButton:SetAttribute("unit", "softinteract")
    end
    SetOverrideBindingClick(taskBindingOwner, false, "K", "WowAgentTaskTarget", "LeftButton")
    local name = KnowsFetch() and C_Spell and C_Spell.GetSpellName and Public(C_Spell.GetSpellName(fetchSpell))
    if type(name) == "string" and name ~= "" then
        if not taskFetchButton then
            taskFetchButton = CreateFrame("Button", "WowAgentTaskFetch", UIParent, "SecureActionButtonTemplate")
            taskFetchButton:RegisterForClicks("AnyDown", "AnyUp")
            taskFetchButton:SetAttribute("type", "macro")
        end
        plan.fetchBody = "/cast " .. name
        taskFetchButton:SetAttribute("macrotext", plan.fetchBody)
        SetOverrideBindingClick(taskBindingOwner, false, "L", "WowAgentTaskFetch", "LeftButton")
        if not taskSweepButton then
            taskSweepButton = CreateFrame("Button", "WowAgentTaskSweep", UIParent, "SecureActionButtonTemplate")
            taskSweepButton:RegisterForClicks("AnyDown", "AnyUp")
            taskSweepButton:SetAttribute("type", "macro")
        end
        plan.sweepBody = "/cleartarget\n/cast " .. name
        taskSweepButton:SetAttribute("macrotext", plan.sweepBody)
        SetOverrideBindingClick(taskBindingOwner, false, "O", "WowAgentTaskSweep", "LeftButton")
    end
    if plan.item then
        if not taskItemButton then
            taskItemButton = CreateFrame("Button", "WowAgentTaskItem", UIParent, "SecureActionButtonTemplate")
            taskItemButton:RegisterForClicks("AnyDown", "AnyUp")
            taskItemButton:SetAttribute("type", "item")
        end
        taskItemButton:SetAttribute("item", "item:" .. plan.item)
        SetOverrideBindingClick(taskBindingOwner, false, "J", "WowAgentTaskItem", "LeftButton")
    end
    return true
end

local function CorpseLootFlags(plan, phase, task)
    if (phase ~= 2 and phase ~= 3) or task % 2 ~= 1 or InCombatLockdown() or IsMounted()
        or (IsFlying and IsFlying()) or (IsFalling and IsFalling()) or (IsSwimming and IsSwimming())
        or (GetCursorInfo and GetCursorInfo()) or (SpellIsTargeting and SpellIsTargeting()) then return 0 end
    local flags = 0
    local target = UnitGUID and Public(UnitGUID("target"))
    if CanLootUnit and type(target) == "string" and target:match("^Creature%-")
        and Public(UnitIsDead("target")) == true then
        local ok, hasLoot, canLoot = pcall(CanLootUnit, target)
        if ok then
            hasLoot, canLoot = Public(hasLoot), Public(canLoot)
            if hasLoot == false or canLoot == false then flags = 8192 end
            if hasLoot == true and canLoot == true then flags = 8192 + 16384 end
        end
    end
    if not plan.fetchBody or not KnowsFetch() or not TaskClickMatches("L", "WowAgentTaskFetch")
        or Public(taskFetchButton:GetAttribute("macrotext")) ~= plan.fetchBody
        or not UnitExists or Public(UnitExists("pet")) ~= true or Public(UnitIsDead("pet")) ~= false then
        return flags
    end
    local usable = C_Spell and C_Spell.IsSpellUsable and Public(C_Spell.IsSpellUsable(fetchSpell))
    if usable ~= false then flags = flags + 32768 end
    return flags
end

local taskTargetMacro
local function ConfigureTaskTarget(name)
    if not taskTargetButton or InCombatLockdown() then return end
    local macro = name and "/targetexact " .. name
    if macro == taskTargetMacro then return end
    taskTargetButton:SetAttribute("unit", not macro and "softinteract" or nil)
    taskTargetButton:SetAttribute("macrotext", macro)
    taskTargetButton:SetAttribute("type", macro and "macro" or "target")
    taskTargetMacro = macro
end

local function SetupTaskDialogue(support, plan)
    if forever then
        if not taskDialogueButton then
            taskDialogueButton = CreateFrame("Button", "WowAgentTaskDialogue", UIParent)
            taskDialogueButton:RegisterForClicks("LeftButtonUp")
            taskDialogueButton:SetScript("OnClick", function() WowAgent.RunPreparedQuestAction() end)
        end
    else
        local index, reason = support.WriteMacro("WAQTask", taskDialogueBody)
        if not index then return nil, reason end
        if not taskDialogueButton then
            taskDialogueButton = CreateFrame("Button", "WowAgentTaskDialogue", UIParent, "SecureActionButtonTemplate")
            taskDialogueButton:RegisterForClicks("AnyDown", "AnyUp")
            taskDialogueButton:SetAttribute("type", "macro")
        end
        taskDialogueButton:SetAttribute("macro", index)
        plan.dialogueMacro = index
    end
    SetOverrideBindingClick(taskBindingOwner, false, "9", "WowAgentTaskDialogue", "LeftButton")
    if forever then forever.BindProgression(taskBindingOwner, plan) end
    local suspendBinding = Public(GetBindingAction("F5", true))
    if plan.round and suspendBinding ~= nil and suspendBinding ~= "SITORSTAND" and suspendBinding ~= "PITCHDOWN" then
        if not WowAgentTaskSuspend then
            local button = CreateFrame("Button", "WowAgentTaskSuspend", UIParent)
            button:RegisterForClicks("LeftButtonUp")
            button:SetScript("OnClick", function() WowAgent.SuspendPreparedQuest() end)
        end
        SetOverrideBindingClick(taskBindingOwner, false, "F5", "WowAgentTaskSuspend", "LeftButton")
    end
    return true
end

-- The range check reads item data only. These items have a 5-yard friendly
-- range in Retail (LibRangeCheck-3.0); no item is equipped or used.
local fiveYardItems = {1970, 8149, 15826, 16991, 17117}
local function TargetWithinFiveYards()
    if InCombatLockdown() then return nil end
    if UnitDistanceSquared then
        local ok, distanceSquared, checked = pcall(UnitDistanceSquared, "target")
        distanceSquared, checked = Public(distanceSquared), Public(checked)
        if ok and checked == true and type(distanceSquared) == "number" and distanceSquared >= 0 then
            return distanceSquared <= 25
        end
    end
    if UnitPosition then
        local ok, x, y, z, instance = pcall(UnitPosition, "target")
        local playerOK, px, py, pz, playerInstance = pcall(UnitPosition, "player")
        x, y, z, instance = Public(x), Public(y), Public(z), Public(instance)
        px, py, pz, playerInstance = Public(px), Public(py), Public(pz), Public(playerInstance)
        if ok and playerOK and type(instance) == "number" and instance == playerInstance
            and type(x) == "number" and type(y) == "number" and type(z) == "number"
            and type(px) == "number" and type(py) == "number" and type(pz) == "number" then
            return (x - px)^2 + (y - py)^2 + (z - pz)^2 <= 25
        end
    end
    if not C_Item or not C_Item.IsItemInRange then return nil end
    local outside
    for _, id in ipairs(fiveYardItems) do
        if not C_Item.IsItemDataCachedByID or C_Item.IsItemDataCachedByID(id) then
            local ok, result = pcall(C_Item.IsItemInRange, id, "target")
            result = Public(result)
            if ok and (result == true or result == 1) then return true end
            if ok and (result == false or result == 0) then outside = false end
        end
    end
    return outside
end

local rangeCVar = "SoftTargetInteractRange"
local hardRangeCVar = "SoftTargetInteractRangeIsHard"

local function RestoreNativeRange()
    local saved = WowAgentDB and WowAgentDB.questRangeOriginal
    if not saved or not C_CVar or not C_CVar.SetCVar or not C_CVar.GetCVar then return end
    local restored = true
    for key, value in pairs(saved) do
        local cutoff = key == rangeCVar and 5 or 1
        if (key == rangeCVar or key == hardRangeCVar) and tonumber(C_CVar.GetCVar(key)) == cutoff then
            pcall(C_CVar.SetCVar, key, value)
            if C_CVar.GetCVar(key) ~= value then restored = false end
        end
    end
    if restored then WowAgentDB.questRangeOriginal = nil end
end

local function CloseRoundDialogue()
    if panel == GOSSIP and C_GossipInfo.CloseGossip then
        C_GossipInfo.CloseGossip()
    elseif CloseQuest then
        CloseQuest()
    end
end

local function SetQuestInteraction(plan, id, npc, pickup)
    RestoreNativeRange()
    plan.quest, plan.npcOverride, plan.npc = id, npc, TurnInNPC(id, npc)
    plan.pickup = pickup
    if pickup and pickup.verifyOffer then
        print("WowAgent：后续任务 " .. id .. " 的前置已完成，将到接取 NPC 核对任务列表。")
    end
    local seen = plan.round and plan.round.offers[id]
    if seen and seen.map ~= C_Map.GetBestMapForUnit("player") then seen = nil end
    plan.pickupNPC = plan.automatic and ((seen and seen.npc) or PickupNPC(id) or (pickup and pickup.npc))
    plan.pickupName = seen and seen.npc == plan.pickupNPC and SafeTaskTargetName(seen.name)
    plan.rangeProbeAt, plan.turnInTarget, plan.rejectedNPCs = nil, nil, nil
    plan.targetEvidence, plan.liveTargetEvidence, plan.npcUnmatchedSince = nil, nil, nil
    plan.choiceSeen, plan.choiceOpened, plan.transportSent = nil, nil, nil
    plan.transportStandUntil = nil
    ConfigureTaskTarget(nil)
end

local function RecoverQuestDialogue(plan, id, phase, pickup)
    if phase == OFF then return false end
    if id == plan.quest and ((pickup and (phase == OFFER or phase == DETAIL))
        or (not pickup and (phase == TURN_IN or phase == PROGRESS or phase == REWARD or phase == 7))) then
        return false
    end
    -- The current quest can coexist with other entries in a gossip list.
    local expected = pickup and OFFER or TURN_IN
    for _, quest in ipairs(DialogueQuests()) do
        if quest.id == plan.quest and quest.phase == expected then
            if pickup then
                if quest.index then SelectAvailableQuest(quest.index)
                else C_GossipInfo.SelectAvailableQuest(quest.id) end
            elseif quest.index then SelectActiveQuest(quest.index)
            else C_GossipInfo.SelectActiveQuest(quest.id) end
            return true
        end
    end
    local npc = QuestGiverGUID() or plan.turnInTarget
    if npc then
        plan.rejectedNPCs = plan.rejectedNPCs or {}
        plan.rejectedNPCs[npc] = true
    end
    CloseRoundDialogue()
    return true
end

local function SwitchRoundQuest(plan, nextAction)
    local id, phase = Dialogue()
    local keep = phase == DETAIL and id == nextAction.id and nextAction.stage == ROUND_PICKUP
    for _, quest in ipairs(DialogueQuests()) do
        if quest.id == nextAction.id and ((quest.phase == OFFER and nextAction.stage == ROUND_PICKUP)
            or (quest.phase == TURN_IN and nextAction.stage == ROUND_TURN_IN)) then keep = true end
    end
    if not keep and phase ~= OFF then CloseRoundDialogue() end
    local round = plan.round
    round.stage, round.waitSince, round.next, round.scanRetried = nextAction.stage, nil, nil, nil
    round.immediate, round.acceptingID = nil, nil
    if nextAction.stage == ROUND_DONE then
        local saved, character = SavedRound()
        if saved and not next(round.suspended or {}) then saved[character] = nil end
        return
    end
    if nextAction.anchor then
        round.anchor, round.jobs, round.options = nextAction.anchor, {}, {}
    end
    round.current = nextAction.id
    if plan.quest ~= nextAction.id then
        local quest = nextAction.id
        local options = round.options[quest] or {}
        SetQuestInteraction(plan, quest, options.npc, nextAction.pickup)
        plan.useItem = options.useItem
        plan.item = plan.useItem and QuestItemID(quest) or nil
        if plan.item then SetupTaskClicks(plan) end
        if quest == 90883 and not plan.tutorialMounts and not IsMounted() then PrepareTutorialMount(plan) end
        WowAgent.QuestCommand("track " .. quest)
    end
end

local function ChoiceRoute(plan)
    local data = retail and retail.quests[plan.quest]
    return data and data.choice
end

local function ChoiceTable(value)
    value = Public(value)
    if type(value) == "table" and (not canaccesstable or canaccesstable(value)) then return value end
end

local function QuestChoicePanel(route)
    local info = route.dialog and C_PlayerChoice and C_PlayerChoice.GetCurrentPlayerChoiceInfo
        and ChoiceTable(C_PlayerChoice.GetCurrentPlayerChoiceInfo())
    if info and Public(info.choiceID) == route.dialog then return info end
end

local function QuestChoiceResponse(route, info)
    if not (info and route.response and C_PlayerChoice.SendPlayerChoiceResponse) then return end
    for _, option in ipairs(ChoiceTable(info.options) or {}) do
        option = ChoiceTable(option)
        if option and Public(option.disabledOption) == false then
            for _, button in ipairs(ChoiceTable(option.buttons) or {}) do
                button = ChoiceTable(button)
                if button and Public(button.id) == route.response and Public(button.disabled) == false then
                    return route.response
                end
            end
        end
    end
end

local function QuestTransportOption(route)
    if panel ~= GOSSIP or not route.gossip or CreatureID(QuestGiverGUID()) ~= route.giver
        or not C_GossipInfo.GetOptions or not C_GossipInfo.SelectOption then return end
    for _, option in ipairs(ChoiceTable(C_GossipInfo.GetOptions()) or {}) do
        option = ChoiceTable(option)
        if option and Public(option.gossipOptionID) == route.gossip then return route.gossip end
    end
end

-- Choice channel values are mirrored by ChoiceStep in travel_channel.py.
local function QuestChoiceStep(plan, route)
    if plan.transportStandUntil and GetTime() < plan.transportStandUntil then return 4 end
    local info = QuestChoicePanel(route)
    if info then
        local response = not plan.transportSent and QuestChoiceResponse(route, info)
        if response then return 1, response end
        return 0
    end
    if not plan.transportSent and not plan.choiceOpened then
        local option = QuestTransportOption(route)
        if option then return route.dialog and 2 or 1, nil, option end
        if route.giver and route.point then return 3 end
    end
    return 0
end

function WowAgent.SuspendPreparedQuest()
    local plan = navigation
    if not plan or not plan.automatic or not plan.round or not screen:IsShown()
        or plan.round.stage == ROUND_DONE or InCombatLockdown() or not TaskClickMatches("F5", "WowAgentTaskSuspend")
        or (IsFlying and IsFlying()) or (IsFalling and IsFalling())
        or (UnitIsDeadOrGhost and Public(UnitIsDeadOrGhost("player")) ~= false)
        or (UnitCastingInfo and Public(UnitCastingInfo("player")))
        or (UnitChannelInfo and Public(UnitChannelInfo("player"))) then return end
    local round = plan.round
    round.suspended = round.suspended or {}
    if round.suspended[plan.quest] then return end
    round.suspended[plan.quest] = QuestPhase(plan.quest)
    round.immediate, round.acceptingID, round.waitSince = nil, nil, nil
    round.suspendEpoch = ((round.suspendEpoch or 0) + 1) % 8
    CloseRoundDialogue()
end

function WowAgent.RunPreparedQuestAction()
    local plan = navigation
    if not plan or not plan.automatic or InCombatLockdown() or not screen:IsShown() then return end
    local route = ChoiceRoute(plan)
    if type(route) == "table" and C_Map.GetBestMapForUnit("player") == route.fromMap
        and QuestPhase(plan.quest) == 2 and not IsMounted() then
        local step, response, option = QuestChoiceStep(plan, route)
        if step == 1 then plan.choiceSeen, plan.transportSent = plan.quest, GetTime() end
        if response then
            C_PlayerChoice.SendPlayerChoiceResponse(response)
        elseif option then
            if step == 2 then plan.choiceOpened = true end
            C_GossipInfo.SelectOption(option)
        end
        if step == 1 or step == 2 or step == 4 then return end
    end
    if plan.round then
        local questPhase = QuestPhase(plan.quest)
        local stage = plan.round.stage
        if RoundNeedsAdvance(plan.round, questPhase) then
            local action = NextRoundAction(plan, questPhase)
            if action and not action.blocked then SwitchRoundQuest(plan, action) end
        elseif (stage == ROUND_PICKUP and questPhase == 1)
            or ((stage == ROUND_TURN_IN or plan.round.immediate) and questPhase == 3) then
            RememberRoundOffers(plan.round)
            local id, phase = Dialogue()
            if plan.round.immediate and questPhase == 3 and phase ~= OFF and id ~= plan.quest then
                return CloseRoundDialogue()
            end
            if RecoverQuestDialogue(plan, id, phase, questPhase == 1) then return end
            if stage == ROUND_PICKUP and questPhase == 1 and id == plan.quest
                and (phase == OFFER or phase == DETAIL) then plan.round.acceptingID = id end
            WowAgent.QuestAction(plan.quest)
        end
        return
    end
    local id, phase = Dialogue()
    local pickup
    if AwaitingFollowup(plan) and C_QuestLog.IsQuestFlaggedCompleted(plan.quest) then
        if phase == DETAIL or phase == BLOCKED then
            local offer = FindFollowup(phase, id)
            if offer then pickup = {id = offer} end
        elseif phase == OFF and plan.nextPickupID then
            pickup = NearbyQuest(plan.nextPickupID) or KnownFollowup(plan.nextPickupID, nil, plan.quest)
        end
    end
    if pickup and not C_QuestLog.IsOnQuest(pickup.id) and not C_QuestLog.IsQuestFlaggedCompleted(pickup.id) then
        local previous = plan.quest
        id = pickup.id
        SetQuestInteraction(plan, id, nil, pickup.map and pickup or nil)
        plan.rewardAt, plan.nextPickupID = nil, nil
        plan.rewardNPC = nil
        plan.item, plan.useItem = nil, nil
        WowAgent.QuestCommand("track " .. id)
        print("WowAgent：任务 " .. previous .. " 已交付，已切换到后续任务 " .. id .. "。")
        return
    end
    if phase == REWARD and id == plan.quest then
        plan.rewardAt, plan.rewardNPC = GetTime(), QuestGiverGUID()
        local map = C_Map.GetBestMapForUnit("player")
        if map and C_QuestLine.RequestQuestLinesForMap then C_QuestLine.RequestQuestLinesForMap(map) end
    end
    local questPhase = QuestPhase(plan.quest)
    if (questPhase == 1 or questPhase == 3) and RecoverQuestDialogue(plan, id, phase, questPhase == 1) then return end
    WowAgent.QuestAction(plan.quest)
end

local function NativeFiveYardRange(plan, soft, target)
    if not C_CVar or not C_CVar.SetCVar or not C_CVar.GetCVar or InCombatLockdown() then return nil end
    if not plan.rangeProbeAt then
        local range, hard = C_CVar.GetCVar(rangeCVar), C_CVar.GetCVar(hardRangeCVar)
        if not tonumber(range) or not tonumber(hard) then return nil end
        WowAgentDB = WowAgentDB or {}
        WowAgentDB.questRangeOriginal = WowAgentDB.questRangeOriginal or {[rangeCVar] = range, [hardRangeCVar] = hard}
        pcall(C_CVar.SetCVar, rangeCVar, 5)
        pcall(C_CVar.SetCVar, hardRangeCVar, 1)
        plan.rangeProbeAt = GetTime() + .6
        return nil
    end
    if tonumber(C_CVar.GetCVar(rangeCVar)) ~= 5 or tonumber(C_CVar.GetCVar(hardRangeCVar)) ~= 1 then
        RestoreNativeRange()
        return nil
    end
    -- Soft-target identity can still reflect the old range on the first frame.
    if GetTime() >= plan.rangeProbeAt then return soft ~= nil and soft == target end
end

local function InteractionFlags(plan, phase, sky, nearMarker, objectiveNPC)
    if not plan.automatic then return 0 end
    local prepared = GameTaskOptions(false) and TaskClickMatches("K", "WowAgentTaskTarget")
        and Public(GetBindingAction("F", true)) == "INTERACTTARGET"
        and Public(GetBindingAction("TAB", true)) == "TARGETNEARESTENEMY"
        and TaskClickMatches("9", "WowAgentTaskDialogue")
    if not sky and not forever then
        prepared = prepared and plan.dialogueMacro
            and WowAgent.LayoutSupport.ReadMacroBody(plan.dialogueMacro) == taskDialogueBody
    end
    local flags = prepared and 1 or 0
    local expected = objectiveNPC or (phase == 3 and plan.npc) or (phase == 1 and plan.pickupNPC)
    if expected then flags = flags + 32 end
    local itemBound = plan.item and TaskClickMatches("J", "WowAgentTaskItem")
    if itemBound then flags = flags + 16 end
    if itemBound and plan.useItem and phase == 2 and QuestItemID(plan.quest) == plan.item then
        flags = flags + 64
    end
    if not prepared or not UnitGUID or not UnitIsDead
        or (GetCursorInfo and GetCursorInfo()) or (SpellIsTargeting and SpellIsTargeting()) then return flags end
    if phase ~= plan.npcEvidencePhase then
        plan.npcEvidencePhase = phase
        plan.rangeProbeAt, plan.turnInTarget, plan.rejectedNPCs = nil, nil, nil
        plan.targetEvidence, plan.liveTargetEvidence, plan.npcUnmatchedSince = nil, nil, nil
        RestoreNativeRange()
    end
    local soft, target = Public(UnitGUID("softinteract")), Public(UnitGUID("target"))
    local function Rejected(guid)
        return (phase == 1 or phase == 3) and plan.rejectedNPCs and plan.rejectedNPCs[guid]
    end
    local function Matches(unit, guid)
        return expected and not Rejected(guid) and (not objectiveNPC or (nearMarker and UnitCanAttack
            and Public(UnitCanAttack("player", unit)) == false)) and MatchesNPC(unit, guid, expected)
    end
    local function LiveName(unit, guid)
        if nearMarker and guid and guid:match("^Creature%-") and not Rejected(guid)
            and Public(UnitIsDead(unit)) == false and UnitCanAttack
            and Public(UnitCanAttack("player", unit)) == false and UnitName then
            return SafeTaskTargetName(Public(UnitName(unit)))
        end
    end
    if (phase == 3 or phase == 1 or objectiveNPC) and (expected or nearMarker) then
        local softMatches = Matches("softinteract", soft)
        local evidence = plan.targetEvidence
        if expected and UnitName then
            local unit, guid = "softinteract", soft
            if not softMatches and nearMarker then
                -- Nearby living NPCs can appear as a friendly target before
                -- they become the game's current interaction target.
                unit, guid = "softfriend", Public(UnitGUID("softfriend"))
            end
            if Matches(unit, guid) and (not evidence or evidence.expected ~= expected or evidence.guid ~= guid) then
                local name = SafeTaskTargetName(Public(UnitName(unit)))
                if name then
                    evidence = {expected = expected, guid = guid, name = name}
                    plan.targetEvidence = evidence
                end
            end
        end
        local observed = expected and evidence and evidence.expected == expected
            and (not plan.rejectedNPCs or not plan.rejectedNPCs[evidence.guid])
        local exactName = observed and evidence.name
        local knownName = expected and ((phase == 1 and plan.pickupName) or KnownTaskTargetName(expected))
        if not exactName and nearMarker and (phase ~= 1 or not plan.rejectedNPCs) then
            exactName = knownName
        end
        local observedTarget = observed and evidence.guid == target
        local selected = target and Matches("target", target)
            and (target == plan.turnInTarget or target == soft or nearMarker or observedTarget)
        local close, probingLiveRange
        if (phase == 1 or phase == 3) and nearMarker and not (softMatches or selected) then
            plan.npcUnmatchedSince = plan.npcUnmatchedSince or GetTime()
            local live = plan.liveTargetEvidence
            local liveSelected = live and target == live.guid and LiveName("target", target) == live.name
            local softName = LiveName("softinteract", soft)
            if liveSelected then close = TargetWithinFiveYards() end
            local softInRange
            if liveSelected or plan.rangeProbeAt then
                -- Keep the hard range active across a retarget. Its positive
                -- receipt can displace a stale selection near the same pin.
                softInRange = NativeFiveYardRange(plan, soft, soft)
                probingLiveRange = true
                if softInRange == true and softName then
                    if liveSelected and soft == target then
                        close = true
                    elseif close ~= true then
                        liveSelected = false
                    end
                end
                if liveSelected and close == nil and softInRange ~= nil then close = false end
            end
            if not liveSelected then
                live = nil
                -- Give the configured target a chance first. A same-name spawn
                -- can be probed immediately. A different name needs live evidence
                -- and a short window without any configured match.
                if softName and (not exactName or softName == knownName or softInRange == true
                    or GetTime() - plan.npcUnmatchedSince >= 6) then
                    live = {guid = soft, name = softName}
                end
            end
            plan.liveTargetEvidence = live
            if live then
                exactName = live.name
                selected = target == live.guid and LiveName("target", target) == live.name
            end
        else
            plan.liveTargetEvidence, plan.npcUnmatchedSince = nil, nil
        end
        ConfigureTaskTarget(exactName or nil)
        if softMatches or (nearMarker and exactName) then flags = flags + 2 end
        if selected then
            plan.turnInTarget = target
            flags = flags + 4
            if not probingLiveRange then
                close = TargetWithinFiveYards()
                if close == nil then close = NativeFiveYardRange(plan, soft, target) end
            end
            if close ~= nil then flags = flags + 256 end
            if close then flags = flags + 128 end
            return flags
        end
        plan.turnInTarget = nil
        if probingLiveRange then return flags end
        plan.rangeProbeAt = nil
    else
        plan.rangeProbeAt, plan.turnInTarget = nil, nil
        plan.liveTargetEvidence, plan.npcUnmatchedSince = nil, nil
        ConfigureTaskTarget(WowAgent.QuestEncounter and WowAgent.QuestEncounter.Name(plan.quest, phase))
        if phase == 2 and soft and target == soft and Public(UnitIsDead("softinteract")) == true then
            flags = flags + 8
        end
    end
    RestoreNativeRange()
    return flags
end

local function ClearTaskBindings()
    camera.cancel()
    if not taskBindingOwner then return end
    if InCombatLockdown() then
        taskBindingOwner:RegisterEvent("PLAYER_REGEN_ENABLED")
    else
        ClearOverrideBindings(taskBindingOwner)
        taskBindingOwner:UnregisterEvent("PLAYER_REGEN_ENABLED")
    end
end

local function ApplyTaskBindings()
    if not SetOverrideBinding or not ClearOverrideBindings then return false end
    if not taskBindingOwner then
        taskBindingOwner = CreateFrame("Frame")
        taskBindingOwner:SetScript("OnEvent", ClearTaskBindings)
    end
    for _, binding in ipairs(taskBindings) do
        local key, action = unpack(binding)
        if Public(GetBindingAction(key, true)) ~= action
            and not pcall(SetOverrideBinding, taskBindingOwner, false, key, action) then
            ClearTaskBindings()
            return false
        end
    end
    return true
end

function WowAgent.ReportQuestBindings()
    for _, binding in ipairs(taskBindings) do
        local key, action, label = unpack(binding)
        local current = Public(GetBindingAction(key, true))
        print("WowAgent：" .. key .. " " .. label .. " · "
            .. (current == action and "已核对" or "当前 " .. ((current and current ~= "") and current or "未绑定")))
    end
    for _, entry in ipairs({{"L", "WowAgentTaskFetch", "拾回"}, {"O", "WowAgentTaskSweep", "继续拾回附近掉落"}}) do
        print("WowAgent：" .. entry[1] .. " " .. entry[3] .. " · "
            .. (TaskClickMatches(entry[1], entry[2]) and "已核对" or "未配置（需已学会拾回）"))
    end
end

local skyKeys, EmptyAuxSlot

local function DescentBinding()
    -- Codes in navigation metadata v2 are mirrored in travel_channel.py.
    for _, key in ipairs({GetBindingKey("SITORSTAND")}) do
        if GetBindingAction(key, true) == "SITORSTAND" then
            if key == "UP" then return key, 1 end
            if key == "DOWN" then return key, 2 end
            local number = tonumber(key:match("^F(%d+)$"))
            if number and number >= 1 and number <= 12 then return key, number + 2 end
            if key:match("^[A-Z]$") and key ~= "F" and key ~= "S" and key ~= "W" then
                return key, key:byte()
            end
        end
    end
    local code = navigation and navigation.skyDescent
    local key = code and skyKeys[code]
    local button = code and navigation.skySpareButtons[code]
    if key and Public(GetBindingAction(key, true)) == "SITORSTAND" and (not button or EmptyAuxSlot(button)) then
        if code <= 12 then return key, code + 2 end
        return key, key:byte()
    end
end

local function ClearSkyBindings()
    if not skyButton then return end
    if InCombatLockdown() then
        skyButton:RegisterEvent("PLAYER_REGEN_ENABLED")
    else
        ClearOverrideBindings(skyButton)
        skyButton:UnregisterEvent("PLAYER_REGEN_ENABLED")
    end
end

-- Protocol v4 adds unused letter keys after the twelve function keys.
skyKeys = {}
for number = 1, 12 do skyKeys[number] = "F" .. number end
for key in ("ABCDEGHIJKLMNOPQRTUVXYZ"):gmatch(".") do skyKeys[#skyKeys + 1] = key end
skyKeys[#skyKeys + 1] = "0"
local skyClick = "CLICK WowAgentSkyDismount:LeftButton"

EmptyAuxSlot = function(button)
    local action = _G["ActionButton" .. button]
    local slot = action and Public(action.action)
    local hasAction = C_ActionBar and C_ActionBar.HasAction or HasAction
    return slot and hasAction and Public(hasAction(slot)) == false
end

local function PrepareSkyBindings(plan, support)
    if not plan.fly then return "没有已选的飞行坐骑。" end
    if not SetOverrideBinding or not SetOverrideBindingClick or not ClearOverrideBindings
        or not support.BoundSlot or not support.ReadAction or not support.ReadMacroBody or not GetMacroInfo then
        return "客户端缺少临时键位接口。"
    end
    local kind, index = support.ReadAction(support.BoundSlot(8))
    local name = kind == "macro" and index and Public(GetMacroInfo(index))
    if not name then return "无法读取数字键 8 的下马宏名称。" end
    local free, spare, spareButtons, existingPitch = {}, {}, {}, nil
    -- Prefer function keys and reuse a supported native pitch binding when present.
    for pass = 1, 2 do
        local first, last = pass == 1 and 12 or #skyKeys, pass == 1 and 1 or 13
        for code = first, last, -1 do
            local binding = GetBindingAction(skyKeys[code], true)
            if not secret(binding) then
                if binding == nil or binding == "" then free[#free + 1] = code end
                if binding == "PITCHDOWN" and not existingPitch then existingPitch = code end
                local button = type(binding) == "string" and binding:match("^ACTIONBUTTON(1[012])$")
                if button and EmptyAuxSlot(button) then
                    spare[#spare + 1], spareButtons[code] = code, button
                end
            end
        end
    end
    for _, code in ipairs(spare) do free[#free + 1] = code end
    local pitch = existingPitch or free[1]
    local dismount = free[existingPitch and 1 or 2]
    if not pitch or not dismount then return "找不到可用的俯冲与下马键。" end
    if not skyButton then
        skyButton = CreateFrame("Button", "WowAgentSkyDismount", UIParent, "SecureActionButtonTemplate")
        skyButton:RegisterForClicks("AnyDown", "AnyUp")
        skyButton:SetScript("OnEvent", ClearSkyBindings)
    end
    skyButton:SetAttribute("type", "macro")
    skyButton:SetAttribute("macro", name)
    if not existingPitch then SetOverrideBinding(skyButton, false, skyKeys[pitch], "PITCHDOWN") end
    SetOverrideBindingClick(skyButton, false, skyKeys[dismount], "WowAgentSkyDismount", "LeftButton")
    plan.skyPitch, plan.skyDismount, plan.skyMacro = pitch, dismount, name
    plan.skySpareButtons = spareButtons
    -- Keep a native descent key for steady flight, even when the normal layout has none.
    if not DescentBinding() then
        for index = existingPitch and 2 or 3, #free do
            local code = free[index]
            if skyKeys[code] ~= "0" then
                SetOverrideBinding(skyButton, false, skyKeys[code], "SITORSTAND")
                plan.skyDescent = code
                break
            end
        end
    end
end

local function SkyBindings()
    local pitch, dismount = navigation.skyPitch, navigation.skyDismount
    if not pitch then return end
    for _, code in ipairs({pitch, dismount}) do
        local button = navigation.skySpareButtons[code]
        if button and not EmptyAuxSlot(button) then
            return nil, nil, "备用键 " .. skyKeys[code] .. " 对应的动作格已占用。"
        end
    end
    if Public(GetBindingAction(skyKeys[pitch], true)) ~= "PITCHDOWN" then
        return nil, nil, "客户端未确认俯冲键 " .. skyKeys[pitch] .. "。"
    end
    local action = Public(GetBindingAction(skyKeys[dismount], true))
    if type(action) ~= "string" or action:lower() ~= skyClick:lower() then
        return nil, nil, "客户端未确认下马键 " .. skyKeys[dismount] .. "（" .. tostring(action) .. "）。"
    end
    local index = GetMacroIndexByName(navigation.skyMacro)
    local body = index and index > 0 and WowAgent.LayoutSupport.ReadMacroBody(index, navigation.skyMacro)
    if body ~= navigation.bodies[3] then
        return nil, nil, "无法核对下马宏 " .. navigation.skyMacro .. " 的内容。"
    end
    return pitch, dismount
end

local skySpell = {
    [372608] = 1, [1227914] = 1,   -- Surge Forward
    [425782] = 2, [1227950] = 2,   -- Second Wind
    [361584] = 3, [447981] = 3, [1227921] = 3, -- Whirling Surge
    [372610] = 4, [1227916] = 4,   -- Skyward Ascent
    [403092] = 5,                 -- Aerial Halt
}
local skyCast, skyCastEpoch = 0, 0

local function SkySpellAction(spell)
    if not spell then return end
    local action = skySpell[spell]
    if not action and C_Spell and C_Spell.GetBaseSpell then
        action = skySpell[Public(C_Spell.GetBaseSpell(spell))]
    end
    return action
end

local function SkyActions()
    local bar, support = C_ActionBar, WowAgent.LayoutSupport
    local keys, shared = {0, 0, 0, 0, 0}, nil
    if not bar or not bar.IsUsableAction or not support.BoundSlot or not support.ReadAction then return keys end
    for key = 1, 9 do
        local slot = support.BoundSlot(key)
        local kind, spell = support.ReadAction(slot)
        local action = kind == "spell" and SkySpellAction(spell)
        if action then
            local charges = bar.GetActionCharges and Public(bar.GetActionCharges(slot))
            local count = charges and Public(charges.currentCharges)
            if count == nil and C_Spell and C_Spell.GetSpellCharges then
                charges = Public(C_Spell.GetSpellCharges(spell))
                count = charges and Public(charges.currentCharges)
            end
            local chargeSkill = action == 1 or action == 4
            if chargeSkill and type(count) == "number" and count >= 0 and count <= 6 then
                shared = shared and math.min(shared, count) or count
            end
            local cooldown = bar.GetActionCooldown and Public(bar.GetActionCooldown(slot))
            local duration = cooldown and Public(cooldown.duration)
            if duration == nil and C_Spell and C_Spell.GetSpellCooldown then
                cooldown = Public(C_Spell.GetSpellCooldown(spell))
                duration = cooldown and Public(cooldown.duration)
            end
            local ready = type(count) == "number" and count > 0
                or (not chargeSkill and count == nil and duration == 0)
            if (action == 3 or action == 5) and duration ~= 0 then ready = false end
            if keys[action] == 0 and ready and Public(bar.IsUsableAction(slot)) == true then
                keys[action] = key
            end
        end
    end
    return keys, shared
end

local function MovementKeys()
    for _, binding in ipairs(movementBindings) do
        local key, action, label = unpack(binding)
        if Public(GetBindingAction(key, true)) ~= action then
            return false, "请在游戏按键设置中把 " .. key .. " 绑定为「" .. label .. "」。"
        end
    end
    return true
end

local function NavigationKeys(sky)
    local support = WowAgent.LayoutSupport
    if not support or not support.ActionMatches then return false end
    if not sky then
        for kind, key in ipairs(travelKeys) do
            local body = navigation.bodies[kind]
            if body and not support.ActionMatches(key, nil, body) then
                return false, "数字键 " .. key .. " 的寻路宏未通过核对，请重新准备。"
            end
        end
    end
    return MovementKeys()
end

local function TutorialButtonForKey(key)
    local binding = Public(GetBindingAction(tostring(key), true))
    local index = binding and binding:match("^ACTIONBUTTON(%d+)$")
    return index and _G["ActionButton" .. index]
end

local function NativeTutorialMount(button, mounts)
    local bar = C_ActionBar
    if not button or not button:IsVisible() or not button:IsMouseEnabled() or not bar
        or not bar.IsUsableAction then return false end
    local slot = Public(button.action)
    local kind, id = WowAgent.LayoutSupport.ReadAction(slot)
    return kind == "summonmount" and mounts[id] and Public(bar.IsUsableAction(slot)) == true
end

local function FindTutorialMount(plan)
    local mounts = plan.tutorialMounts
    if not mounts then return end
    for key = 2, 4 do
        local button = TutorialButtonForKey(key)
        if NativeTutorialMount(button, mounts) then return button, key end
    end
    local found, frames = nil, ActionBarButtonEventsFrame
    if frames and frames.ForEachFrame then
        frames:ForEachFrame(function(button)
            if not found and NativeTutorialMount(button, mounts) then found = button end
        end)
    end
    return found
end

PrepareTutorialMount = function(plan)
    local api, bar = C_MountJournal, C_ActionBar
    if not api or not api.GetCollectedDragonridingMounts then return end
    plan.tutorialMounts = {}
    for _, id in ipairs(api.GetCollectedDragonridingMounts()) do plan.tutorialMounts[id] = true end
    local existing, key = FindTutorialMount(plan)
    if key then return end
    local place = bar and bar.PutActionInSlot or PlaceAction
    if not bar or not bar.HasAction or not api.GetNumDisplayedMounts or not api.GetDisplayedMountID
        or not api.Pickup or not place or not ClearCursor or not GetCursorInfo
        or GetCursorInfo() or InCombatLockdown() then return end
    local display
    for i = 1, api.GetNumDisplayedMounts() do
        local id = api.GetDisplayedMountID(i)
        local mount = plan.tutorialMounts[id] and Mount(id)
        if mount and mount.usable then
            display = display or i
            if id == plan.fly then display = i; break end
        end
    end
    if not display then return end
    for candidate = 2, 4 do
        local button = TutorialButtonForKey(candidate)
        local slot = button and Public(button.action)
        if slot and button:IsVisible() and Public(bar.HasAction(slot)) == false then
            pcall(api.Pickup, display)
            if Public(GetCursorInfo()) == "mount" then
                if not InCombatLockdown() and Public(button.action) == slot
                    and Public(bar.HasAction(slot)) == false then pcall(place, slot) end
                if Public(GetCursorInfo()) == "mount" then ClearCursor() end
            end
            if NativeTutorialMount(button, plan.tutorialMounts) then
                print("WowAgent：驭空坐骑原生图标已放到数字键 " .. candidate .. "。")
            end
            return
        end
    end
    if not existing and plan.quest == 90883 then
        print("WowAgent：请在主动作栏放一个原生驭空坐骑图标，或空出数字键 2–4 的一个位置。")
    end
end

local function MountTutorialClick(plan)
    if IsMounted() or InCombatLockdown() or (GetCursorInfo and GetCursorInfo())
        or (WowAgentControlPanel and WowAgentControlPanel:IsShown())
        or (WorldMapFrame and WorldMapFrame:IsShown()) then return 0, 0 end
    local button, key = FindTutorialMount(plan)
    if not button then return 0, 0 end
    local x, y = button:GetCenter()
    x, y = Public(x), Public(y)
    if not x or not y then return 0, 0 end
    local scale = button:GetEffectiveScale() / UIParent:GetEffectiveScale()
    x = (x * scale - UIParent:GetLeft()) / UIParent:GetWidth()
    y = (UIParent:GetTop() - y * scale) / UIParent:GetHeight()
    if not Coordinate(x) or not Coordinate(y) then return 0, 0 end
    return math.floor(x * 1023 + .5) * 1024 + math.floor(y * 1023 + .5), key or 0
end

-- Match the current character's unfinished objective in native tooltip data.
-- Some builds omit the quest ID from tooltip lines, so require the title
-- and exact objective text in that case.
local function ObjectiveLabel(text)
    if type(text) ~= "string" then return nil end
    text = text:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", "")
    return text:gsub("^%s*%d+%s*/%s*%d+%s*", ""):gsub("%s*%d+%s*/%s*%d+%s*$", "")
        :gsub("：%s*$", ""):gsub(":%s*$", "")
        :match("^%s*(.-)%s*$")
end

local function TalkObjectiveNPC(objective)
    if not objective then return end
    local text = ObjectiveLabel(Public(objective.text))
    if not text then return end
    local locale = GetLocale and GetLocale()
    if locale == "zhCN" or locale == "zhTW" then
        text = text:gsub("。$", "")
        return SafeTaskTargetName(text:match("^与(.+)交谈$") or text:match("^與(.+)交談$"))
    elseif locale == "enUS" or locale == "enGB" then
        text = text:gsub("%.$", "")
        return SafeTaskTargetName(text:match("^Speak with (.+)$") or text:match("^Speak to (.+)$")
            or text:match("^Talk to (.+)$"))
    end
end

local function TooltipMatchesQuest(data, id, objectiveText, resource)
    local lines = data and Public(data.lines)
    local types = Enum.TooltipDataLineType
    if not lines or not types then return false end
    local title = Public(C_QuestLog.GetTitleForQuestID(id))
    local belongs, otherPlayer = false, false
    local label = ObjectiveLabel(objectiveText)
    if not resource and (not label or label == "") then return false end
    for _, line in ipairs(lines) do
        local kind = Public(line.type)
        if kind == types.QuestTitle then
            local questID = Public(line.questID)
            belongs = questID and questID == id
                or not questID and title and Public(line.leftText) == title
            otherPlayer = false
        elseif kind == types.QuestPlayer then
            local owner = Public(line.leftText)
            otherPlayer = owner and UnitName and owner ~= Public(UnitName("player"))
        elseif kind == types.QuestObjective and belongs and not otherPlayer and Public(line.completed) == false
            and (resource or ObjectiveLabel(Public(line.leftText)) == label) then
            return true
        end
    end
    return false
end

local function DisplayedWorldObjectMatches(objectName)
    local tooltip = GameTooltip
    if not objectName or not tooltip or not tooltip:IsShown() or not tooltip.GetPrimaryTooltipInfo then
        return false
    end
    local ok, info = pcall(tooltip.GetPrimaryTooltipInfo, tooltip)
    info = ok and Public(info)
    -- Blizzard clears the world cursor info when the mouse leaves the object,
    -- even if its text is still visible while fading out.
    if type(info) ~= "table" or Public(info.getterName) ~= "GetWorldCursor" then return false end
    local text = GameTooltipTextLeft1 and Public(GameTooltipTextLeft1:GetText())
    return ObjectiveLabel(text) == objectName
end

local function GenericObjective(id, objectives, objective, index, objectName, knownObject, objectCreature, resourceProfile)
    if resourceProfile and resourceProfile.combatNPC and objective and Public(objective.type) == "monster" then
        local target = UnitGUID and CreatureID(Public(UnitGUID("target")))
        return 1, target == resourceProfile.combatNPC and target or 0
    end
    if IsResourceObjective(id, objectives, index) then
        local target = UnitGUID and CreatureID(Public(UnitGUID("target")))
        if target and UnitCanAttack and Public(UnitCanAttack("player", "target")) == true then
            local locale = GetLocale and GetLocale()
            local names = resourceProfile and resourceProfile.resourceNames
            names = names and names[locale == "enGB" and "enUS" or locale]
            local name = names and UnitName and Public(UnitName("target"))
            if name and names[name] then return 1, target end
            local api = C_TooltipInfo and C_TooltipInfo.GetUnit
            if api then
                local ok, tooltip = pcall(api, "target")
                if ok and TooltipMatchesQuest(Public(tooltip), id, nil, true) then return 1, target end
            end
        end
        return 1, 0
    end
    if not objectives then return 0, 0 end
    if #objectives == 0 then return 3, 0 end
    if not objective then return 0, 0 end
    local kind, objectiveText = Public(objective.type), Public(objective.text)
    if type(objectiveText) ~= "string" or objectiveText == "" then return 0, 0 end
    if kind == "location" then return 3, 0 end
    if kind == "event" then
        local text = ObjectiveLabel(objectiveText)
        if text and (text:match("^Reach ") or text:match("^Arrive at ") or text:match("^Travel to ")
            or text:match("^到达") or text:match("^抵达") or text:match("^前往")) then return 3, 0 end
    end
    local precise = knownObject or kind == "object"
    if not precise and (kind == "monster" or kind == "item") then
        local guid = UnitGUID and Public(UnitGUID("target"))
        local target = CreatureID(guid)
        local api = C_TooltipInfo and C_TooltipInfo.GetUnit
        if target and api and UnitCanAttack and Public(UnitCanAttack("player", "target")) == true then
            local ok, data = pcall(api, "target")
            if ok and TooltipMatchesQuest(Public(data), id, objectiveText) then return 1, target end
        end
    end
    if not precise and kind ~= "item" and kind ~= "monster" then return 0, 0 end
    local default = precise and 2 or 1
    local api = C_TooltipInfo and C_TooltipInfo.GetWorldCursor
    if not GetCursorPosition or (WorldMapFrame and WorldMapFrame:IsShown())
        or (WowAgentControlPanel and WowAgentControlPanel:IsShown()) then return default, 0, precise end
    local matched = objectCreature and UnitGUID and CreatureID(Public(UnitGUID("mouseover"))) == objectCreature
        and Public(UnitIsDead("mouseover")) == false
    if api and not matched then
        local ok, data = pcall(api)
        data = ok and Public(data)
        if data and Enum.TooltipDataType and Public(data.type) == Enum.TooltipDataType.Object then
            local lines = Public(data.lines)
            local first = lines and Public(lines[1])
            matched = (objectName and first and objectName == ObjectiveLabel(Public(first.leftText)))
                or TooltipMatchesQuest(data, id, objectiveText)
        end
    end
    if not matched and not DisplayedWorldObjectMatches(objectName) then return default, 0, precise end
    local x, y = GetCursorPosition()
    x, y = Public(x), Public(y)
    if not x or not y then return 2, 0, true end
    local scale = UIParent:GetEffectiveScale()
    x = (x / scale - UIParent:GetLeft()) / UIParent:GetWidth()
    y = (UIParent:GetTop() - y / scale) / UIParent:GetHeight()
    if not Coordinate(x) or not Coordinate(y) then return 2, 0, true end
    return 2, math.floor(x * 1023 + .5) * 1024 + math.floor(y * 1023 + .5), true
end

local function ObjectiveObjectPoint(name, player, creature)
    local api = C_VignetteInfo
    if (not name and not creature) or not player or not api or not api.GetVignettes
        or not api.GetVignetteInfo or not api.GetVignettePosition then return end
    local best, bestX, bestY = math.huge, nil, nil
    for _, guid in ipairs(api.GetVignettes() or {}) do
        guid = Public(guid)
        local info = guid and Public(api.GetVignetteInfo(guid))
        local object = info and Public(info.objectGUID)
        local matches = (creature and CreatureID(object) == creature)
            or (name and type(object) == "string" and object:match("^GameObject%-") and Public(info.name) == name)
        if matches
            and (Public(info.onMinimap) or Public(info.onWorldMap)) then
            local position = Public(api.GetVignettePosition(guid, player.map))
            if position then
                local x, y = position:GetXY()
                x, y = Public(x), Public(y)
                if Coordinate(x) and Coordinate(y) then
                    local distance = ((x - player.x) * player.width)^2 + ((y - player.y) * player.height)^2
                    if distance < best then best, bestX, bestY = distance, x, y end
                end
            end
        end
    end
    if bestX then return player.map, bestX, bestY end
end

local function APRObjectivePoint(plan, map)
    local route = WowAgent.APRQuestData and WowAgent.APRQuestData[plan.quest]
    if not route or route.map ~= map or not C_Map.GetWorldPosFromMapPos or not CreateVector2D then return end
    local kind, progress = Objective(plan.quest, true)
    if kind ~= 2 or progress % 1024 ~= #route.points then return end
    local point = route.points[math.floor(progress / 1024) + 1]
    if not point then return end
    if not plan.aprMapBounds then
        local firstMap, first = C_Map.GetWorldPosFromMapPos(map, CreateVector2D(0, 0))
        local lastMap, last = C_Map.GetWorldPosFromMapPos(map, CreateVector2D(1, 1))
        firstMap, first, lastMap, last = Public(firstMap), Public(first), Public(lastMap), Public(last)
        if not firstMap or firstMap ~= lastMap or not first or not last then return end
        -- APR x is world Y, which increases along the map's horizontal axis.
        local left, top = Public(first.y), Public(first.x)
        local right, bottom = Public(last.y), Public(last.x)
        if not left or not top or not right or not bottom or left == right or top == bottom then return end
        plan.aprMapBounds = {left = left, top = top, width = right - left, height = bottom - top}
    end
    local bounds = plan.aprMapBounds
    local x, y = (point[1] - bounds.left) / bounds.width, (point[2] - bounds.top) / bounds.height
    if Coordinate(x) and Coordinate(y) then return map, x, y, true end
end

local function NavigationDestination(plan, phase, map)
    local id, round = plan.quest, plan.round
    if plan.automatic and phase == 1 then
        local point = plan.pickup or RetailNPCPoint(id, "pickup", map)
        if point then return point.map, point.x, point.y end
    end
    if plan.automatic and phase == 3 and not plan.npcOverride then
        local point = RetailNPCPoint(id, "turnIn", map)
        if point then return point.map, point.x, point.y end
    end
    if plan.automatic and phase == 2 then
        local choice = ChoiceRoute(plan)
        if choice and choice.fromMap == map and choice.point then
            return map, choice.point[1] / 100, choice.point[2] / 100
        end
        local data = RetailQuest(id, map)
        if data and data.arrival then
            local point = RetailNPCPoint(id, "turnIn", map)
            if point then return point.map, point.x, point.y end
        end
        local routeMap, x, y, guided = APRObjectivePoint(plan, map)
        if routeMap then return routeMap, x, y, guided end
    end
    local targetMap, x, y = QuestDestination(id, map)
    if not targetMap and forever and phase == 2 then
        local data = RetailQuest(id, map)
        local point = data and data.objectivePoint
        if point then return map, point[1] / 100, point[2] / 100 end
    end
    if not targetMap and round and round.immediate and phase == 3 then
        local point = round.offers[id] or plan.pickup
        if point then return point.map, point.x, point.y end
    end
    return targetMap, x, y
end

local function MinimapGeometry()
    if not Minimap or not Minimap:IsVisible() or not C_Minimap or not C_Minimap.GetViewRadius then return end
    local radius = Public(C_Minimap.GetViewRadius())
    local x, y = Minimap:GetCenter()
    x, y = Public(x), Public(y)
    local rotating = C_CVar and C_CVar.GetCVar and Public(C_CVar.GetCVar("rotateMinimap"))
    if C_Minimap.IsRotateMinimapIgnored and Public(C_Minimap.IsRotateMinimapIgnored()) == true then rotating = "0" end
    if type(radius) ~= "number" or radius < 1 or radius > 1023 or not x or not y
        or (rotating ~= "0" and rotating ~= "1") then return end
    local scale = Minimap:GetEffectiveScale() / UIParent:GetEffectiveScale()
    local width, height = UIParent:GetWidth(), UIParent:GetHeight()
    x, y = (x * scale - UIParent:GetLeft()) / width, (UIParent:GetTop() - y * scale) / height
    local w, h = Minimap:GetWidth() * scale / width, Minimap:GetHeight() * scale / height
    if not Coordinate(x - w / 2) or not Coordinate(x + w / 2)
        or not Coordinate(y - h / 2) or not Coordinate(y + h / 2) then return end
    local function Pack(a, b)
        return math.floor(a * 1023 + .5) * 1024 + math.floor(b * 1023 + .5)
    end
    return Pack(x, y), Pack(w, h), math.floor(radius + .5) + (rotating == "1" and 1024 or 0)
end

local function ObserveChoiceHandoff(plan, map)
    local round = plan.round
    local choice = ChoiceRoute(plan)
    if not round or type(choice) ~= "table" then return end
    if map == choice.fromMap and QuestPhase(plan.quest) == 2 then
        if QuestChoicePanel(choice) or (not choice.dialog and QuestTransportOption(choice)) then
            plan.choiceSeen = plan.quest
        end
        return
    end
    if plan.choiceSeen ~= plan.quest or map ~= choice.map then return end
    local nextQuest = choice.nextQuest or plan.quest
    if choice.nextQuest then
        if Public(C_QuestLog.IsOnQuest(plan.quest)) ~= false or (WowAgent.OpenQuestOfferID() ~= nextQuest
                and Public(C_QuestLog.IsOnQuest(nextQuest)) ~= true) then return end
    elseif QuestPhase(plan.quest) ~= 3 then return end
    local anchor = QuestArea()
    if not anchor then return end
    local previous = plan.quest
    plan.choiceSeen, plan.transportSent, plan.choiceOpened = nil, nil, nil
    local stage = choice.nextQuest and ROUND_PICKUP or ROUND_TURN_IN
    SwitchRoundQuest(plan, {id = nextQuest, stage = stage, anchor = anchor})
    if stage == ROUND_TURN_IN then
        SetQuestInteraction(plan, nextQuest, nil, nil)
        AddRoundQuest(round, nextQuest)
    end
    print("WowAgent：已核对冒险选择后的地图和后续任务 " .. previous .. " → " .. nextQuest .. "。")
end

local function UpdateNavigation()
    if not navigation then return end
    local mapID = C_Map.GetBestMapForUnit("player") or 0
    ObserveChoiceHandoff(navigation, mapID)
    local id = navigation.quest
    local phase = QuestPhase(id)
    local active = phase == 2 or phase == 3
    local round = navigation.round
    if round and round.stage ~= ROUND_DONE then round.next = NextRoundAction(navigation, phase) end
    if navigation.taskBindings and ((round and round.stage == ROUND_DONE)
        or (not round and phase == 4 and not AwaitingFollowup(navigation))) then
        ClearTaskBindings()
        ClearSkyBindings()
        navigation.taskBindings = nil
        navigation.skyPitch, navigation.skyDismount, navigation.skyMacro = nil, nil, nil
    end
    local objectives = active and (QuestObjectives(id) or false)
    local entry, objectiveIndex, objectivesFinished = ActiveObjective(objectives)
    local objectiveKind, objectiveProgress, encodedIndex = Objective(id, active, objectives)
    local multi = type(objectives) == "table" and #objectives > 1
    local profile = navigation.automatic and phase == 2 and RetailQuest(id, mapID)
    local route = navigation.automatic and phase == 2 and ChoiceRoute(navigation)
    local choice = type(route) == "table" and route.fromMap == mapID
    local data = not multi and profile
    if objectivesFinished and not multi then entry = Public(objectives[1]) end
    local objectiveNPC = choice and route.giver or (navigation.automatic and phase == 2
        and not (data and data.arrival) and TalkObjectiveNPC(entry))
    local objectNames = data and data.objectNames
    local objectCreature = data and data.objectCreature
    local locale = objectNames and GetLocale and GetLocale()
    local objectName = locale and objectNames[locale == "enGB" and "enUS" or locale]
    local targetMap, gx, gy, aprWaypoint
    if objectName or objectCreature then
        targetMap, gx, gy = ObjectiveObjectPoint(objectName, QuestArea(), objectCreature)
    end
    local objectPin = targetMap ~= nil
    if not targetMap then targetMap, gx, gy, aprWaypoint = NavigationDestination(navigation, phase, mapID) end
    if WowAgent.Forever and navigation.automatic then
        navigation.foreverTrain = phase == 2 and IsOutdoors() or navigation.foreverTraining
        navigation.foreverProgress = WowAgent.Forever.Progression(navigation, mapID)
        if navigation.foreverProgress and navigation.foreverProgress.trainer then
            local trainer = navigation.foreverProgress.trainer
            targetMap, gx, gy, objectiveNPC = trainer.map, trainer.x, trainer.y, trainer.id
        end
    end
    local position = C_Map.GetPlayerMapPosition and C_Map.GetPlayerMapPosition(mapID, "player")
    local px, py
    if position then px, py = position:GetXY() end
    px, py, gx, gy = Public(px), Public(py), Public(gx), Public(gy)
    local height = UnitPosition and select(3, UnitPosition("player"))
    height = Public(height)
    local width, depth
    if C_Map.GetMapWorldSize then width, depth = C_Map.GetMapWorldSize(mapID) end
    width, depth = Public(width), Public(depth)
    local pitch, dismount, skyError = SkyBindings()
    if skyError then
        ClearSkyBindings()
        navigation.skyPitch, navigation.skyDismount, navigation.skyMacro = nil, nil, nil
        print("WowAgent：驭空术键位已撤销。" .. skyError)
    end
    local _, descentCode = DescentBinding()
    local flags, meta = 0, (WowAgent.RecoveryState and 15 or 9) + phase * 16 + (descentCode or 0) * 8192
    local outdoors = IsOutdoors and IsOutdoors()
    local flyable = IsFlyableArea and IsFlyableArea()
    local fly, ground = Mount(navigation.fly), Mount(navigation.ground)
    if outdoors then flags = flags + 1 end
    if flyable then flags = flags + 2 end
    if IsMounted() then flags = flags + 4 end
    local sky, gliding, skySpeed
    if C_PlayerInfo and C_PlayerInfo.GetGlidingInfo then
        local activeGlide, canGlide, forwardSpeed = C_PlayerInfo.GetGlidingInfo()
        gliding = Public(activeGlide)
        sky = IsMounted() and (gliding or Public(canGlide))
        skySpeed = Public(forwardSpeed)
    end
    if IsFlying() or gliding then flags = flags + 8 end
    if IsFalling and IsFalling() then flags = flags + 16 end
    if IsSwimming and IsSwimming() then flags = flags + 32 end
    if UnitAffectingCombat and UnitAffectingCombat("player") then flags = flags + 64 end
    if UnitCastingInfo and Public(UnitCastingInfo("player"))
        or UnitChannelInfo and Public(UnitChannelInfo("player")) then flags = flags + 128 end
    if (descentCode or pitch) and outdoors and flyable and fly and fly.usable and navigation.bodies[1] then
        flags = flags + 256
    end
    if ground and ground.usable and navigation.bodies[2] then flags = flags + 512 end
    if sky then flags = flags + 1024 end
    if navigation.bodies[4] then flags = flags + 2048 end
    if (not sky or pitch) and NavigationKeys(sky) then flags = flags + 4096 end
    local skyActions, skyCharges
    if sky and pitch then skyActions, skyCharges = SkyActions() end
    skyActions = skyActions or {0, 0, 0, 0, 0}
    local ascent, halt = skyActions[4], skyActions[5]
    if Coordinate(gx) and Coordinate(gy) then meta = meta + 128 else gx, gy = 0, 0 end
    if type(height) == "number" and height == height and height > -52428 and height < 52428 then
        meta = meta + 256
    else height = 0 end
    if width and depth and width > 0 and depth > 0 and width < 104857 and depth < 104857 then
        meta = meta + 512
    else width, depth = 0, 0 end
    if Coordinate(px) and Coordinate(py) then meta = meta + 1024 else px, py = 0, 0 end
    if fly and fly.active then meta = meta + 2048 end
    if GetBindingAction("F", true) == "INTERACTTARGET" then meta = meta + 4096 end
    navSequence = (navSequence + 1) % 128
    writeObjectiveProgress(navSequence % 16, objectiveProgress)
    local recoveryGhost
    if WowAgent.RecoveryState then
        local state, corpse, button, aspect = WowAgent.RecoveryState(navigation.recovery, mapID, MovementKeys)
        if state % 4 == 1 and navigation.automatic and WowAgent.NearbyPatrol
            and not InCombatLockdown() and not (IsFlying and IsFlying()) and not (IsFalling and IsFalling()) then
            button, aspect = WowAgent.NearbyPatrol()
        elseif state % 4 == 1 and navigation.automatic and InCombatLockdown() and WowAgent.CombatHandoff then
            button, aspect = WowAgent.CombatHandoff()
            state = state + 1024
        end
        recoveryGhost = state % 4 == 3
        writeRecovery[1](12, state + navSequence * 8192)
        writeRecovery[2](13, corpse)
        writeRecovery[3](14, button)
        writeRecovery[4](15, aspect)
    end
    local nearMarker = (phase == 1 or phase == 3 or objectiveNPC) and targetMap == mapID and width > 0 and depth > 0
        and Coordinate(px) and Coordinate(py) and meta % 256 >= 128
        and ((gx - px) * width)^2 + ((gy - py) * depth)^2 <= 8^2
    local task, click, clickKey = InteractionFlags(navigation, navigation.foreverProgress and 2 or phase,
        sky, nearMarker, objectiveNPC), 0, 0
    local preciseObject
    if id == 90883 and phase == 2 then
        local objective, progress = Objective(id, active)
        if objective == 2 and progress == 1 then
            task = task + 512
            click, clickKey = MountTutorialClick(navigation)
        end
    elseif choice then
        clickKey, click = 6, QuestChoiceStep(navigation, route)
    elseif objectiveNPC then
        clickKey = 5
    elseif data and data.arrival then
        clickKey = 3
    elseif navigation.automatic and phase == 2 and id ~= 90882 then
        clickKey, click, preciseObject = GenericObjective(id, objectives, entry, objectiveIndex, objectName,
            objectCreature or type(objectNames) == "table" or (not multi and navigation.preciseObjective == id),
            objectCreature, data)
        if preciseObject then navigation.preciseObjective = id end
        if clickKey == 2 and objectPin then clickKey = 4 end
    end
    local encounterFlags, encounterTarget = 0, 0
    if navigation.automatic and WowAgent.QuestEncounter then
        encounterFlags, encounterTarget = WowAgent.QuestEncounter.State(id, phase, taskTargetButton)
        if encounterFlags >= 2048 and not choice then clickKey, click = 1, encounterTarget end
    end
    if navigation.foreverProgress then
        local state = navigation.foreverProgress
        clickKey, click = 7, state.value or state.node or 0
        if click >= 1048576 then click = 0 end
        if state.trainer then navigation.foreverTraining = true end
        if state.code == 11 or state.code == 12 then navigation.foreverTraining = nil end
    end
    if WowAgent.WriteNav3D then
        WowAgent.WriteNav3D(navSequence, id, phase, mapID, targetMap,
            meta % 256 >= 128 and gx or nil, meta % 256 >= 128 and gy or nil, flags, encounterFlags)
    end
    local mapCenter, mapSize, mapInfo
    if preciseObject or recoveryGhost or navigation.automatic and InCombatLockdown() then
        mapCenter, mapSize, mapInfo = MinimapGeometry()
    end
    writeMinimap[1](0, mapCenter or 0)
    writeMinimap[2](1, mapSize or 0)
    writeMinimap[3](2, mapInfo and mapInfo + navSequence * 2048 or 0)
    writeSky(0, ascent + halt * 16 + (pitch or 0) * 256 + (dismount or 0) * 16384)
    writeClick(navigation.foreverProgress and navigation.foreverProgress.code or 2, click)
    writeTask(1, task + navSequence * 1024 + clickKey * 131072)
    local roundStatus = 0
    if round then
        roundStatus = round.stage + (round.next and (round.next.blocked and 16 or 8) or 0)
        if round.immediate then roundStatus = roundStatus + 32 end
    end
    local suspendReady = round and round.stage ~= ROUND_DONE and TaskClickMatches("F5", "WowAgentTaskSuspend")
    local resource = objectiveKind == 3 and objectiveProgress % 1024 == 1000
    local suspended = round and round.suspended and round.suspended[id]
    writeObjective(12, math.min(63, encodedIndex or objectiveIndex or 0) + (suspended and 64 or 0)
        + (resource and 128 or 0)
        + (objectivesFinished and 256 or 0)
        + (suspendReady and 512 or 0) + (round and round.suspendEpoch or 0) * 1024 + navSequence * 8192)
    local strafeVerified = GetBindingAction("Q", true) == "STRAFELEFT"
        and GetBindingAction("E", true) == "STRAFERIGHT"
    local lastTargetVerified = Public(GetBindingAction("P", true)) == "TARGETLASTTARGET"
    local area = phase == 2 and targetMap == mapID and WowAgent.QuestAreaMask
        and WowAgent.QuestAreaMask(id, mapID, gx, gy, width, depth)
    for row = 1, 5 do writeArea[row](row + 3, (area and area[row] or 0) + (navSequence % 16) * 65536) end
    local sweepReady = navigation.sweepBody and taskSweepButton and TaskClickMatches("O", "WowAgentTaskSweep")
        and Public(taskSweepButton:GetAttribute("macrotext")) == navigation.sweepBody
    local cameraReady = camera.ready()
    local cameraDistance = targetMap == mapID and width > 0 and depth > 0
        and Coordinate(px) and Coordinate(py) and meta % 256 >= 128
        and math.sqrt(((gx - px) * width)^2 + ((gy - py) * depth)^2) or nil
    camera.observe({phase = phase, action = id == 90882 and phase == 2 and 1 or clickKey,
        distance = cameraDistance, outdoors = Public(outdoors), flying = flags % 16 >= 8,
        flightMount = fly and fly.active, speed = type(skySpeed) == "number" and skySpeed or nil})
    local cameraValue, cameraState = camera.telemetry(navSequence)
    writeCamera(13, cameraValue)
    writeCameraState(14, cameraState)
    -- Extension schema 6: low two bits retain orbit binding/lease; bits 2..8
    -- report outstanding suspensions (saturated at 127), including saved ones.
    -- Bit 19 retains overhead availability; the build row carries nav sequence.
    writeBuild(14, 293 + navSequence * 4096 + (camera.probeReady() and 524288 or 0))
    writeBuildSchema(15, 357888 + (camera.orbitReady and camera.orbitReady() and 1 or 0)
        + (camera.orbitActive and camera.orbitActive() and 2 or 0)
        + math.min(127, round and ReconcileRoundSuspensions(round) or 0) * 4
        + (camera.overheadReady and camera.overheadReady() and 524288 or 0))
    -- Optional UI extension v1. Existing schema 6 and all old rows stay intact.
    -- bits 0..3: verified hidden groups; 4..6: version; 7..13: nav sequence;
    -- bits 14..19: signature 45. No option or Apply result can certify visibility.
    local hiddenUI = WowAgent.VisualUI and WowAgent.VisualUI.HiddenMask() or 0
    writeVisualUI(13, 45 * 16384 + navSequence * 128 + 16 + hiddenUI)
    local cameraZoom = cameraReady and camera.zoom()
    local cameraClose = cameraZoom and cameraZoom < camera.target() * .65
    writeLoot(9, lootEpoch + (sweepReady and 256 or 0) + (area and 512 or 0) + navSequence * 1024
        + (cameraReady and 131072 or 0) + (cameraClose and 262144 or 0) + camera.epoch * 524288)
    local speed = type(skySpeed) == "number" and skySpeed >= 0 and skySpeed < math.huge
        and math.min(31, math.floor(skySpeed / 4 + .5) + 1) or 0
    writeSkySkills(10, skyActions[1] + skyActions[2] * 16 + skyActions[3] * 256 + navSequence * 8192)
    writeSkyMotion(11, (sky and speed or 0) + (skyCharges and skyCharges + 1 or 0) * 32
        + skyCast * 256 + skyCastEpoch * 2048 + navSequence * 8192)
    writeRound(3, roundStatus + navSequence * 64 + CorpseLootFlags(navigation, phase, task)
        + (preciseObject and 65536 or 0) + (aprWaypoint and 131072 or 0) + (strafeVerified and 262144 or 0)
        + (lastTargetVerified and 524288 or 0))
    local values = {id, targetMap or 0, math.floor(gx * 1048575 + .5), math.floor(gy * 1048575 + .5),
                    flags + navSequence * 8192, math.floor(height * 10 + 524288 + .5),
                    math.floor(width * 10 + .5), math.floor(depth * 10 + .5), meta,
                    math.floor(px * 1048575 + .5), math.floor(py * 1048575 + .5), mapID}
    for row, value in ipairs(values) do navRows[row](row + 3, value >= 0 and value <= 1048575 and value or 0) end
end

function WowAgent.PrepareQuestNavigation(id, fly, ground, useTaskBindings, automatic, npc, useItem, batch)
    if InCombatLockdown() or IsMounted() then return print("WowAgent：请下马脱战后准备寻路。") end
    local round
    if automatic and batch and id then
        local selected = id
        round, id = PrepareRound(id)
        if not round then return print("WowAgent：未能读取当前地图位置，无法准备附近任务。") end
        if id ~= selected then npc, useItem = nil, nil end
        if npc and npc ~= "" or useItem then
            round.options[id] = {npc = npc ~= "" and npc or nil, useItem = useItem}
        elseif round.options[id] then
            npc, useItem = round.options[id].npc, round.options[id].useItem
        end
    end
    local pickup = automatic and id and QuestPickup(id, round)
    local function CanPrepare()
        if not id or id < 1 or id > 1048575 then return false end
        if C_QuestLog.IsOnQuest(id) then return true end
        if not automatic then return false end
        if round and (round.stage ~= ROUND_PICKUP or round.immediate) and C_QuestLog.IsQuestFlaggedCompleted(id) then
            return true
        end
        return pickup or HasOpenQuestOffer(id)
    end
    if not CanPrepare() then return print("WowAgent：请选择已接任务，或先打开对应的接任务对话。") end
    local support = WowAgent.LayoutSupport
    if not support or not support.SetupAction then return print("WowAgent：动作配置模块尚未加载。") end
    local plan = {fly = ChooseMount("fly", fly), ground = ChooseMount("ground", ground), bodies = {},
        automatic = automatic, useItem = useItem, round = round}
    SetQuestInteraction(plan, id, npc ~= "" and npc or nil, pickup)
    if automatic then
        if C_Item and C_Item.RequestLoadItemDataByID then
            for _, item in ipairs(fiveYardItems) do C_Item.RequestLoadItemDataByID(item) end
        end
        plan.item = QuestItemID(id)
        if useItem and not plan.item then return print("WowAgent：任务日志未提供可绑定的专用道具。") end
    end
    ClearSkyBindings()
    ClearTaskBindings()
    RestoreNativeRange()
    navigation = plan
    navFrame:Hide()
    local actions = {}
    for kind, key in ipairs(travelKeys) do
        local name, body = WowAgent.TravelMacro(kind)
        if name and not forever then
            actions[#actions + 1] = {kind = "travel", id = kind, key = key, body = body}
        end
    end
    if automatic then
        if forever then
            local spell = support.LearnedSpells and forever.OutputSpell(support.LearnedSpells())
            if not spell then
                WowAgent.StopQuestNavigation()
                return print("WowAgent：无限服猎人的已学输出技能尚未核对。")
            end
            local melee = forever.MeleeSpell(support.LearnedSpells())
            local meleeSlot = melee and support.BoundSlot(5)
            if meleeSlot and not select(1, support.ReadAction(meleeSlot)) then
                actions[#actions + 1] = {kind = "forever_melee", id = melee, key = 5, optional = true}
            end
            actions[#actions + 1] = {kind = "forever", id = spell, key = 4}
        else
            actions[#actions + 1] = {kind = "assist", key = 1}
        end
        actions[#actions + 1] = {kind = "prepared_dialogue", key = 9}
    else
        actions[#actions + 1] = {kind = "quest", id = id, key = 9}
        actions[#actions + 1] = {kind = "assist", key = 1}
    end
    local function Publish(skyError)
        if navigation ~= plan then return end
        if InCombatLockdown() or IsMounted() or not CanPrepare() then
            WowAgent.StopQuestNavigation()
            return print("WowAgent：准备已停止，请下马脱战并确认任务仍在日志中后重试。")
        end
        local pitch, dismount, verifyError = SkyBindings()
        if not pitch then
            ClearSkyBindings()
            plan.skyPitch, plan.skyDismount, plan.skyMacro = nil, nil, nil
            print("WowAgent：驭空术暂不可用。" .. (skyError or verifyError or "临时键位核对失败。"))
        end
        if C_QuestLog.AddQuestWatch and C_QuestLog.IsOnQuest(id) then C_QuestLog.AddQuestWatch(id) end
        if automatic and (InteractionFlags(plan, 2, false) % 2 ~= 1
                or (plan.sweepBody and (not TaskClickMatches("O", "WowAgentTaskSweep")
                    or Public(taskSweepButton:GetAttribute("macrotext")) ~= plan.sweepBody))
                or (plan.item and not TaskClickMatches("J", "WowAgentTaskItem"))) then
            WowAgent.StopQuestNavigation()
            return print("WowAgent：互动、选敌、拾取或任务对话核对失败。")
        end
        if id == 90883 or (automatic and id == 90882) then PrepareTutorialMount(plan) end
        if round then
            local saved, character = SavedRound()
            if saved then saved[character] = round end
        end
        WowAgent.QuestCommand("track " .. id)
        UpdateNavigation()
        navFrame:Show()
        local mapID = C_Map.GetBestMapForUnit("player")
        local targetMap, x, y, aprWaypoint = NavigationDestination(plan, QuestPhase(id), mapID)
        local descent = DescentBinding()
        if pitch then
            print("WowAgent：寻路已准备，驭空术俯冲 " .. skyKeys[pitch] .. "，下马 " .. skyKeys[dismount] .. "。")
        elseif descent then
            print("WowAgent：寻路已准备，下降键 " .. descent .. "。优先飞行，其次坐骑，最后步行。")
        else
            print("WowAgent：寻路已准备。未找到可用的下降键，本次可骑乘或步行。")
        end
        if targetMap and x and y then
            print(string.format("WowAgent：任务目标地图 %d，坐标 %.2f, %.2f。", targetMap, x * 100, y * 100))
            if aprWaypoint then print("WowAgent：已加载 APR 目标搜索点。") end
        else print("WowAgent：任务目标坐标尚未返回，脚本会等待坐标。") end
        print("WowAgent：" .. camera.probeStatus())
        if camera.ready() then
            print("WowAgent：已核对 F8，主控会按场景调整并等待距离回执。可将调好的约 20° 基准保存到视角 5。")
        elseif automatic then
            local started = WowAgent.RestoreQuestCamera()
            print("WowAgent：" .. camera.status()
                .. (started and "已开始直接调整镜头。" or "本次未启动镜头恢复。"))
        end
        if automatic then
            if plan.fetchBody and TaskClickMatches("L", "WowAgentTaskFetch") then
                print("WowAgent：猎人「拾回」已准备，L 拾回当前尸体，O 清除目标并继续拾回附近掉落。")
            end
            if QuestPhase(id) == 1 and type(plan.pickupNPC) == "number" then
                print("WowAgent：接取对象 NPC #" .. plan.pickupNPC .. "，进入 5 码后核对任务对话。")
            end
            if plan.npc then
                print("WowAgent：交付对象 " .. (type(plan.npc) == "number" and ("任务配置 NPC #" .. plan.npc) or plan.npc)
                    .. "。互动和自动拾取已启用。"
                    .. (plan.item and ("J 已核对任务道具 " .. plan.item .. "。") or "当前任务没有专用道具键。"))
            else print("WowAgent：将按游戏任务标记和对话中的任务编号确认交付对象。") end
            print("WowAgent：一键准备完成。到 macOS 菜单栏「WoW 键鼠」选择「授权并运行已准备任务」。")
        end
    end
    local function Prepare(index, retries)
        if navigation ~= plan then return end
        if InCombatLockdown() or IsMounted() or not CanPrepare() then
            WowAgent.StopQuestNavigation()
            return print("WowAgent：准备已停止，请下马脱战并确认任务仍在日志中后重试。")
        end
        local action = actions[index]
        if action then
            local ok, reason
            if action.kind == "prepared_dialogue" then
                ok, reason = SetupTaskDialogue(support, plan)
            else
                ok, reason = support.SetupAction(action.kind, action.id, action.key)
            end
            if ok then
                if action.body then plan.bodies[action.id] = action.body end
                C_Timer.After(.4, function() Prepare(index + 1) end)
            elseif reason == "macro_pending" and (retries or 0) < 2 then
                C_Timer.After(.8, function() Prepare(index, (retries or 0) + 1) end)
            elseif action.optional then
                C_Timer.After(.4, function() Prepare(index + 1) end)
            else
                print("WowAgent：寻路准备停在数字键 " .. action.key .. "，请查看上一条提示。")
                WowAgent.StopQuestNavigation()
            end
            return
        end
        local verified, message = NavigationKeys()
        if not verified then
            WowAgent.StopQuestNavigation()
            return print("WowAgent：" .. (message or "寻路键位未通过核对。"))
        end
        local skyError = PrepareSkyBindings(plan, support)
        C_Timer.After(.25, function() Publish(skyError) end)
    end
    print("WowAgent：正在逐个核对寻路键位，请等待准备结果。")
    if not useTaskBindings then return Prepare(1) end
    if not ApplyTaskBindings() then
        WowAgent.StopQuestNavigation()
        return print("WowAgent：无法启用临时任务键位。")
    end
    plan.taskBindings = true
    camera.prepare(taskBindingOwner)
    if automatic then pcall(SetOverrideBinding, taskBindingOwner, false, "P", "TARGETLASTTARGET") end
    if automatic and (not GameTaskOptions(true) or not SetupTaskClicks(plan)) then
        WowAgent.StopQuestNavigation()
        return print("WowAgent：无法启用游戏互动、自动拾取或任务辅助键位。")
    end
    if automatic and WowAgent.PrepareRecovery then
        plan.recovery = WowAgent.PrepareRecovery(taskBindingOwner)
    end
    C_Timer.After(.25, function()
        if navigation ~= plan then return end
        for _, binding in ipairs(taskBindings) do
            local key, action, label = unpack(binding)
            if Public(GetBindingAction(key, true)) ~= action then
                WowAgent.StopQuestNavigation()
                return print("WowAgent：临时键位未通过核对，" .. key .. " 应为「" .. label .. "」。")
            end
        end
        print("WowAgent：临时任务键位已核对，F 与目标互动，Tab 选敌。")
        Prepare(1)
    end)
end

function WowAgent.StopQuestNavigation()
    if WowAgent.HideNav3D then WowAgent.HideNav3D() end
    if WowAgent.VisualUI then WowAgent.VisualUI.Restore(true) end
    navigation = nil
    ConfigureTaskTarget(nil)
    RestoreNativeRange()
    ClearTaskBindings()
    ClearSkyBindings()
    if navFrame then navFrame:Hide() end
end

navFrame = CreateFrame("Frame", nil, UIParent)
-- Extensions grow upward using negative offsets. Keep the core's bottom
-- anchored to the main strip; increasing this height would shift every row.
navFrame:SetSize(142, 304)
navFrame:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 144, 0)
navFrame:SetFrameStrata("TOOLTIP")
writeMinimap = {}
writeRecovery = {}
writeArea = {}
for row = 1, 5 do writeArea[row] = WowAgent.NewPixelRow(navFrame, (row - 11) * 16) end
writeLoot = WowAgent.NewPixelRow(navFrame, -80)
writeSkySkills = WowAgent.NewPixelRow(navFrame, -192)
writeObjective = WowAgent.NewPixelRow(navFrame, -208)
writeObjectiveProgress = WowAgent.NewPixelRow(navFrame, -224)
writeCamera = WowAgent.NewPixelRow(navFrame, -240)
writeCameraState = WowAgent.NewPixelRow(navFrame, -256)
writeBuild = WowAgent.NewPixelRow(navFrame, -272)
writeBuildSchema = WowAgent.NewPixelRow(navFrame, -288)
writeVisualUI = WowAgent.NewPixelRow(navFrame, -304)
writeSkyMotion = WowAgent.NewPixelRow(navFrame, -176)
if WowAgent.RecoveryState then
    for row = 1, 4 do writeRecovery[row] = WowAgent.NewPixelRow(navFrame, (row - 5) * 16) end
end
for row = 1, 3 do writeMinimap[row] = WowAgent.NewPixelRow(navFrame, (row - 1) * 16) end
writeRound = WowAgent.NewPixelRow(navFrame, 48)
writeClick = WowAgent.NewPixelRow(navFrame, 64)
writeTask = WowAgent.NewPixelRow(navFrame, 80)
writeSky = WowAgent.NewPixelRow(navFrame, 96)
for row = 1, 12 do navRows[row] = WowAgent.NewPixelRow(navFrame, (row + 6) * 16) end
navFrame:SetScript("OnUpdate", function(_, dt)
    navElapsed = navElapsed + dt
    if navElapsed < .20 then return end
    navElapsed = 0
    UpdateNavigation()
end)
navFrame:Hide()
navFrame:RegisterEvent("ADDON_LOADED")
navFrame:RegisterEvent("PLAYER_LOGOUT")
navFrame:RegisterEvent("PLAYER_DEAD")
navFrame:RegisterEvent("PLAYER_UNGHOST")
navFrame:RegisterEvent("LOOT_SLOT_CLEARED")
navFrame:RegisterEvent("UI_ERROR_MESSAGE")
navFrame:RegisterUnitEvent("UNIT_SPELLCAST_SUCCEEDED", "player")
navFrame:SetScript("OnEvent", function(_, event, name, message, spell)
    if event == "UNIT_SPELLCAST_SUCCEEDED" and name == "player" then
        if WowAgent.RecordPetMend then WowAgent.RecordPetMend(spell) end
        local action = SkySpellAction(Public(spell))
        if action then skyCast, skyCastEpoch = action, (skyCastEpoch + 1) % 4 end
    end
    if event == "UI_ERROR_MESSAGE" and navigation and navigation.transportSent and not navigation.transportStandUntil then
        local route = ChoiceRoute(navigation)
        local now = GetTime()
        if route and route.retryStanding and now - navigation.transportSent <= 3
            and C_Map.GetBestMapForUnit("player") == route.fromMap and QuestPhase(navigation.quest) == 2 then
            local code = Public(name)
            message = Public(message)
            if (type(code) == "number" and (code == LE_GAME_ERR_SPELL_FAILED_NOT_STANDING
                    or code == LE_GAME_ERR_TAXINOTSTANDING))
                or (type(message) == "string" and (message == SPELL_FAILED_NOT_STANDING
                    or message == ERR_SPELL_FAILED_NOT_STANDING or message == ERR_TAXINOTSTANDING)) then
                navigation.transportSent, navigation.transportStandUntil = nil, now + 2
            end
        end
    end
    if event == "LOOT_SLOT_CLEARED" then lootEpoch = (lootEpoch + 1) % 256 end
    if event == "PLAYER_DEAD" and WowAgent.RecordRecoveryDeath then WowAgent.RecordRecoveryDeath() end
    if event == "PLAYER_UNGHOST" and WowAgent.ClearRecoveryDeath then WowAgent.ClearRecoveryDeath() end
    if event == "PLAYER_LOGOUT" or (event == "ADDON_LOADED" and name == "WowAgent") then RestoreNativeRange() end
end)
