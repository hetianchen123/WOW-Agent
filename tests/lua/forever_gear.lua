local clock, level, money = 1, 5, 50
local combat, dead, gossip, merchant = false, false, false, false
local equipped, bags = {}, {}
local wear, repairCalls, equipCalls = {}, 0, 0
local targetID, npcID, merchantRepairs, cost = 2046, nil, true, 35
local vendorIcon, optionSelected = 132049, nil
local frames = {}

WowAgent, UIParent, SlashCmdList = {}, {}, {}
function GetBuildInfo() return "1.60.1", "70959", "", 16001 end
function GetTime() return clock end
function UnitLevel() return level end
function UnitClass() return "Hunter", "HUNTER", 3 end
function UnitFactionGroup() return "Alliance" end
function UnitIsDeadOrGhost() return dead end
function UnitAffectingCombat() return combat end
function InCombatLockdown() return combat end
function UnitIsDead() return false end
function UnitCanAttack() return false end
function GetCursorInfo() end
function GetMoney() return money end
function GetInventoryItemID(_, slot) return equipped[slot] and equipped[slot].itemID end
function GetInventoryItemLink(_, slot) return equipped[slot] and equipped[slot].hyperlink end
function GetInventoryItemDurability(slot)
    if wear[slot] ~= nil then return wear[slot], 30 end
end
function UnitGUID(unit)
    if unit == "player" then return "Player-1-1" end
    local id = unit == "target" and targetID or unit == "npc" and npcID
    return id and "Creature-0-1-0-1-" .. id .. "-00000ABC" or nil
end
function CreateFrame(_, name)
    local frame = {RegisterEvent = function() end, RegisterForClicks = function() end}
    function frame:SetScript(event, fn) self[event] = fn end
    if name then _G[name] = frame else frames[#frames + 1] = frame end
    return frame
end
function GetBindingAction() return "CLICK WowAgentForeverProgress:LeftButton" end
function SetOverrideBindingClick() end
GossipFrame = {IsShown = function() return gossip end}
MerchantFrame = {IsShown = function() return merchant end}
C_GossipInfo = {
    GetOptions = function() return {{gossipOptionID = 55, icon = vendorIcon, status = 0}} end,
    SelectOption = function(id) optionSelected = id end,
    CloseGossip = function() gossip = false end,
}
Enum = {GossipOptionStatus = {Available = 0}}
C_Map = {
    GetBestMapForUnit = function() return 1429 end,
    GetMapWorldSize = function() return 3470.8, 2314.6 end,
    GetPlayerMapPosition = function() return {GetXY = function() return .415, .67 end} end,
}
function CanMerchantRepair() return merchantRepairs end
function GetRepairAllCost() return cost, true end
function RepairAllItems(...)
    assert(select("#", ...) == 0 and money >= cost and merchantRepairs)
    repairCalls = repairCalls + 1
end
function CloseMerchant() merchant = false end

local items = {}
local function Item(id, equipType, itemLevel, required)
    local link = "|Hitem:" .. id .. ":0|h[gear]|h"
    local item = {itemID = id, hyperlink = link, isLocked = false, equipType = equipType,
        level = itemLevel, required = required or 1, usable = true}
    items[link] = item
    return item
end

C_Item = {
    GetDetailedItemLevelInfo = function(link) return items[link].level end,
    GetItemInfoInstant = function(link)
        local item = items[link]
        return item.itemID, nil, nil, item.equipType
    end,
    GetItemInfo = function(link)
        return "gear", link, 1, items[link].level, items[link].required
    end,
    IsUsableItem = function(link) return items[link].usable end,
    GetItemUniquenessByID = function(link) return items[link].unique == true end,
    EquipItemByName = function(link, slot)
        equipCalls = equipCalls + 1
        items[link].offeredSlot = slot
    end,
}
C_Container = {
    GetContainerNumSlots = function(bag) return bag == 0 and #bags or 0 end,
    GetContainerItemInfo = function(_, slot) return bags[slot] end,
    GetContainerItemDurability = function(_, slot) return bags[slot].durability, 20 end,
}

dofile("addon/WowAgent/TaskBindings.lua")
dofile("addon/WowAgent/Forever.lua")
dofile("addon/WowAgent/ForeverGear.lua")
WowAgent.LayoutSupport = {
    LearnedSpells = function() return {[75] = "Auto Shot"} end,
    ArmedActionMatches = function() return true end,
}
local forever = WowAgent.Forever
local merchantEvents = frames[#frames]
local function Observe(plan, expected)
    local action = forever.Progression(plan, 1429)
    assert((action and action.code) == expected, "unexpected state " .. tostring(action and action.code))
    plan.foreverProgress = action
    return action
end
local function Click() WowAgentForeverProgress.OnClick() end
local function Plan()
    local plan = {automatic = true}
    forever.BindProgression({}, plan)
    return plan
end

local oldChest = Item(101, "INVTYPE_CHEST", 4)
local upgrade = Item(102, "INVTYPE_ROBE", 9)
local belt = Item(103, "INVTYPE_WAIST", 3)
local ring = Item(104, "INVTYPE_FINGER", 7)
local forbidden = Item(105, "INVTYPE_HEAD", 70)
forbidden.usable = false
local tooHigh = Item(106, "INVTYPE_HAND", 80, 10)
local broken = Item(107, "INVTYPE_FEET", 90)
broken.durability = 0
local locked = Item(108, "INVTYPE_SHOULDER", 100)
locked.isLocked = true
local equal = Item(109, "INVTYPE_CHEST", 4)
local twoHanded = Item(110, "INVTYPE_2HWEAPON", 120)
local uniqueRing = Item(111, "INVTYPE_FINGER", 8)
uniqueRing.unique = true
equipped[5], equipped[11], equipped[17] = oldChest, uniqueRing, Item(112, "INVTYPE_SHIELD", 4)
bags = {upgrade, belt, ring, forbidden, tooHigh, broken, locked, equal, twoHanded, uniqueRing}

local plan = Plan()
combat = true
Observe(plan, nil)
combat = false
assert(Observe(plan, 15).value == 12)
bags[3] = equal
Click()
assert(equipCalls == 0) -- The offered bag item has moved since the rendered frame.
bags[3] = ring
clock = 2
assert(Observe(plan, 15).id == ring.itemID)
Click()
assert(equipCalls == 1 and ring.offeredSlot == 12)
Observe(plan, 16)
Click()
assert(equipCalls == 1)
equipped[12], bags[3] = ring, equal
Observe(plan, 17)
clock = 4
assert(Observe(plan, 15).value == 6)
Click()
equipped[6], bags[2] = belt, equal
Observe(plan, 17)
clock = 6
assert(Observe(plan, 15).id == upgrade.itemID)
Click()
assert(equipCalls == 3 and upgrade.offeredSlot == 5)
clock = 11
Observe(plan, 18) -- No native equipment readback, so the attempt expires.
clock = 13
Observe(plan, nil)

-- Only a zero-durability equipped slot opens a repair detour.
bags, wear[5], wear[6] = {}, 1, 29
plan = Plan()
Observe(plan, nil)
wear[5], wear[6] = 0, 0
local trip = Observe(plan, 19)
assert(trip.npc.id == 2046 or trip.npc.id == 54)
targetID, npcID = trip.npc.id, trip.npc.id
gossip = true
Observe(plan, 22) -- A quest/chat icon is never selected as a merchant option.
assert(optionSelected == nil)
clock = 20
Observe(plan, 26)
Click()
Observe(plan, 25)
clock = 22
trip = Observe(plan, 19)
targetID, npcID = trip.npc.id, trip.npc.id
vendorIcon, gossip = 132060, true
Observe(plan, 20)
Click()
assert(optionSelected == 55)
Observe(plan, 22)
gossip, merchant, npcID = false, true, 999
merchantEvents:OnEvent("MERCHANT_SHOW")
Observe(plan, 22)
Click()
assert(repairCalls == 0)

-- The verified merchant quotes a price, but a short balance defers the visit.
npcID, money = targetID, 10
merchantEvents:OnEvent("MERCHANT_SHOW")
assert(Observe(plan, 27).value == 35)
Click()
assert(Observe(plan, 24).value == 35 and not merchant and repairCalls == 0)
clock = 24
Observe(plan, nil)
money = 35
trip = Observe(plan, 19)
targetID, npcID, merchant = trip.npc.id, trip.npc.id, true
merchantEvents:OnEvent("MERCHANT_SHOW")
Observe(plan, 21)
Click()
assert(repairCalls == 1)
Observe(plan, 22)
Click()
assert(repairCalls == 1)
wear[5] = 30
Observe(plan, 22)
wear[6] = 30
assert(Observe(plan, 23).value == 35 and not merchant)
clock = 26
Observe(plan, nil)

wear[5], dead = 0, true
Observe(plan, nil)
dead = false
trip = Observe(plan, 19)
targetID, npcID, merchant = trip.npc.id, trip.npc.id, true
merchantEvents:OnEvent("MERCHANT_SHOW")
Observe(plan, 21)
Click()
clock = 32
Observe(plan, 25)
assert(repairCalls == 2 and not merchant)
