-- Verify pickup preparation and dialogue actions against the same quest ID.
local frames, values, calls, messages = {}, {}, {}, {}
local active, combat, prepared, currentID = false, false, false, 0
local available = {{questID = 90882, title = "若是那豺狼来了"}}
Enum = {QuestClassification = {Normal = 0}}
UIParent, WowAgentChannelFrame = {}, {}
function CreateFrame()
    local frame = {shown = false, scripts = {}}
    function frame:SetScript(event, handler) self.scripts[event] = handler end
    function frame:Show() self.shown = true end
    function frame:Hide() self.shown = false end
    function frame:IsShown() return self.shown end
    setmetatable(frame, {__index = function() return function() end end})
    frames[#frames + 1] = frame
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
    NewPixelRow = function() return function(kind, value) values[kind] = value end end,
    LayoutSupport = {SetupAction = function(kind, id, key)
        assert(kind == "quest" and id == 90882 and key == 9)
        if not prepared then print("macro creation failed") end
        return prepared
    end},
}
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
WowAgent.StopQuestNavigation()
assert(not next(overrides))

-- Only the configured living soft target becomes eligible for F.
active = true
mounted, gliding, slotOccupied = false, false, nil
C_QuestLog.ReadyForTurnIn = function() return true end
C_QuestLog.IsQuestFlaggedCompleted = function() return false end
Enum.SoftTargetEnableFlags = {Any = 3}
local cvars = {}
C_CVar = {
    GetCVar = function(key) return cvars[key] end,
    SetCVar = function(key, value) cvars[key] = tostring(value) end,
}
local soft, selected, dead, npc = "Creature-NPC", "Creature-Corpse", true, "萨尔"
function UnitGUID(unit) return unit == "softinteract" and soft or selected end
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
assert(values[1] % 128 == 33)
dead = false
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 128 == 35)
selected = soft
frames[3].scripts.OnUpdate(nil, .2)
assert(values[1] % 128 == 39)
WowAgent.StopQuestNavigation()
assert(not next(overrides))

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
