local registered, payload, fullPayload = {}, 0, 0
local timers, clock = {}, 10
local closes = 0
LE_GAME_ERR_INV_FULL = 0
function GetTime() return clock end
C_Timer = {After = function(_, action) timers[#timers + 1] = action end}
WowAgent = {
    NewPixelRow = function(_, offset)
        assert(offset == -448 or offset == -480)
        return function(kind, value)
            if offset == -448 then assert(kind == 6); payload = value
            else assert(kind == 8); fullPayload = value end
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
function CloseLoot() closes = closes + 1; core.Event("LOOT_CLOSED") end
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

source, selected = "creature", "creature"
assert(not core.Skipped(source))
core.Event("LOOT_READY")
core.Event("LOOT_OPENED", true, false, true)
core.Event("UI_ERROR_MESSAGE", LE_GAME_ERR_INV_FULL)
core.Event("LOOT_SLOT_CLEARED", 1) -- Other slots are still allowed to finish.
timers[1]() -- Earlier close was superseded by the new loot progress.
assert(closes == 0)
timers[2]()
assert(closes == 1 and core.Skipped(source))
core.Publish(4)
assert(payload == 4 * 8192 + 64 + 4) -- The clear receipt has not advanced.
assert(fullPayload == 4 * 8192 + 4096 + 64 + 4)

clock = 101
assert(not core.Skipped(source))
Open(false) -- Manual auto-loot, no managed interaction.
core.Event("UI_ERROR_MESSAGE", LE_GAME_ERR_INV_FULL)
core.Event("LOOT_CLOSED")
assert(closes == 1 and core.fullSerial == 1)
core.Event("LOOT_READY")
core.Event("LOOT_OPENED", true, false, true)
core.Event("UI_ERROR_MESSAGE", 999, "unrelated error")
core.Event("LOOT_CLOSED")
assert(closes == 1 and core.fullSerial == 1)
