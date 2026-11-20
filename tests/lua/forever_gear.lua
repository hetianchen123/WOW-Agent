local clock, level, money = 1, 5, 50
local combat, dead, gossip, merchant = false, false, false, false
local equipped, bags = {}, {}
local wear, repairCalls, equipCalls = {}, 0, 0
local targetID, npcID, merchantRepairs, cost = 2046, nil, true, 35
local vendorIcon, optionSelected = 132049, nil
local mapX, mapY = .415, .67
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
    GetPlayerMapPosition = function() return {GetXY = function() return mapX, mapY end} end,
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

-- Town service runs with healthy equipment and sells only eligible bag slots.
local function BagItem(id, equipType, itemLevel, quality, class, price)
    local item = Item(id, equipType, itemLevel)
    item.quality, item.classID, item.price, item.stackCount = quality, class, price, 1
    item.hasNoValue, item.hasLoot, item.isReadable = false, false, false
    item.usable = false
    return item
end
local obsolete = BagItem(201, "INVTYPE_CHEST", 2, 1, 4, 7)
local shield = BagItem(208, "INVTYPE_SHIELD", 10, 1, 4, 15)
local gray = BagItem(202, "", 1, 0, 15, 5)
gray.stackCount = 3
local questItem = BagItem(203, "", 1, 0, 15, 20)
questItem.quest = 42
local better = BagItem(204, "INVTYPE_CHEST", 55, 1, 4, 10)
better.usable, obsolete.usable = true, true
local uncommon = BagItem(205, "INVTYPE_CHEST", 1, 2, 4, 40)
local locked = BagItem(206, "", 1, 0, 15, 5)
locked.isLocked = true
local supplies = BagItem(207, "", 1, 1, 0, 1)
local saleCalls = 0
bags = {shield, gray, questItem, better, uncommon, locked, supplies}
equipped[5] = obsolete
wear[5] = 30
C_Item.GetItemInfo = function(link)
    local item = items[link]
    return "gear", link, item.quality, item.level, item.required, "type", "subtype", 20,
        item.equipType, 100, item.price, item.classID, 0, 0
end
C_Container.GetContainerNumSlots = function(bag) return bag == 0 and 7 or 0 end
C_Container.GetContainerItemQuestInfo = function(bag, index)
    return {isQuestItem = bags[index].quest ~= nil, isActive = false, questID = bags[index].quest}
end
local saleIndex
C_Container.UseContainerItem = function(bag, index)
    assert(bag == 0 and merchant)
    saleCalls, saleIndex = saleCalls + 1, index
end
clock, mapX = 40, .415
plan = Plan()
assert(Observe(plan, 15).id == better.itemID)
Click()
Observe(plan, 16)
equipped[5], bags[4] = better, obsolete
Observe(plan, 17)
clock = 42
trip = Observe(plan, 19)
targetID, npcID, merchant = trip.npc.id, trip.npc.id, true
merchantEvents:OnEvent("MERCHANT_SHOW")
assert(Observe(plan, 28).value == 15) -- Gray stack before white gear.
bags[2], bags[3] = questItem, gray
Click()
assert(saleCalls == 0) -- Same item moved to a different slot after the offer.
bags[2], bags[3] = gray, questItem
Click()
assert(saleCalls == 1 and saleIndex == 2)
Observe(plan, 29)
Click()
assert(saleCalls == 1)
bags[2] = nil
Observe(plan, 29) -- Slot change alone is not proof of a sale.
money = money + 15
assert(Observe(plan, 30).value == 15)
clock = 44
assert(Observe(plan, 28).link == shield.hyperlink) -- Ineligible shield with an empty off hand.
Click()
assert(saleCalls == 2 and saleIndex == 1)
bags[1], money = nil, money + 15
assert(Observe(plan, 30).value == 15)
clock = 46
assert(Observe(plan, 28).link == obsolete.hyperlink) -- The replaced, still usable white item.
Click()
assert(saleCalls == 3 and saleIndex == 4)
bags[4], money = nil, money + 7
assert(Observe(plan, 30).value == 7)
clock = 48
Observe(plan, 26)
Click()
assert(not merchant and saleCalls == 3 and bags[3] == questItem and equipped[5] == better
    and bags[5] == uncommon and bags[6] == locked and bags[7] == supplies)
Observe(plan, nil)

-- Sell before quoting a repair that the initial balance cannot afford.
clock, money, wear[5] = 49, 10, 0
gray.stackCount, bags[2] = 5, gray
plan = Plan()
trip = Observe(plan, 19)
targetID, npcID, merchant = trip.npc.id, trip.npc.id, true
merchantEvents:OnEvent("MERCHANT_SHOW")
assert(Observe(plan, 28).value == 25)
Click()
assert(saleCalls == 4 and repairCalls == 2)
bags[2], money = nil, 35
Observe(plan, 30)
clock = 51
Observe(plan, 21)
Click()
assert(repairCalls == 3)

-- An equip timeout leaves a viable upgrade in the bag instead of selling it.
merchant, wear[5], clock = false, 30, 56
equipped[5], bags[4] = obsolete, better
plan = Plan()
Observe(plan, 15)
Click()
clock = 61
Observe(plan, 18)
clock = 63
Observe(plan, nil)
assert(saleCalls == 4 and bags[4] == better)
