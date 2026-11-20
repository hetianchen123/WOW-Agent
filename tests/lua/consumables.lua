WowAgent = {Forever = {}}
local now, hp, mana, fighting = 0, 100, 100, false
local counts = {[118] = 1, [117] = 2, [159] = 2}
local frames, bindings = {}, {F1 = "TARGETSELF", F3 = "TARGETPARTYMEMBER2"}
GetTime = function() return now end
UnitHealth = function() return hp end
UnitHealthMax = function() return 100 end
UnitPowerType = function() return 0 end
UnitPower = function() return mana end
UnitPowerMax = function() return 100 end
InCombatLockdown = function() return fighting end
GetBindingAction = function(key) return bindings[key] or "" end
SetOverrideBindingClick = function(_, _, key, frame)
    bindings[key] = "CLICK " .. frame .. ":LeftButton"
end
CreateFrame = function(_, name)
    local frame = {RegisterForClicks = function() end,
        SetAttribute = function(self, key, value) self[key] = value end,
        GetAttribute = function(self, key) return self[key] end,
        SetScript = function(self, key, call) self[key] = call end,
        HookScript = function(self, key, call) self[key] = call end}
    if name then frames[name] = frame end
    return frame
end
bit = {band = function(_, _) return 0 end}
C_Item = {
    GetItemCount = function(id) return counts[id] or 0 end,
    GetItemSpell = function(id) return counts[id] and "Recover" or nil end,
    IsUsableItem = function() return true end,
    GetItemCooldown = function() return 0, 0, true end,
}
dofile("addon/WowAgent/TaskBindings.lua")
dofile("addon/WowAgent/Recovery.lua")

local plan = WowAgent.PrepareRecovery({})
local function Check(state, supply)
    local value, id = WowAgent.PlayerConsumables(plan, 1, 0)
    assert(math.floor(value / 16384) == state and id == supply)
end
Check(0, 0)
local value = WowAgent.PlayerConsumables(nil, 1, 0)
assert(value == 101 + 101 * 128)
UnitHealthPercent = function() return .73 end
hp = nil
assert(WowAgent.PlayerConsumables(nil, 1, 0) == 74 + 101 * 128)
assert(WowAgent.PlayerConsumables(nil, 3, 0) == 0)
fighting, hp = true, 28
Check(1, 118)
local potion = frames.WowAgentRecoveryPotion
potion.PreClick(potion)
Check(0, 0)
counts[118], hp = 0, 57
Check(9, 118)
now, fighting = 3, false
Check(2, 117)
local rest = frames.WowAgentRecoveryFood
rest.PreClick(rest)
now = 6
Check(14, 117) -- The food count stayed unchanged.
now, hp, mana = 9, 95, 30
Check(3, 159)
fighting = true
Check(0, 0)
fighting = false
bindings.F3 = "CUSTOM"
assert(WowAgent.PrepareRecovery({}).Food == nil and bindings.F3 == "CUSTOM")
