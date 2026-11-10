-- Verify pickup preparation and dialogue actions against the same quest ID.
local frames, values, calls, messages = {}, {}, {}, {}
local namedFrames, rowValues = {}, {}
local active, combat, prepared, currentID = false, false, false, 0
local available = {{questID = 90882, title = "若是那豺狼来了"}}
Enum = {QuestClassification = {Normal = 0}}
UIParent, WowAgentChannelFrame = {}, {}
function CreateFrame(_, name)
    local frame = {shown = false, scripts = {}, attributes = {}}
    function frame:SetScript(event, handler) self.scripts[event] = handler end
    function frame:SetAttribute(key, value) self.attributes[key] = value end
    function frame:GetAttribute(key) return self.attributes[key] end
    function frame:Show() self.shown = true end
    function frame:Hide() self.shown = false end
    function frame:IsShown() return self.shown end
    setmetatable(frame, {__index = function() return function() end end})
    frames[#frames + 1] = frame
    if name then namedFrames[name] = frame end
    return frame
end
function GetQuestID() return currentID end
function InCombatLockdown() return combat end
function QuestGetAutoAccept() return false end
function AcceptQuest() active = true; calls[#calls + 1] = "accept" end
function print(message) messages[#messages + 1] = message end
C_QuestInfoSystem = {GetQuestClassification = function() return 0 end}
C_QuestLine = {GetQuestLineInfo = function() return nil end}
C_Map = {GetBestMapForUnit = function() return 2248 end}
C_GossipInfo = {
    GetAvailableQuests = function() return available end,
    GetActiveQuests = function() return {} end,
    SelectAvailableQuest = function(id) calls[#calls + 1] = id end,
}
C_QuestLog = {
    GetNumQuestLogEntries = function() return 0 end,
    GetLogIndexForQuestID = function() return nil end,
    GetTitleForQuestID = function() return "若是那豺狼来了" end,
    IsWorldQuest = function() return false end,
    IsRepeatableQuest = function() return false end,
    IsOnQuest = function() return active end,
    ReadyForTurnIn = function() return false end,
    IsQuestFlaggedCompleted = function() return false end,
    RequestLoadQuestByID = function() end,
    GetQuestObjectives = function() return {{numFulfilled = 0, numRequired = 6}} end,
}
WowAgent = {
    NewPixelRow = function(_, offset)
        return function(kind, value) values[kind], rowValues[offset] = value, value end
    end,
    LayoutSupport = {SetupAction = function(kind, id, key)
        assert(kind == "quest" and id == 90882 and key == 9)
        if not prepared then print("macro creation failed") end
        return prepared
    end},
}
dofile("addon/WowAgent/RetailQuestData.lua")
dofile("addon/WowAgent/Quest.lua")
local function event(name) frames[1].scripts.OnEvent(nil, name) end
event("GOSSIP_SHOW")
WowAgent.QuestCommand("prepare")
assert(not frames[2]:IsShown())
assert(#messages == 1 and messages[1] == "macro creation failed")
prepared = true
available[2] = {questID = 12345}
WowAgent.QuestCommand("prepare")
assert(not frames[2]:IsShown())
WowAgent.QuestCommand("prepare 90882")
assert(frames[2]:IsShown() and values[10] == 90882 and values[11] >= 524288)
WowAgent.QuestAction(12345)
assert(#calls == 0)
WowAgent.QuestAction(90882)
assert(calls[1] == 90882)
currentID = 90882
event("QUEST_DETAIL")
combat = true
WowAgent.QuestAction(90882)
assert(not active)
combat = false
WowAgent.QuestAction(90882)
assert(active and calls[2] == "accept")
frames[2].scripts.OnUpdate(nil, .2)
assert(math.floor(values[11] / 16) % 8 == 2)

-- Preparing an accepted quest publishes its native waypoint and verified controls.
C_Map.GetPlayerMapPosition = function() return {GetXY = function() return .4, .5 end} end
C_Map.GetMapWorldSize = function() return 1000, 500 end
C_QuestLog.GetQuestsOnMap = function()
    return {{questID = 12345, x = .1, y = .2}, {questID = 90882, x = .45, y = .55}}
end
function UnitPosition() return 10, 20, 100 end
function IsOutdoors() return true end
function IsFlyableArea() return true end
function IsMounted() return false end
function IsFlying() return false end
local descent = "C"
function GetBindingKey(action)
    if action == "SITORSTAND" then return "SHIFT-X", descent end
end
function GetBindingAction(key)
    if key == descent or key == "SHIFT-X" then return "SITORSTAND" end
    return ({W = "MOVEFORWARD", S = "MOVEBACKWARD", SPACE = "JUMP", X = "ACTIONBUTTON12",
             LEFT = "TURNLEFT", RIGHT = "TURNRIGHT", F = "INTERACTTARGET"})[key]
end
C_MountJournal = {
    GetMountIDs = function() return {10, 20} end,
    GetMountInfoByID = function(id)
        return id == 10 and "飞行坐骑" or "地面坐骑", id + 100, nil, false, true,
               nil, id == 10, nil, nil, false, true
    end,
    GetMountInfoExtraByID = function(id) return nil, nil, nil, false, id == 10 and 248 or 230 end,
}
C_Spell = {GetSpellName = function(id)
    return ({[110] = "飞行坐骑", [120] = "地面坐骑", [436854] = "切换飞行风格"})[id]
end}
local preparedKeys = {}
local timers, rideAttempts = {}, 0
C_Timer = {After = function(_, callback) timers[#timers + 1] = callback end}
WowAgent.LayoutSupport.SetupAction = function(kind, id, key)
    if kind == "travel" then
        if key == 7 then
            rideAttempts = rideAttempts + 1
            if rideAttempts == 1 then return nil, "macro_pending" end
        end
        local _, body = WowAgent.TravelMacro(id)
        preparedKeys[key] = body
    end
    return true
end
WowAgent.LayoutSupport.ActionMatches = function(key, _, body) return preparedKeys[key] == body end
WowAgent.PrepareQuestNavigation(90882, 0, 0)
assert(not frames[3]:IsShown())
while #timers > 0 do table.remove(timers, 1)() end
assert(rideAttempts == 2)
assert(frames[3]:IsShown() and values[4] == 90882 and values[5] == 2248)
assert(preparedKeys[6]:find("飞行坐骑", 1, true) and preparedKeys[7]:find("地面坐骑", 1, true))
assert(preparedKeys[8] == "/dismount [mounted,noflying]")
assert(values[6] == math.floor(.45 * 1048575 + .5) and values[9] == 525288)
assert(math.floor(values[8] / 4096) % 2 == 1)
assert(math.floor(values[8] / 256) % 2 == 1 and math.floor(values[12] / 8192) == string.byte("C"))
descent = nil
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[8] / 4096) % 2 == 1)
assert(math.floor(values[8] / 256) % 4 == 2 and math.floor(values[12] / 8192) == 0)
WowAgent.QuestCommand("off")
assert(not frames[3]:IsShown())
WowAgent.PrepareQuestNavigation(90882, 0, 0)
WowAgent.StopQuestNavigation()
while #timers > 0 do table.remove(timers, 1)() end
assert(not frames[3]:IsShown() and rideAttempts == 2)

-- Skyriding changes the main action bar. The temporary keys stay available
-- while ascent and braking are read from the live bar and its cooldowns.
local overrides, owners = {}, {}
local baseBinding = GetBindingAction
local mounted, gliding, ascentCharges = false, false, 1
function IsMounted() return mounted end
function GetBindingAction(key)
    return overrides[key] or (key == "F12" and "USER_BINDING") or baseBinding(key) or ""
end
function SetOverrideBinding(owner, _, key, action)
    overrides[key], owners[key] = action, owner
end
function SetOverrideBindingClick(owner, _, key, button, mouse)
    SetOverrideBinding(owner, false, key, "CLICK " .. button .. ":" .. mouse)
end
function ClearOverrideBindings(owner)
    for key, frame in pairs(owners) do
        if frame == owner then overrides[key], owners[key] = nil, nil end
    end
end
function GetMacroInfo(id)
    if id == 50 then return "WATLand", 134400, "/dismount [mounted,noflying]" end
end
function GetMacroIndexByName(name) return name == "WATLand" and 50 or 0 end
function GetMacroBody(id) return select(3, GetMacroInfo(id)) end
WowAgent.LayoutSupport.ReadMacroBody = GetMacroBody
WowAgent.LayoutSupport.BoundSlot = function(key) return key end
WowAgent.LayoutSupport.ReadAction = function(slot)
    if mounted and slot == 2 then return "spell", 372610 end
    if mounted and slot == 4 then return "spell", 403092 end
    if not mounted and slot == 8 then return "macro", 50 end
end
WowAgent.LayoutSupport.ActionMatches = function(key, _, body)
    return not mounted and preparedKeys[key] == body
end
C_PlayerInfo = {GetGlidingInfo = function() return gliding, mounted end}
C_ActionBar = {
    IsUsableAction = function() return true end,
    GetActionCharges = function(slot) return slot == 2 and {currentCharges = ascentCharges} or nil end,
    GetActionCooldown = function() return {duration = 0} end,
}
WowAgent.PrepareQuestNavigation(90882, 0, 0)
while #timers > 0 do table.remove(timers, 1)() end
assert(frames[3]:IsShown() and math.floor(values[8] / 256) % 2 == 1)
assert(not overrides.F12 and overrides.F11 == "PITCHDOWN")
assert(overrides.F10 == "CLICK WowAgentSkyDismount:LeftButton")
mounted, gliding = true, true
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[8] / 4096) % 2 == 1 and math.floor(values[8] / 8) % 2 == 1)
assert(values[0] == 2 + 4 * 16 + 11 * 256 + 10 * 16384)
ascentCharges = 0
frames[3].scripts.OnUpdate(nil, .2)
assert(values[0] % 16 == 0)
WowAgent.StopQuestNavigation()
assert(not next(overrides) and GetBindingAction("F12") == "USER_BINDING")

-- A full key layout can borrow 0 and Z only while both action slots are empty.
mounted, gliding, ascentCharges = false, false, 1
local slotOccupied
ActionButton10, ActionButton11 = {action = 10}, {action = 11}
C_ActionBar.HasAction = function(slot) return slot == slotOccupied end
function GetBindingAction(key)
    return overrides[key] or (key == "0" and "ACTIONBUTTON10")
        or (key == "Z" and "ACTIONBUTTON11") or baseBinding(key) or "USER_BINDING"
end
WowAgent.PrepareQuestNavigation(90882, 0, 0)
while #timers > 0 do table.remove(timers, 1)() end
assert(overrides["0"] == "PITCHDOWN" and overrides.Z == "CLICK WowAgentSkyDismount:LeftButton")
mounted, gliding = true, true
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[8] / 4096) % 2 == 1)
slotOccupied = 11
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[8] / 4096) % 2 == 0 and values[0] == 0)
assert(not next(overrides))

-- Only the configured living soft target becomes eligible for F.
active = true
mounted, gliding, slotOccupied = false, false, nil
C_QuestLog.ReadyForTurnIn = function() return true end
C_QuestLog.IsQuestFlaggedCompleted = function() return false end
Enum.SoftTargetEnableFlags = {Any = 3}
local cvars = {SoftTargetInteractRange = "10", SoftTargetInteractRangeIsHard = "0"}
local now = 0
function GetTime() return now end
C_CVar = {
    GetCVar = function(key) return cvars[key] end,
    SetCVar = function(key, value) cvars[key] = tostring(value) end,
}
local soft, selected, dead, npc = "Creature-NPC", "Creature-Corpse", true, "萨尔"
function UnitGUID(unit)
    if unit == "softinteract" then return soft end
    return selected
end
function UnitIsDead() return dead end
function UnitName() return npc end
WowAgent.LayoutSupport.WriteMacro = function(name, body)
    assert(name == "WAQTask" and body == "/run WowAgent.RunPreparedQuestAction()")
    return 51
end
WowAgent.LayoutSupport.ReadMacroBody = function(index)
    return index == 51 and "/run WowAgent.RunPreparedQuestAction()" or GetMacroBody(index)
end
WowAgent.PrepareQuestNavigation(90882, 0, 0, true, true, "萨尔")
while #timers > 0 do table.remove(timers, 1)() end
assert(frames[3]:IsShown() and cvars.autoLootDefault == "1" and cvars.softTargetInteract == "3")
assert(overrides.K == "CLICK WowAgentTaskTarget:LeftButton")
assert(overrides["9"] == "CLICK WowAgentTaskDialogue:LeftButton")
assert(values[1] % 512 == 33)
dead = false
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 512 == 35)
selected = soft
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 512 == 39) -- No usable distance data.
assert(cvars.SoftTargetInteractRange == "5" and cvars.SoftTargetInteractRangeIsHard == "1")
soft = nil
now = 1
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 512 == 37 + 256)
soft = selected
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 512 == 39 + 256 + 128)
local squared = 25.01
function UnitDistanceSquared() return squared, true end
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 512 == 39 + 256)
squared = 25
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 512 == 39 + 256 + 128)
UnitDistanceSquared = nil
C_Item = {IsItemInRange = function(id, unit) return id == 1970 and unit == "target" end}
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 512 == 39 + 256 + 128)
WowAgent.StopQuestNavigation()
assert(not next(overrides))
assert(cvars.SoftTargetInteractRange == "10" and cvars.SoftTargetInteractRangeIsHard == "0")

-- The panel's task profile temporarily overrides conflicts and restores them
-- once the quest is turned in or the user closes the navigation strip.
mounted, gliding, slotOccupied = false, false, nil
local previousBinding = GetBindingAction
function GetBindingAction(key)
    return overrides[key] or (key == "F" and "ACTIONBUTTON3")
        or (key == "TAB" and "TARGETNEARESTFRIEND") or previousBinding(key)
end
C_QuestLog.ReadyForTurnIn = function() return true end
WowAgent.PrepareQuestNavigation(90882, 0, 0, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(frames[3]:IsShown())
assert(GetBindingAction("F") == "INTERACTTARGET" and GetBindingAction("TAB") == "TARGETNEARESTENEMY")
assert(GetBindingAction("9") == "ACTIONBUTTON9")
assert(math.floor(values[12] / 4096) % 2 == 1)
active = false
C_QuestLog.IsQuestFlaggedCompleted = function() return true end
frames[3].scripts.OnUpdate(nil, .2)
assert(GetBindingAction("F") == "ACTIONBUTTON3" and GetBindingAction("TAB") == "TARGETNEARESTFRIEND")
WowAgent.StopQuestNavigation()
assert(not next(overrides))

-- After the reward, hand off only the expected quest and leave acceptance to
-- the next verified dialogue action.
function GetLocale() return "zhCN" end
function UnitFactionGroup() return "Horde" end
local activeQuest, completedQuest = 90882, nil
C_QuestLog.IsOnQuest = function(id) return id == activeQuest end
C_QuestLog.IsQuestFlaggedCompleted = function(id) return id == completedQuest end
C_QuestLog.GetNumQuestLogEntries = function() return 0 end
function GetNumQuestChoices() return 0 end
function GetQuestReward()
    completedQuest, activeQuest, currentID = 90882, nil, 90883
    event("QUEST_DETAIL")
end
currentID = 90882
event("QUEST_COMPLETE")
WowAgent.PrepareQuestNavigation(90882, 0, 0, true, true, "萨尔")
while #timers > 0 do table.remove(timers, 1)() end
WowAgent.RunPreparedQuestAction()
assert(completedQuest == 90882)
frames[3].scripts.OnUpdate(nil, .2)
assert(overrides["9"] == "CLICK WowAgentTaskDialogue:LeftButton")
local rewardTarget = selected
selected = "Creature-Other"
currentID = 99999
WowAgent.RunPreparedQuestAction()
frames[3].scripts.OnUpdate(nil, .2)
assert(values[4] == 90882 and not activeQuest)
selected = rewardTarget
currentID = 90883
WowAgent.RunPreparedQuestAction()
frames[3].scripts.OnUpdate(nil, .2)
assert(values[4] == 90883 and not activeQuest)
function AcceptQuest() activeQuest = currentID end
WowAgent.RunPreparedQuestAction()
assert(activeQuest == 90883)
function GetQuestReward()
    completedQuest, activeQuest, currentID = 90883, nil, 90885
    event("QUEST_DETAIL")
end
event("QUEST_COMPLETE")
WowAgent.RunPreparedQuestAction()
assert(completedQuest == 90883)
WowAgent.RunPreparedQuestAction()
frames[3].scripts.OnUpdate(nil, .2)
assert(values[4] == 90885 and not activeQuest and values[1] % 128 == 1)
WowAgent.StopQuestNavigation()
WowAgent.PrepareQuestNavigation(90885, 0, 0, true, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(frames[3]:IsShown() and values[4] == 90885)
WowAgent.RunPreparedQuestAction()
assert(activeQuest == 90885)
function GetQuestReward()
    completedQuest, activeQuest, currentID = 90885, nil, 67890
    event("QUEST_DETAIL")
end
event("QUEST_COMPLETE")
WowAgent.RunPreparedQuestAction()
WowAgent.RunPreparedQuestAction()
frames[3].scripts.OnUpdate(nil, .2)
assert(values[4] == 67890 and not activeQuest)
WowAgent.RunPreparedQuestAction()
assert(activeQuest == 67890)
WowAgent.StopQuestNavigation()
activeQuest = nil
completedQuest = nil
event("QUEST_FINISHED")
Enum.QuestLineFloorLocation = {Same = 2}
C_QuestLine.GetAvailableQuestLines = function()
    return {{questID = 90883, x = .5, y = .5, startMapID = 2248, floorLocation = 2,
             isQuestStart = true, isHidden = false}}
end
assert(WowAgent.GetNearbyQuestID() == 90883)
WowAgent.PrepareQuestNavigation(90883, 0, 0, true, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(values[4] == 90883 and values[6] == math.floor(.5 * 1048575 + .5))
WowAgent.StopQuestNavigation()
completedQuest = 90883
C_QuestLine.GetAvailableQuestLines = function()
    return {{questID = 90885, x = .5, y = .5, startMapID = 2248, floorLocation = 2,
             isQuestStart = true, isHidden = false}}
end
assert(WowAgent.GetNearbyQuestID() == 90885)
assert(not next(overrides))

-- The mount objective publishes the verified button position only while it
-- is incomplete and the button can be clicked.
activeQuest = 90883
completedQuest = nil
assert(WowAgent.GetActiveSupportedQuestID() == 90883)
C_QuestLog.ReadyForTurnIn = function() return false end
local progress, visible = 0, true
C_QuestLog.GetQuestObjectives = function() return {{numFulfilled = progress, numRequired = 1}} end
local oldBinding = GetBindingAction
function GetBindingAction(key) return key == "2" and "ACTIONBUTTON2" or oldBinding(key) end
ActionButton2 = {
    action = 2,
    GetCenter = function() return 300, 30 end,
    IsVisible = function() return visible end,
    IsMouseEnabled = function() return true end,
    GetEffectiveScale = function() return 1 end,
}
local placed, cursor = false, nil
C_MountJournal.GetCollectedDragonridingMounts = function() return {10} end
C_MountJournal.GetNumDisplayedMounts = function() return 2 end
C_MountJournal.GetDisplayedMountID = function(index) return index == 1 and 20 or 10 end
C_MountJournal.Pickup = function(index) assert(index == 2); cursor = "mount" end
function GetCursorInfo() return cursor end
function ClearCursor() cursor = nil end
C_ActionBar.HasAction = function(slot) return slot == 2 and placed end
C_ActionBar.PutActionInSlot = function(slot) assert(slot == 2 and cursor == "mount"); placed, cursor = true, nil end
local originalRead = WowAgent.LayoutSupport.ReadAction
WowAgent.LayoutSupport.ReadAction = function(slot)
    if slot == 2 and placed then return "summonmount", 10 end
    return originalRead(slot)
end
UIParent.GetEffectiveScale = function() return 1 end
UIParent.GetLeft = function() return 0 end
UIParent.GetTop = function() return 500 end
UIParent.GetWidth = function() return 1000 end
UIParent.GetHeight = function() return 500 end
WowAgent.PrepareQuestNavigation(90883, 0, 0, true, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(math.floor(values[1] / 512) % 2 == 1)
assert(values[2] == 307 * 1024 + 962)
assert(placed and math.floor(values[1] / 131072) == 2)
visible = false
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 512) % 2 == 1 and values[2] == 0)
visible, progress = true, 1
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 512) % 2 == 0 and values[2] == 0)
WowAgent.StopQuestNavigation()

-- An arbitrary active quest uses native objective evidence, not a configured NPC.
activeQuest = 67890
event("QUEST_FINISHED")
local objectiveKind, tooltipQuest = "monster", activeQuest
C_QuestLog.GetQuestObjectives = function()
    return {{type = objectiveKind, text = "Objective: 0/4", numFulfilled = 0, numRequired = 4}}
end
Enum.TooltipDataLineType, Enum.TooltipDataType = {QuestTitle = 17, QuestObjective = 8}, {Object = 1}
local function objectiveTooltip()
    return {type = 1, lines = {{type = 17, questID = tooltipQuest},
        {type = 8, completed = false, leftText = "0/4 Objective"}}}
end
C_TooltipInfo = {GetUnit = objectiveTooltip, GetWorldCursor = objectiveTooltip}
function GetCursorPosition() return 300, 30 end
function UnitCanAttack() return true end
selected = "Creature-0-0-0-0-654321-01"
WowAgent.PrepareQuestNavigation(activeQuest, 0, 0, true, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(math.floor(values[1] / 131072) == 1 and values[2] == 654321)
tooltipQuest = 11111
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 131072) == 1 and values[2] == 0)
tooltipQuest, objectiveKind = activeQuest, "object"
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 131072) == 2 and values[2] == 307 * 1024 + 962)
WowAgent.StopQuestNavigation()

-- Batch pickup remembers every quest in the same NPC's offer list.
local questState = {}
local oldUnitGUID = UnitGUID
function UnitGUID(unit)
    if unit == "player" then return "Player-1-01" end
    if unit == "questnpc" then return "Creature-0-0-0-0-222222-01" end
    return oldUnitGUID(unit)
end
C_QuestLog.IsOnQuest = function(id) return questState[id] == 2 or questState[id] == 3 end
C_QuestLog.ReadyForTurnIn = function(id) return questState[id] == 3 end
C_QuestLog.IsQuestFlaggedCompleted = function(id) return questState[id] == 4 end
C_QuestLine.GetAvailableQuestLines = function() return {} end
available = {{questID = 70001}, {questID = 70002}}
event("GOSSIP_SHOW")
function CloseQuest() event("QUEST_FINISHED") end
function AcceptQuest()
    questState[currentID] = currentID == 70001 and 3 or 2
    event("QUEST_FINISHED")
end
function GetQuestReward()
    assert(questState[currentID] == 3)
    questState[currentID] = 4
    if currentID == 70001 or currentID == 70003 then
        currentID = currentID == 70001 and 70003 or 70004
        event("QUEST_DETAIL")
    else
        event("QUEST_FINISHED")
    end
end
local function AdvanceRound(stage, quest)
    frames[3].scripts.OnUpdate(nil, .2)
    now = now + 2.1
    frames[3].scripts.OnUpdate(nil, .2)
    assert(math.floor(values[3] / 8) % 2 == 1)
    WowAgent.RunPreparedQuestAction()
    frames[3].scripts.OnUpdate(nil, .2)
    assert(values[3] % 8 == stage and values[4] == quest)
end
WowAgent.PrepareQuestNavigation(70001, 0, 0, true, true, nil, nil, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(values[3] % 8 == 1)
currentID = 70001
event("QUEST_DETAIL")
WowAgent.RunPreparedQuestAction()
assert(questState[70001] == 3)
frames[3].scripts.OnUpdate(nil, .2)
assert(values[3] % 64 == 33)
WowAgent.StopQuestNavigation()
WowAgent.PrepareQuestNavigation(70002, 0, 0, true, true, nil, nil, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(values[3] % 64 == 33 and values[4] == 70001)
event("QUEST_COMPLETE")
WowAgent.RunPreparedQuestAction()
assert(questState[70001] == 4)
AdvanceRound(1, 70003)
WowAgent.RunPreparedQuestAction()
assert(questState[70003] == 2)
AdvanceRound(1, 70002)
currentID = 70002
event("QUEST_DETAIL")
WowAgent.RunPreparedQuestAction()
assert(questState[70002] == 2)
AdvanceRound(2, 70003)
questState[70003] = 3
AdvanceRound(2, 70002)
questState[70002] = 3
AdvanceRound(3, 70003)
currentID = 70003
event("QUEST_COMPLETE")
WowAgent.RunPreparedQuestAction()
assert(questState[70003] == 4 and questState[70002] == 3)
AdvanceRound(3, 70002)
assert(questState[70004] == nil)
currentID = 70002
event("QUEST_COMPLETE")
WowAgent.RunPreparedQuestAction()
AdvanceRound(1, 70004)
WowAgent.StopQuestNavigation()

-- The next farm quest uses its own Grail NPC even when the previous NPC
-- is still selected and the live map pins are clustered together.
C_Map.GetBestMapForUnit = function() return 2451 end
C_Map.GetPlayerMapPosition = function() return {GetXY = function() return .5642, .5973 end} end
C_QuestLine.GetAvailableQuestLines = function()
    return {
        {questID = 90887, x = .5642, y = .5973, startMapID = 2451, floorLocation = 2,
         isQuestStart = true, isHidden = false},
        {questID = 90886, x = .5642, y = .5973, startMapID = 2451, floorLocation = 2,
         isQuestStart = true, isHidden = false},
    }
end
WowAgentDB.questRounds = {}
available = {}
event("QUEST_FINISHED")
questState[90883], questState[90887] = 4, 2
dead = false
soft, selected = "Creature-0-0-0-0-244655-01", "Creature-0-0-0-0-244655-01"
WowAgent.PrepareQuestNavigation(90886, 0, 0, true, true, nil, nil, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(values[4] == 90886 and values[5] == 2451)
assert(values[6] == math.floor(.5633 * 1048575 + .5))
assert(values[7] == math.floor(.5957 * 1048575 + .5))
assert(math.floor(values[1] / 2) % 4 == 0)

C_GossipInfo.CloseGossip = function() event("GOSSIP_CLOSED") end
event("GOSSIP_SHOW")
WowAgent.RunPreparedQuestAction()
assert(questState[90886] == nil)
soft = "Creature-0-0-0-0-244656-01"
npc = "Farm quest giver"
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 2) % 4 == 1)
local selector = namedFrames.WowAgentTaskTarget
assert(selector.attributes.type == "macro" and selector.attributes.unit == nil)
assert(selector.attributes.macrotext == "/targetexact Farm quest giver")
selected = soft
soft = "Creature-0-0-0-0-244655-01"
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 2) % 4 == 3)
available = {{questID = 90886}}
event("GOSSIP_SHOW")
WowAgent.RunPreparedQuestAction()
assert(calls[#calls] == 90886)
currentID = 90886
event("QUEST_DETAIL")
WowAgent.RunPreparedQuestAction()
assert(questState[90886] == 2)
WowAgent.StopQuestNavigation()

-- The pumpkin quest is a world-object interaction even when the quest log
-- classifies its count as an item. Unrelated yellow markers are ignored.
questState[90885] = 2
event("QUEST_FINISHED")
C_QuestLog.GetQuestsOnMap = function()
    return {{questID = 90885, x = .52, y = .59}}
end
C_QuestLog.GetQuestObjectives = function()
    return {{type = "item", text = "找回稀有的南瓜：0/4", numFulfilled = 0, numRequired = 4}}
end
local tooltipObject = "其他南瓜"
C_TooltipInfo.GetWorldCursor = function()
    return {type = Enum.TooltipDataType.Object, lines = {{leftText = tooltipObject}}}
end
C_VignetteInfo = {
    GetVignettes = function() return {"near", "far", "unrelated"} end,
    GetVignetteInfo = function(guid)
        return {name = guid == "unrelated" and "其他南瓜" or "稀有的南瓜",
            objectGUID = "GameObject-0-0-0-0-123-01", onMinimap = true}
    end,
    GetVignettePosition = function(guid, map)
        assert(map == 2451)
        return {GetXY = function() return ({near = .56, far = .54, unrelated = .5642})[guid], .5973 end}
    end,
}
WowAgent.PrepareQuestNavigation(90885, 0, 0, true, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(math.floor(values[1] / 131072) == 4 and values[2] == 0)
assert(values[6] == math.floor(.56 * 1048575 + .5))
tooltipObject = "稀有的南瓜"
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 131072) == 4 and values[2] == 307 * 1024 + 962)
C_VignetteInfo = nil
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[1] / 131072) == 2 and values[6] == math.floor(.52 * 1048575 + .5))
Minimap = {
    IsVisible = function() return true end, GetCenter = function() return 900, 425 end,
    GetWidth = function() return 100 end, GetHeight = function() return 85 end,
    GetEffectiveScale = function() return 1 end,
}
C_Minimap = {GetViewRadius = function() return 233 end}
cvars.rotateMinimap = "1"
frames[3].scripts.OnUpdate(nil, .2)
assert(rowValues[0] == 921 * 1024 + 153 and rowValues[16] == 102 * 1024 + 174)
assert(rowValues[32] % 2048 == 233 + 1024)
assert(math.floor(rowValues[32] / 2048) == math.floor(values[8] / 8192))
assert(math.floor(values[3] / 65536) == 1)
WowAgent.StopQuestNavigation()

-- Fetch is armed only for a known skill and a lootable corpse with a live pet.
C_SpellBook = {IsSpellKnown = function(id) return id == 125050 end}
local spellName = C_Spell.GetSpellName
C_Spell.GetSpellName = function(id) return id == 125050 and "拾回" or spellName(id) end
function UnitExists(unit) return unit == "pet" end
function UnitIsDead(unit) return unit == "target" end
local canLoot = true
function CanLootUnit(guid)
    assert(guid == selected)
    return true, canLoot
end
WowAgent.PrepareQuestNavigation(90885, 0, 0, true, true)
while #timers > 0 do table.remove(timers, 1)() end
assert(overrides.L == "CLICK WowAgentTaskFetch:LeftButton")
assert(namedFrames.WowAgentTaskFetch.attributes.macrotext == "/cast [@target,exists,dead] 拾回")
assert(math.floor(values[3] / 8192) % 8 == 7)
canLoot = false
frames[3].scripts.OnUpdate(nil, .2)
assert(math.floor(values[3] / 8192) % 8 == 1)
WowAgent.StopQuestNavigation()
assert(not overrides.L)
