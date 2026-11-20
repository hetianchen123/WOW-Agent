-- Report a cleared native loot window tied to a previously observed creature.
local core = {serial = 0, epoch = 0}
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
    if epoch ~= nil and epoch ~= false and remaining > 0 then
        return {epoch = epoch, slots = slots, remaining = remaining}
    end
end

function core.Register(frame)
    core.write = WowAgent.NewPixelRow(frame, -448)
    for _, event in ipairs({"LOOT_READY", "LOOT_OPENED", "LOOT_CLOSED"}) do frame:RegisterEvent(event) end
end

function core.Event(event, slot, fromItem)
    if event == "LOOT_READY" or event == "LOOT_OPENED" then
        if event == "LOOT_OPENED" and (secret(fromItem) or fromItem == true) then
            core.batch = nil
        elseif not core.batch then
            core.batch = SnapshotLoot()
        end
    elseif event == "LOOT_SLOT_CLEARED" and core.batch and not secret(slot) and core.batch.slots[slot] then
        core.batch.slots[slot] = nil
        core.batch.remaining = core.batch.remaining - 1
    elseif event == "LOOT_CLOSED" then
        if core.batch and core.batch.remaining == 0 then
            core.serial = core.serial % 63 + 1
            core.epoch = core.batch.epoch
        end
        core.batch = nil
    elseif event == "PLAYER_ENTERING_WORLD" or event == "PLAYER_DEAD" then
        core.batch = nil
    end
end

function core.Publish(sequence, nearbyCorpse)
    core.write(6, sequence * 8192 + (nearbyCorpse and 4096 or 0) + core.serial * 64 + core.epoch)
end
