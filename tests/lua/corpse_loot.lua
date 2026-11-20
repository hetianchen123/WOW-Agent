local registered, payload = {}, 0
WowAgent = {
    NewPixelRow = function(_, offset)
        assert(offset == -448)
        return function(kind, value)
            assert(kind == 6)
            payload = value
        end
    end,
    CombatLootTargetEpoch = function(guid)
        if guid == "creature" then return 4 end
    end,
}
local selected, source = "creature", "creature"
function UnitGUID() return selected end
function GetNumLootItems() return 3 end
function GetLootSlotType(slot) return slot == 2 and 0 or 1 end
function GetLootSourceInfo() return source, 1 end

dofile("addon/WowAgent/CorpseLoot.lua")
local core = WowAgent.CorpseLoot
core.Register({RegisterEvent = function(_, event) registered[event] = true end})
assert(registered.LOOT_READY and registered.LOOT_OPENED and registered.LOOT_CLOSED)

local function Open(fromItem)
    core.Event("LOOT_READY")
    core.Event("LOOT_OPENED", true, fromItem)
end

Open(false)
core.Event("LOOT_SLOT_CLEARED", 1)
core.Event("LOOT_SLOT_CLEARED", 1)
core.Event("LOOT_CLOSED")
core.Publish(1)
assert(payload == 8192)

Open(false)
selected = nil
core.Event("LOOT_SLOT_CLEARED", 1)
core.Event("LOOT_SLOT_CLEARED", 3)
core.Event("LOOT_CLOSED")
core.Publish(2)
assert(payload == 2 * 8192 + 64 + 4)
core.Publish(2, true)
assert(payload == 2 * 8192 + 4096 + 64 + 4)

local function AssertNoReceipt(fromItem, reset)
    Open(fromItem)
    core.Event("LOOT_SLOT_CLEARED", 1)
    core.Event("LOOT_SLOT_CLEARED", 3)
    if reset then core.Event(reset) end
    core.Event("LOOT_CLOSED")
    core.Publish(3)
    assert(payload == 3 * 8192 + 64 + 4)
end

AssertNoReceipt(true)
AssertNoReceipt(false, "PLAYER_DEAD")
source = "unknown"
AssertNoReceipt(false)
