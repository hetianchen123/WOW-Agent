-- WowAgent.State: game-state providers feeding the pixel channel.
-- Message ids and payload packing mirror bot/perception/channel_spec.py.

WowAgent = WowAgent or {}

local MSG_POS, MSG_HDG, MSG_STATUS, MSG_BAG = 0x0, 0x1, 0x2, 0x3
local MSG_GOLD, MSG_ERR, MSG_CAST, MSG_ZONE = 0x4, 0x5, 0x6, 0x7
local MSG_BAGSUM, MSG_FASTEV = 0x8, 0x9

local EVK_CAST_START, EVK_CAST_DONE, EVK_UIERR, EVK_AHPOST = 1, 2, 3, 4
local IsSecretValue = issecretvalue or function() return false end

------------------------------------------------------------------ config --
-- tracked item whitelist (index 1..16). Fill with your farmed fish/herbs/ore
-- item IDs; counts are reported in the slow rotation (BAG messages).
WowAgent.TRACKED_ITEMS = WowAgent.TRACKED_ITEMS or {
    -- [itemID] = slotIndex (1..16), e.g.:
    -- [173032] = 1, -- 浮标鱼
}

-- tracked spells: spellID -> small payload id used by FASTEV.
-- fishing = 1, herbalism = 2, mining = 3. Run `/wagent learn` while you
-- manually cast/gather once to discover the exact spellIDs your client uses,
-- then list them here (SavedVariables WowAgentDB.spells overrides defaults).
WowAgent.TRACKED_SPELLS = WowAgent.TRACKED_SPELLS or {
    [131296] = 1, -- Fishing (retail cast)
    [7620]   = 1, -- Fishing (classic)
    -- herbalism / mining: fill after `/wagent learn`, e.g.
    -- [2366] = 2,
    -- [2575] = 3,
}
if WowAgentDB and WowAgentDB.spells then
    for id, small in pairs(WowAgentDB.spells) do WowAgent.TRACKED_SPELLS[id] = small end
end

------------------------------------------------------------------ state ----
local bestMapID = nil
local lootOpen = false
local bagDirty = true
local trackedCounts = {}   -- slotIndex -> total quantity
local trackedTotal = 0

local function RefreshMapID()
    bestMapID = C_Map and C_Map.GetBestMapForUnit and C_Map.GetBestMapForUnit("player") or nil
end

local function MarkBagDirty() bagDirty = true end

local function CountTracked()
    trackedCounts, trackedTotal = {}, 0
    local itemToSlot = WowAgent.TRACKED_ITEMS
    if not next(itemToSlot) then return end
    for bag = -1, 4 do
        local slots = C_Container.GetContainerNumSlots(bag)
        if slots then
            for slot = 1, slots do
                local info = C_Container.GetContainerItemInfo(bag, slot)
                if info and info.itemID and itemToSlot[info.itemID] then
                    local idx = itemToSlot[info.itemID]
                    trackedCounts[idx] = (trackedCounts[idx] or 0) + (info.stackCount or 1)
                end
            end
        end
    end
    for _, c in pairs(trackedCounts) do trackedTotal = trackedTotal + c end
    bagDirty = false
end

local function OnEvent(self, event, arg1, _, spellID)
    if event == "PLAYER_ENTERING_WORLD" or event == "ZONE_CHANGED_NEW_AREA" then
        RefreshMapID()
    elseif event == "LOOT_OPENED" then
        lootOpen = true
    elseif event == "LOOT_CLOSED" then
        lootOpen = false
    elseif event == "BAG_UPDATE" then
        MarkBagDirty()
    elseif event == "UI_ERROR_MESSAGE" then
        -- arg1 = game-err numeric id, arg2 = message text
        WowAgent.seq = (WowAgent.seq + 1) % 32
        table.insert(WowAgent.events, { kind = EVK_UIERR, seq = WowAgent.seq, payload = math.min(arg1 or 0, 4095) })
    elseif event == "UNIT_SPELLCAST_START" and arg1 == "player" then
        local small = not IsSecretValue(spellID) and WowAgent.TRACKED_SPELLS[spellID]
        if small then
            WowAgent.seq = (WowAgent.seq + 1) % 32
            table.insert(WowAgent.events, { kind = EVK_CAST_START, seq = WowAgent.seq, payload = small })
        end
    elseif event == "UNIT_SPELLCAST_SUCCEEDED" and arg1 == "player" then
        local small = not IsSecretValue(spellID) and WowAgent.TRACKED_SPELLS[spellID]
        if small then
            WowAgent.seq = (WowAgent.seq + 1) % 32
            table.insert(WowAgent.events, { kind = EVK_CAST_DONE, seq = WowAgent.seq, payload = small })
        end
    end
    if #WowAgent.events > 8 then table.remove(WowAgent.events, 1) end
end

local sink = CreateFrame("Frame", "WowAgentEventFrame")
sink:RegisterEvent("PLAYER_ENTERING_WORLD")
sink:RegisterEvent("ZONE_CHANGED_NEW_AREA")
sink:RegisterEvent("LOOT_OPENED")
sink:RegisterEvent("LOOT_CLOSED")
sink:RegisterEvent("BAG_UPDATE")
sink:RegisterEvent("UI_ERROR_MESSAGE")
sink:RegisterEvent("UNIT_SPELLCAST_START")
sink:RegisterEvent("UNIT_SPELLCAST_SUCCEEDED")
sink:SetScript("OnEvent", OnEvent)

-- observe-only hook on auction posting (fired by Auctionator's Post button)
if C_AuctionHouse and C_AuctionHouse.PostItem then
    hooksecurefunc(C_AuctionHouse, "PostItem", function(item, duration, buyout, quantity)
        pcall(function()
            WowAgent.seq = (WowAgent.seq + 1) % 32
            table.insert(WowAgent.events, { kind = EVK_AHPOST, seq = WowAgent.seq, payload = math.min(quantity or 0, 4095) })
        end)
    end)
end

---------------------------------------------------------------- providers --
function WowAgent.BuildPos()
    if not (bestMapID and C_Map.GetPlayerMapPosition) then return nil, 0 end
    local pos = C_Map.GetPlayerMapPosition(bestMapID, "player")
    if not pos then return nil, 0 end
    local x, y = pos:GetXY()
    if not x or not y then return nil, 0 end
    local qx = math.floor(math.min(math.max(x, 0), 1) * 1023 + 0.5)
    local qy = math.floor(math.min(math.max(y, 0), 1) * 1023 + 0.5)
    return MSG_POS, qx * 1024 + qy
end

function WowAgent.BuildHdg()
    local f = GetPlayerFacing and GetPlayerFacing()
    if not f then return nil, 0 end
    local q = math.floor((f % (2 * math.pi)) / (2 * math.pi) * 1048575 + 0.5)
    return MSG_HDG, q
end

WowAgent.heartbeat = 0

function WowAgent.BuildStatus()
    local flags = 0
    if UnitAffectingCombat("player") then flags = flags + 1 end            -- bit0
    if UnitIsDead("player") then flags = flags + 2 end                     -- bit1
    if UnitIsGhost("player") then flags = flags + 4 end                    -- bit2
    if IsMounted() then flags = flags + 8 end                              -- bit3
    if IsFlying() then flags = flags + 16 end                              -- bit4
    if IsSwimming() then flags = flags + 32 end                            -- bit5
    if IsFalling() then flags = flags + 64 end                             -- bit6
    local castName = UnitCastingInfo("player")
    if not IsSecretValue(castName) and castName then flags = flags + 128 end -- bit7
    if lootOpen then flags = flags + 256 end                               -- bit8
    if UnitExists("target") then flags = flags + 512 end                   -- bit9
    local speed = GetUnitSpeed("player")
    if not IsSecretValue(speed) and speed and speed > 0.5 then flags = flags + 1024 end -- bit10
    WowAgent.heartbeat = 1 - WowAgent.heartbeat
    if WowAgent.heartbeat == 1 then flags = flags + 2048 end               -- bit11

    local hp = 0
    local currentHealth = UnitHealth("player")
    local hpm = UnitHealthMax("player")
    if IsSecretValue(currentHealth) or IsSecretValue(hpm) or not currentHealth or not hpm or hpm <= 0 then
        flags = flags + 4096                                               -- bit12: HP unavailable
    else
        hp = math.floor(currentHealth / hpm * 15 + 0.5)
        if hp < 0 then hp = 0 elseif hp > 15 then hp = 15 end
    end
    return MSG_STATUS, flags * 16 + hp
end

function WowAgent.BuildBagSum()
    local free = 0
    for bag = 0, 4 do
        local f = C_Container.GetContainerNumFreeSlots(bag)
        if f then free = free + f end
    end
    if free > 255 then free = 255 end
    if bagDirty then CountTracked() end
    local total = trackedTotal
    if total > 4095 then total = 4095 end
    return MSG_BAGSUM, free * 4096 + total
end

function WowAgent.BuildFastEv()
    local e = table.remove(WowAgent.events, 1)
    if not e then
        return MSG_FASTEV, 0 -- kind NONE
    end
    return MSG_FASTEV, e.kind * 131072 + e.seq * 4096 + e.payload
end

-- slow rotation: idx 0 ZONE, 1 GOLD, 2..17 BAG slot 1..16
function WowAgent.BuildSlowRot(idx)
    if idx == 0 then
        return MSG_ZONE, math.min(bestMapID or 0, 1048575)
    elseif idx == 1 then
        return MSG_GOLD, math.min(math.floor(GetMoney() / 10000), 1048575)
    else
        local slot = idx - 1 -- 1..16
        if bagDirty then CountTracked() end
        local count = math.min(trackedCounts[slot] or 0, 65535)
        return MSG_BAG, slot * 65536 + count
    end
end
