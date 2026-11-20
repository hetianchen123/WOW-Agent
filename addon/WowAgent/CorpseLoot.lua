-- Report a cleared native loot window tied to a previously observed creature.
local core = {serial = 0, epoch = 0, fullSerial = 0, skipped = {}}
WowAgent.CorpseLoot = core
local secret = issecretvalue or function() return false end

local function Read(api, ...)
    if not api then return end
    local ok, value = pcall(api, ...)
    if ok and not secret(value) then return value end
end

local function SnapshotLoot()
    local count = Read(GetNumLootItems)
    if type(count) ~= "number" or count < 1 or count > 64 or not GetLootSourceInfo
        or not WowAgent.CombatLootTargetEpoch then return end
    local selected = Read(UnitGUID, "target")
    local selectedEpoch, knownGuid, knownEpoch, multiple
    local slots, remaining = {}, 0
    local empty = Enum and Enum.LootSlotType and Enum.LootSlotType.None or 0
    for slot = 1, count do
        local kind = Read(GetLootSlotType, slot)
        if type(kind) ~= "number" then return end
        if kind ~= empty then
            slots[slot], remaining = true, remaining + 1
            local values = {pcall(GetLootSourceInfo, slot)}
            if values[1] then
                for i = 2, #values, 2 do
                    local guid = values[i]
                    if not secret(guid) and type(guid) == "string" then
                        local epoch = WowAgent.CombatLootTargetEpoch(guid)
                        if epoch ~= nil then
                            if guid == selected then selectedEpoch = epoch end
                            if knownGuid and knownGuid ~= guid then multiple = true end
                            knownGuid, knownEpoch = guid, epoch
                        end
                    end
                end
            end
        end
    end
    local epoch = selectedEpoch or (not multiple and knownEpoch)
    if remaining > 0 then
        return {epoch = epoch or nil, guid = epoch and (selectedEpoch and selected or knownGuid),
            slots = slots, remaining = remaining}
    end
end

function core.Register(frame)
    core.write = WowAgent.NewPixelRow(frame, -448)
    core.writeFull = WowAgent.NewPixelRow(frame, -480)
    for _, event in ipairs({"LOOT_READY", "LOOT_OPENED", "LOOT_CLOSED"}) do frame:RegisterEvent(event) end
end

local function CapacityError(code, message)
    return (not secret(code) and type(code) == "number" and (code == LE_GAME_ERR_INV_FULL
        or code == LE_GAME_ERR_BAG_FULL)) or (not secret(message) and type(message) == "string"
        and (message == ERR_INV_FULL or message == ERR_BAG_FULL))
end

function core.Skipped(guid)
    return core.skipped[guid] ~= nil and core.skipped[guid] > GetTime()
end

local function CloseFullBatch(batch)
    if not batch.auto or not batch.managed or not C_Timer or not CloseLoot then return end
    -- Let the native auto-loot pass finish other slots, including money and stacks that fit.
    batch.closeTicket = (batch.closeTicket or 0) + 1
    local ticket = batch.closeTicket
    C_Timer.After(.35, function()
        if core.batch == batch and batch.closeTicket == ticket and batch.remaining > 0
            and not Read(StaticPopup_IsAnyDialogShown) then Read(CloseLoot) end
    end)
end

function core.Event(event, slot, fromItem, managed)
    if event == "LOOT_READY" or event == "LOOT_OPENED" then
        if event == "LOOT_OPENED" and (secret(fromItem) or fromItem == true) then
            core.batch = nil
        elseif not core.batch then
            core.batch = SnapshotLoot()
        end
        if event == "LOOT_OPENED" and core.batch then
            core.batch.auto = not secret(slot) and slot == true
            core.batch.managed = managed
            if core.batch.full then CloseFullBatch(core.batch) end
        end
    elseif event == "UI_ERROR_MESSAGE" and core.batch and CapacityError(slot, fromItem) then
        core.batch.full = true
        CloseFullBatch(core.batch)
    elseif event == "LOOT_SLOT_CLEARED" and core.batch and not secret(slot) and core.batch.slots[slot] then
        core.batch.slots[slot] = nil
        core.batch.remaining = core.batch.remaining - 1
        if core.batch.full then CloseFullBatch(core.batch) end
    elseif event == "LOOT_CLOSED" then
        if core.batch and core.batch.remaining == 0 and core.batch.epoch ~= nil then
            core.serial = core.serial % 63 + 1
            core.epoch = core.batch.epoch
        elseif core.batch and core.batch.remaining > 0 and core.batch.full and core.batch.auto and core.batch.managed then
            local now = GetTime()
            for guid, expires in pairs(core.skipped) do
                if expires <= now then core.skipped[guid] = nil end
            end
            if core.batch.guid then core.skipped[core.batch.guid] = now + 90 end
            core.fullSerial = core.fullSerial % 63 + 1
            core.fullEpoch = core.batch.epoch
        end
        core.batch = nil
    elseif event == "PLAYER_ENTERING_WORLD" or event == "PLAYER_DEAD" then
        core.batch = nil
        core.skipped = {}
        core.fullSerial, core.fullEpoch = 0, nil
    end
end

function core.Publish(sequence, nearbyCorpse)
    core.write(6, sequence * 8192 + (nearbyCorpse and 4096 or 0) + core.serial * 64 + core.epoch)
    local epochBit = core.fullEpoch ~= nil and 4096 or 0
    core.writeFull(8, sequence * 8192 + epochBit + core.fullSerial * 64 + (core.fullEpoch or 0))
end
