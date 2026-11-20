local forever = WowAgent.Forever
if not forever then return end

local gear = {}
forever.Gear = gear

-- Codes above 14 use the extended Forever progression row.
local step = {equip = 15, equipWait = 16, equipped = 17, equipFailed = 18,
    repairTravel = 19, repairGossip = 20, repair = 21, repairWait = 22,
    repaired = 23, repairFunds = 24, repairFailed = 25, repairClose = 26, repairCannotPay = 27}

local slotsByType = {
    INVTYPE_HEAD = {1}, INVTYPE_NECK = {2}, INVTYPE_SHOULDER = {3}, INVTYPE_BODY = {4},
    INVTYPE_CHEST = {5}, INVTYPE_ROBE = {5}, INVTYPE_WAIST = {6}, INVTYPE_LEGS = {7},
    INVTYPE_FEET = {8}, INVTYPE_WRIST = {9}, INVTYPE_HAND = {10}, INVTYPE_FINGER = {11, 12},
    INVTYPE_TRINKET = {13, 14}, INVTYPE_CLOAK = {15}, INVTYPE_WEAPON = {16, 17},
    INVTYPE_2HWEAPON = {16}, INVTYPE_WEAPONMAINHAND = {16}, INVTYPE_WEAPONOFFHAND = {17},
    INVTYPE_SHIELD = {17}, INVTYPE_HOLDABLE = {17}, INVTYPE_RANGED = {18},
    INVTYPE_RANGEDRIGHT = {18}, INVTYPE_THROWN = {18}, INVTYPE_RELIC = {18},
    INVTYPE_TABARD = {19},
}

-- Repair flags and approach points from QuestieDB's Forever NPC data.
local repairers = {
    {id = 78, map = 1429, x = .4724, y = .4190, enUS = "Janos Hammerknuckle", zhCN = "亚努斯·哈默纳克"},
    {id = 1213, map = 1429, x = .4769, y = .4142, enUS = "Godric Rothgar", zhCN = "高德瑞克·洛斯迦"},
    {id = 54, map = 1429, x = .4153, y = .6590, enUS = "Corina Steele", zhCN = "科瑞娜·斯蒂利"},
    {id = 2046, map = 1429, x = .4170, y = .6586, enUS = "Andrew Krighton", zhCN = "安德温·克里顿"},
}
for _, npc in ipairs(repairers) do
    forever.questData.npcNames[npc.id] = npc
end

local function Public(value)
    if not issecretvalue or not issecretvalue(value) then return value end
end

local function Read(api, ...)
    if type(api) ~= "function" then return end
    local ok, first, second, third = pcall(api, ...)
    if ok then return Public(first), Public(second), Public(third) end
end

local function Shown(frame)
    return frame and frame:IsShown()
end

local function Safe()
    return not InCombatLockdown() and Read(UnitAffectingCombat, "player") == false
        and Read(UnitIsDeadOrGhost, "player") == false and not Read(IsFlying) and not Read(IsFalling)
        and not Read(IsSwimming) and not Read(UnitCastingInfo, "player") and not Read(UnitChannelInfo, "player")
        and type(GetCursorInfo) == "function" and not GetCursorInfo() and not Read(SpellIsTargeting)
        and not (Shown(QuestFrame) or Shown(ClassTrainerFrame) or Shown(LootFrame)
            or Shown(TradeFrame) or Shown(BankFrame) or Shown(MailFrame) or Shown(AuctionHouseFrame))
end

local function ItemLevel(link)
    local level = Read(C_Item.GetDetailedItemLevelInfo or GetDetailedItemLevelInfo, link)
    if type(level) == "number" and level >= 0 then return level end
end

local function ItemKey(link)
    return type(link) == "string" and link:match("|H(item:[^|]+)|h") or nil
end

local function ItemType(link)
    local api = C_Item.GetItemInfoInstant or GetItemInfoInstant
    if not api then return end
    local ok, _, _, _, equipType = pcall(api, link)
    if ok then return Public(equipType) end
end

local function Equipment()
    local slots = {}
    for slot = 1, 19 do
        local ok, id = pcall(GetInventoryItemID, "player", slot)
        if ok and (not issecretvalue or not issecretvalue(id)) then
            local link = id and Read(GetInventoryItemLink, "player", slot)
            slots[slot] = {id = id, link = link, level = link and ItemLevel(link) or not id and -1 or nil}
        end
    end
    return slots
end

local function UpgradeSlot(slots, equipType, level, id, unique, category, limit)
    local main, off = slots[16], slots[17]
    local mainType = main and main.link and ItemType(main.link)
    local offhandAvailable = main and (not main.id or mainType and mainType ~= "INVTYPE_2HWEAPON")
    local best
    for _, slot in ipairs(slotsByType[equipType]) do
        local current = slots[slot]
        local usableSlot = (slot ~= 17 or offhandAvailable and (equipType ~= "INVTYPE_WEAPON"
            and equipType ~= "INVTYPE_WEAPONOFFHAND" or main.id and Read(CanDualWield) == true))
            and (equipType ~= "INVTYPE_2HWEAPON" or off and not off.id)
        if usableSlot and (unique or category and type(limit) == "number" and limit > 0) then
            local matches = 0
            for other, equipped in pairs(slots) do
                if other ~= slot and equipped.id then
                    if unique and equipped.id == id then usableSlot = false end
                    if category and equipped.link then
                        local equippedCategory = Read(C_Item.GetItemUniqueness, equipped.link)
                        if category == equippedCategory then matches = matches + 1 end
                    end
                end
            end
            if type(limit) == "number" and limit > 0 and matches >= limit then usableSlot = false end
        end
        if usableSlot and current and current.level and current.level < level
            and (not best or current.level < slots[best].level) then
            best = slot
        end
    end
    return best
end

local function Upgrade(state)
    if not C_Item or not C_Container or type(GetInventoryItemID) ~= "function"
        or type(C_Item.EquipItemByName or EquipItemByName) ~= "function" then return end
    local playerLevel = Read(UnitLevel, "player")
    if type(playerLevel) ~= "number" then return end
    local slots, best = Equipment()
    for bag = 0, NUM_BAG_SLOTS or 4 do
        local count = Read(C_Container.GetContainerNumSlots, bag)
        for index = 1, type(count) == "number" and count or 0 do
            local info = Read(C_Container.GetContainerItemInfo, bag, index)
            if type(info) == "table" and (not canaccesstable or canaccesstable(info)) then
                local link, id = Public(info.hyperlink), Public(info.itemID)
                local key = ItemKey(link)
                local equipType = key and ItemType(link)
                if slotsByType[equipType] and type(id) == "number" and Public(info.isLocked) == false
                    and (state.failed[key] or 0) <= GetTime() and Read(C_Item.IsUsableItem, link) == true then
                    local ok, _, _, _, _, required = pcall(C_Item.GetItemInfo or GetItemInfo, link)
                    local durability, maximum = Read(C_Container.GetContainerItemDurability, bag, index)
                    local level = ItemLevel(link)
                    required = ok and Public(required)
                    if level and type(required) == "number" and required <= playerLevel
                        and not (durability == 0 and type(maximum) == "number" and maximum > 0) then
                        local unique = Read(C_Item.GetItemUniquenessByID, link)
                        local category, limit = Read(C_Item.GetItemUniqueness, link)
                        if type(category) ~= "number" or category <= 0 then category = nil end
                        local slot = UpgradeSlot(slots, equipType, level, id, unique, category, limit)
                        if slot then
                            local previous = slots[slot].level
                            if not best or previous == -1 and best.previous ~= -1
                                or (previous == -1) == (best.previous == -1) and level > best.level then
                                best = {code = step.equip, value = slot, link = link, key = key,
                                    id = id, level = level, previous = previous}
                            end
                        end
                    end
                end
            end
        end
    end
    return best
end

local function Durability()
    local damaged, lowest = {}, nil
    for slot = 1, 19 do
        local current, maximum = Read(GetInventoryItemDurability, slot)
        if type(current) == "number" and type(maximum) == "number" and maximum > 0 and current >= 0 then
            local percent = math.floor(100 * current / maximum)
            lowest = math.min(lowest or percent, percent)
            if current == 0 then
                damaged[#damaged + 1] = {slot = slot, id = Read(GetInventoryItemID, "player", slot)}
            end
        end
    end
    return damaged, lowest
end

function gear.Inspect()
    local damaged, lowest = Durability()
    print("WA GEAR", lowest or "unknown", #damaged, type(C_Item and C_Item.EquipItemByName),
        type(GetInventoryItemDurability), type(RepairAllItems))
    if Safe() then
        local upgrade = Upgrade({failed = {}})
        if upgrade then print("WA GEAR UPGRADE", upgrade.value, upgrade.previous, upgrade.level, upgrade.link) end
    end
end

local function CreatureGUID(unit, expected)
    local guid = Read(UnitGUID, unit)
    if type(guid) == "string" and tonumber(guid:match("^Creature%-%d+%-%d+%-%d+%-%d+%-(%d+)%-")) == expected
        and Read(UnitIsDead, unit) == false and Read(UnitCanAttack, "player", unit) == false then
        return guid
    end
end

local merchantGUID
local events = CreateFrame("Frame")
events:RegisterEvent("MERCHANT_SHOW")
events:RegisterEvent("MERCHANT_CLOSED")
events:SetScript("OnEvent", function(_, event)
    merchantGUID = event == "MERCHANT_SHOW" and Read(UnitGUID, "npc") or nil
end)

local function Merchant(npc)
    return npc and Shown(MerchantFrame) and merchantGUID
        and merchantGUID == CreatureGUID("target", npc.id) and merchantGUID == Read(UnitGUID, "npc")
end

local function VendorGossip(npc)
    local guid = Shown(GossipFrame) and CreatureGUID("npc", npc.id)
    return guid and guid == CreatureGUID("target", npc.id)
end

local function VendorOption()
    local options = C_GossipInfo and Read(C_GossipInfo.GetOptions)
    if type(options) ~= "table" or canaccesstable and not canaccesstable(options) then return end
    local found
    for _, option in ipairs(options) do
        if type(option) == "table" and (not canaccesstable or canaccesstable(option))
            and Public(option.icon) == 132060 and type(Public(option.gossipOptionID)) == "number"
            and (not (Enum and Enum.GossipOptionStatus)
                or Public(option.status) == Enum.GossipOptionStatus.Available) then
            if found then return end
            found = option.gossipOptionID
        end
    end
    return found
end

local function NearestRepairer(state, map)
    if Read(UnitFactionGroup, "player") ~= "Alliance" or not C_Map then return end
    local pos = Read(C_Map.GetPlayerMapPosition, map, "player")
    local x, y
    if pos then x, y = Read(pos.GetXY, pos) end
    local width, height = Read(C_Map.GetMapWorldSize, map)
    if type(x) ~= "number" or type(y) ~= "number" or type(width) ~= "number" or type(height) ~= "number"
        or width <= 0 or height <= 0 then return end
    local best, distance
    for _, npc in ipairs(repairers) do
        if npc.map == map and (state.repairFailed[npc.id] or 0) <= GetTime() then
            local d = ((npc.x - x) * width)^2 + ((npc.y - y) * height)^2
            if not distance or d < distance then best, distance = npc, d end
        end
    end
    return best
end

local function Report(state, code, value, detail)
    state.receipt = {code = code, value = value, untilTime = GetTime() + 1.4}
    state.pending = nil
    state.nextScan = nil
    print("WowAgent 无限服装备", code, value, detail or "")
    return state.receipt
end

local function EndRepair(state, npc, code, value)
    if Merchant(npc) then Read(CloseMerchant) end
    if VendorGossip(npc) and C_GossipInfo then Read(C_GossipInfo.CloseGossip) end
    if code == step.repairFailed then
        state.repairFailed[npc.id] = GetTime() + 240
        state.repairAttempts = (state.repairAttempts or 0) + 1
        if state.repairAttempts >= 2 then
            state.repairAttempts = 0
            state.repairRetryAt = GetTime() + 120
        end
    else
        state.repairAttempts = 0
    end
    state.visit, state.openingUntil = nil, nil
    if code then return Report(state, code, value) end
end

local function RepairState(state, damaged)
    local npc = state.visit
    if #damaged == 0 then
        if Merchant(npc) or VendorGossip(npc) then
            return {code = step.repairClose, value = npc.id, npc = npc}
        end
        state.visit, state.openingUntil = nil, nil
        return
    end
    if Merchant(npc) then
        local cost, canRepair = Read(GetRepairAllCost)
        local money = Read(GetMoney)
        if Read(CanMerchantRepair) == true and canRepair == true
            and type(cost) == "number" and cost > 0 and type(money) == "number" then
            return {code = money >= cost and step.repair or step.repairCannotPay,
                value = cost, npc = npc, damaged = damaged}
        end
    elseif VendorGossip(npc) and not state.openingUntil then
        local option = VendorOption()
        if option then return {code = step.repairGossip, value = npc.id, npc = npc, option = option} end
    elseif not state.openingUntil and not Shown(GossipFrame) and not Shown(MerchantFrame) then
        return {code = step.repairTravel, value = npc.id, npc = npc}
    end
    state.openingUntil = state.openingUntil or GetTime() + 6
    if GetTime() < state.openingUntil then return {code = step.repairWait, value = npc.id, npc = npc} end
    return {code = step.repairClose, value = npc.id, npc = npc, failed = true}
end

function gear.State(plan, map, refresh)
    if not Safe() then return end
    local state = plan.foreverGear
    if not state then
        state = {failed = {}, repairFailed = {}}
        plan.foreverGear = state
    end
    local now = GetTime()
    if state.receipt and now < state.receipt.untilTime then return state.receipt end
    local pending = state.pending
    if pending then
        if pending.npc then
            local confirmed = true
            for _, item in ipairs(pending.damaged) do
                local current, maximum = Read(GetInventoryItemDurability, item.slot)
                if not item.id or Read(GetInventoryItemID, "player", item.slot) ~= item.id
                    or type(current) ~= "number" or type(maximum) ~= "number" or maximum <= 0
                    or current ~= maximum then confirmed = false; break end
            end
            if confirmed then return EndRepair(state, pending.npc, step.repaired, pending.value) end
            if now >= pending.deadline then return EndRepair(state, pending.npc, step.repairFailed, pending.npc.id) end
            return {code = step.repairWait, value = pending.npc.id, npc = pending.npc}
        end
        if ItemKey(Read(GetInventoryItemLink, "player", pending.value)) == pending.key then
            return Report(state, step.equipped, pending.value, pending.link)
        end
        if now >= pending.deadline then
            state.failed[pending.key] = now + 240
            return Report(state, step.equipFailed, pending.value, pending.link)
        end
        return {code = step.equipWait, value = pending.value}
    end
    if Read(StaticPopup_IsAnyDialogShown) == true then return end
    if state.visit and map ~= state.visit.map then state.visit, state.openingUntil = nil, nil end
    if state.visit then return RepairState(state, Durability()) end
    if Shown(MerchantFrame) or Shown(GossipFrame) then return end
    if refresh or not state.nextScan or now >= state.nextScan then
        state.nextScan = now + .75
        state.upgrade = Upgrade(state)
    end
    if state.upgrade then return state.upgrade end

    local damaged = Durability()
    local money = Read(GetMoney)
    if #damaged == 0 then state.cost = nil; return end
    if state.cost and (type(money) ~= "number" or money < state.cost) then return end
    if state.repairRetryAt and now < state.repairRetryAt then return end
    if type(RepairAllItems) ~= "function" or type(GetRepairAllCost) ~= "function" then return end
    state.visit = NearestRepairer(state, map)
    if state.visit then return RepairState(state, damaged) end
end

function gear.Run(plan, map, offered)
    local action = gear.State(plan, map, true)
    if not action or action.code ~= offered.code or action.value ~= offered.value
        or action.key ~= offered.key or action.npc ~= offered.npc or action.option ~= offered.option
        or (Read(GetUnitSpeed, "player") or 0) > 0 then return end
    local state, npc = plan.foreverGear, action.npc
    if action.code == step.equip then
        state.pending = action
        action.deadline = GetTime() + 4
        if not pcall(C_Item.EquipItemByName or EquipItemByName, action.link, action.value) then
            action.deadline = GetTime()
        end
    elseif action.code == step.repairGossip and VendorGossip(npc) then
        state.openingUntil = GetTime() + 6
        if not pcall(C_GossipInfo.SelectOption, action.option) then state.openingUntil = GetTime() end
    elseif action.code == step.repair and Merchant(npc) then
        state.pending = action
        action.deadline = GetTime() + 5
        if not pcall(RepairAllItems) then action.deadline = GetTime() end
    elseif action.code == step.repairCannotPay and Merchant(npc) then
        state.cost = action.value
        EndRepair(state, npc, step.repairFunds, action.value)
    elseif action.code == step.repairClose or action.code == step.repairTravel then
        EndRepair(state, npc, (action.failed or action.code == step.repairTravel)
            and step.repairFailed or nil, npc.id)
    end
end
