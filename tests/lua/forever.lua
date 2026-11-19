-- Run from the repository root with Lua 5.1 or lupa.lua51.
local clock, level, money, combat = 1, 2, 10, false
local learned, rank, staged, buys, purchases, commits = {}, 0, false, 0, 0, 0
local trainerVisible, status, target = false, "available", 248415
local gossipVisible, optionIcon, gossipSelection = false, 132049, nil
local events

UIParent, SlashCmdList = {}, {}
function GetBuildInfo() return "1.60.1", "70959", "", 16001 end
function GetTime() return clock end
function UnitClass() return "Hunter", "HUNTER", 3 end
function UnitLevel() return level end
function UnitIsDeadOrGhost() return false end
function UnitIsDead() return false end
function UnitCanAttack() return false end
function UnitAffectingCombat() return combat end
function InCombatLockdown() return combat end
function UnitGUID() return "Creature-0-1-0-1-" .. target .. "-00000ABC" end
function GetBindingAction() return "CLICK WowAgentForeverProgress:LeftButton" end
function SetOverrideBindingClick() end
function GetMoney() return money end
function GetNumTrainerServices() return 1 end
function GetTrainerServiceInfo() return "Raptor Strike", status end
function GetTrainerServiceCost() return 10, false end
function BuyTrainerService(i) assert(i == 1); buys = buys + 1 end
function CloseTrainer() trainerVisible = false end
function UnitCharacterPoints() return 1 end
function GetCombatConfigIDForSpecGroup() return 42 end
function CreateFrame(kind, name)
    local frame = {RegisterEvent = function() end, RegisterForClicks = function() end}
    function frame:SetScript(event, fn) self[event] = fn end
    if name then _G[name] = frame else events = frame end
    return frame
end
ClassTrainerFrame = {IsShown = function() return trainerVisible end}
GossipFrame = {IsShown = function() return gossipVisible end}
C_GossipInfo = {
    GetOptions = function() return {{icon = optionIcon, gossipOptionID = 300, status = 0}} end,
    SelectOption = function(id) gossipSelection = id end,
}
Enum = {GossipOptionStatus = {Available = 0}}
C_Map = {
    GetBestMapForUnit = function() return 1429 end,
    GetPlayerMapPosition = function() return {GetXY = function() return .51, .41 end} end,
}
C_TooltipInfo = {GetTrainerService = function() return {id = 2973} end}
C_SpecializationInfo = {GetActiveSpecGroup = function() return 1 end}
C_Traits = {
    ConfigHasStagedChanges = function() return staged end,
    GetNodeInfo = function(config, node)
        assert(config == 42 and node == 104960)
        return {activeRank = rank, canPurchaseRank = true, entryIDs = {1}}
    end,
    GetEntryInfo = function() return {definitionID = 2} end,
    GetDefinitionInfo = function() return {spellID = 19552} end,
    PurchaseRank = function() purchases = purchases + 1; staged = true; return true end,
    CommitConfig = function() commits = commits + 1; return true end,
}

dofile("addon/WowAgent/Forever.lua")
local forever = WowAgent.Forever
WowAgent.LayoutSupport = {
    LearnedSpells = function() return learned end,
    ActionMatches = function() return true end,
}
learned[75], learned[3044] = "自动射击", "奥术射击"
assert(forever.OutputSpell(learned) == 3044)
assert(forever.OutputMacro(3044, learned) == "#showtooltip 奥术射击\n/cast [harm,nodead] !自动射击\n/cast [harm,nodead] 奥术射击")
assert(forever.OutputMacro(75, {[75] = "/run\nbad"}) == nil)

local function Observe(plan)
    local value = forever.Progression(plan, 1429)
    return value and value.code, value and value.value
end
local function Click()
    WowAgentForeverProgress.OnClick()
end

local plan = {foreverTrain = true}
forever.BindProgression({}, plan)
assert(Observe(plan) == 1)
gossipVisible = true
Click()
assert(gossipSelection == nil)
optionIcon, plan.foreverSkipLevel, plan.foreverReceipt = 132058, nil, nil
assert(Observe(plan) == 3)
Click()
assert(gossipSelection == 300 and buys == 0)
gossipVisible = false
trainerVisible, target = true, 999
events:OnEvent("TRAINER_SHOW")
Click()
assert(buys == 0)

target, plan.foreverSkipLevel, plan.foreverReceipt = 248415, nil, nil
events:OnEvent("TRAINER_SHOW")
money = 9
assert(Observe(plan) == 13)
money = 10
assert(Observe(plan) == 4)
Click()
assert(buys == 1 and Observe(plan) == 5)
Click()
assert(buys == 1 and Observe(plan) == 5)
status, learned[2973] = "used", "猛禽一击"
local code, value = Observe(plan)
assert(code == 8 and value == 2973)
clock = 3
assert(Observe(plan) == 13)
Click()
assert(Observe(plan) == 11 and not trainerVisible)

clock, level, combat = 5, 10, true
assert(Observe(plan) == nil)
combat, staged = false, true
assert(Observe(plan) == 1)
staged = false
assert(Observe(plan) == 6)
Click()
assert(purchases == 1 and commits == 1 and Observe(plan) == 5)
rank = 1
assert(Observe(plan) == 5)
staged = false
code, value = Observe(plan)
assert(code == 9 and value == 104960)
print("forever progression ok")
