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
