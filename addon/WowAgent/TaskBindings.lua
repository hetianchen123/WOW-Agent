WowAgent = WowAgent or {}

local bindings = {}
WowAgent.TaskBindings = bindings
local secret = issecretvalue or function() return false end

local clicks = {
    {"quest_target", "K", "WowAgentTaskTarget", "LeftButton", nil,
        {TOGGLECHARACTER4 = true, TOGGLEPROFESSIONBOOK = true}},
    {"quest_dialogue", "9", "WowAgentTaskDialogue", "LeftButton", nil, {ACTIONBUTTON9 = true}},
    {"quest_item", "J", "WowAgentTaskItem", "LeftButton", nil,
        {TOGGLEGUILDTAB = true, TOGGLEGROUPFINDER = true}},
    {"pet_fetch", "L", "WowAgentTaskFetch", "LeftButton", nil, {TOGGLEQUESTLOG = true}},
    {"pet_fetch_next", "O", "WowAgentTaskSweep", "LeftButton", nil, {TOGGLESOCIAL = true}},
    {"quest_suspend", "F5", "WowAgentTaskSuspend", "LeftButton", nil, {TARGETPARTYMEMBER4 = true}},
    {"camera_restore", "F8", "WowAgentTaskCamera", "LeftButton", nil, {TOGGLEBAG1 = true}},
    {"camera_probe", "F6", "WowAgentTaskCamera", "RightButton"},
    {"camera_orbit", "F4", "WowAgentTaskCamera", "MiddleButton", "retail"},
    {"player_recovery", "U", "WowAgentPlayerRecovery", "LeftButton"},
    {"forever_progress", "F4", "WowAgentForeverProgress", "LeftButton", "forever",
        {TARGETPARTYMEMBER3 = true}},
    {"pet_mend", "F7", "WowAgentRecoveryMend", "LeftButton"},
    {"pet_follow", "F9", "WowAgentRecoveryFollow", "LeftButton", nil, {TOGGLEBAG2 = true}},
    {"pet_summon", "F10", "WowAgentRecoverySummon", "LeftButton", nil, {TOGGLEBAG3 = true}},
    {"pet_revive", "F11", "WowAgentRecoveryRevive", "LeftButton", nil, {TOGGLEBAG4 = true}},
    {"class_buff", "F12", "WowAgentRecoveryBuff", "LeftButton", nil, {TOGGLEBACKPACK = true}},
    {"potion", "F1", "WowAgentRecoveryPotion", "LeftButton", "forever", {TARGETSELF = true}},
    {"water", "F2", "WowAgentRecoveryWater", "LeftButton", "forever", {TARGETPARTYMEMBER1 = true}},
    {"food", "F3", "WowAgentRecoveryFood", "LeftButton", "forever", {TARGETPARTYMEMBER2 = true}},
    {"controller_prepare", "CTRL-ALT-SHIFT-F5", "WowAgentControllerPrepare", "LeftButton"},
}
for code = 0, 7 do
    clicks[#clicks + 1] = {"run_status_" .. code, "F" .. (13 + code),
        "WowAgentRunStatus" .. code, "LeftButton"}
end
local clickByID = {}
for _, spec in ipairs(clicks) do clickByID[spec[1]] = spec end
local cameraPool = {"U", "Y", "H", "G", "B", "N", "M", "I"}
local recoveryPool = {"U", "Y", "H", "G", "B", "N", "M", "F7"}
local alternatePools = {camera_restore = cameraPool, camera_probe = cameraPool, player_recovery = recoveryPool}
local recoveryCodes = {}
for index, key in ipairs(recoveryPool) do recoveryCodes[key] = index end
local cameraCodes = {F6 = 1, F8 = 2}
for index, key in ipairs(cameraPool) do cameraCodes[key] = index + 2 end
local assigned, generation, previous, dirty = {}, 0, nil, true
local alternateOwners = {}
local writeCameraKeys

bindings.movement = {
    {"W", "MOVEFORWARD", "前进"}, {"S", "MOVEBACKWARD", "后退"},
    {"LEFT", "TURNLEFT", "左转"}, {"RIGHT", "TURNRIGHT", "右转"},
    {"SPACE", "JUMP", "跳跃"},
}
bindings.native = {
    {"TAB", "TARGETNEARESTENEMY", "选敌"}, {"F", "INTERACTTARGET", "与目标互动"},
    {"1", "ACTIONBUTTON1", "正式服输出"}, {"5", "ACTIONBUTTON5", "五号动作"},
    {"6", "ACTIONBUTTON6", "飞行坐骑"}, {"7", "ACTIONBUTTON7", "地面坐骑"},
    {"8", "ACTIONBUTTON8", "下马"}, {"9", "ACTIONBUTTON9", "任务对话"},
    {"Q", "STRAFELEFT", "左横移"}, {"E", "STRAFERIGHT", "右横移"},
}
for _, row in ipairs(bindings.movement) do bindings.native[#bindings.native + 1] = row end

local function Applicable(spec)
    return not spec[5] or spec[5] == (WowAgent.Forever and "forever" or "retail")
end

function bindings.Read(key, overrides)
    if not GetBindingAction then return nil end
    local ok, action = pcall(GetBindingAction, key, overrides)
    if ok and not secret(action) and type(action) == "string" then return action end
end

function bindings.Reserved(key, exceptRole)
    if key == "P" or WowAgent.Forever and key == "4" then return true end
    for _, row in ipairs(bindings.native) do if row[1] == key then return true end end
    for _, spec in ipairs(clicks) do
        if spec[1] ~= exceptRole and Applicable(spec) and spec[2] == key then return true end
    end
    for _, entry in pairs(assigned) do if entry.key == key then return true end end
    return false
end

local actionBars = { "MultiBarBottomLeftButton", "MultiBarBottomRightButton", "MultiBarRightButton",
    "MultiBarLeftButton", "MultiBar5Button", "MultiBar6Button", "MultiBar7Button" }

function bindings.ActionSlot(action)
    if not action then return nil end
    local main = action:match("^ACTIONBUTTON(%d+)$")
    local bar, button = action:match("^MULTIACTIONBAR(%d+)BUTTON(%d+)$")
    local prefix = bar and actionBars[tonumber(bar)]
    local frame = main and _G["ActionButton" .. main] or prefix and _G[prefix .. button]
    local slot = frame and frame.action
    if not secret(slot) and type(slot) == "number" and slot >= 1 then return slot end
end

function bindings.SlotEmpty(slot)
    local hasAction = C_ActionBar and C_ActionBar.HasAction or HasAction
    if slot and hasAction then
        local ok, occupied = pcall(hasAction, slot)
        return ok and not secret(occupied) and occupied == false
    end
    return false
end

local function ClickAction(spec)
    return "CLICK " .. spec[3] .. ":" .. spec[4]
end

local function SameAction(actual, expected)
    return actual and actual:lower() == expected:lower()
end

local function CanUseClick(spec, borrowEmptySlot, key)
    key = key or spec[2]
    local defaults = key == spec[2] and spec[6]
    local base, effective = bindings.Read(key, false), bindings.Read(key, true)
    local expected = ClickAction(spec)
    local function Available(action)
        return action ~= nil and (action == "" or SameAction(action, expected)
            or defaults and defaults[action] or key == spec[2] and borrowEmptySlot
            and bindings.SlotEmpty(bindings.ActionSlot(action)))
    end
    return Applicable(spec) and Available(base) and Available(effective)
end

function bindings.ClickKey(id)
    local spec = clickByID[id]
    local entry = assigned[id]
    local key = entry and entry.key or spec[2]
    if CanUseClick(spec, true, key) and SameAction(bindings.Read(key, true), ClickAction(spec)) then return key end
end

function bindings.ClickReady(id)
    return bindings.ClickKey(id) ~= nil
end

function bindings.ClickAvailable(id, borrowEmptySlot)
    return CanUseClick(clickByID[id], borrowEmptySlot)
end

function bindings.BindClick(owner, id, borrowEmptySlot, allowAlternate)
    local spec = clickByID[id]
    if not SetOverrideBindingClick or InCombatLockdown() or not Applicable(spec) then return false end
    local existing = assigned[id]
    local key
    if CanUseClick(spec, borrowEmptySlot) then
        key = spec[2]
    elseif allowAlternate and alternatePools[id] then
        if existing and existing.owner == owner and bindings.ClickKey(id) == existing.key then return true end
        for _, candidate in ipairs(alternatePools[id]) do
            -- Player recovery prepares after Mend, so F7 can be used only
            -- when that role did not claim it and both binding layers are empty.
            local exceptRole = id == "player_recovery" and candidate == "F7" and "pet_mend" or nil
            if not bindings.Reserved(candidate, exceptRole) and bindings.Read(candidate, false) == ""
                and bindings.Read(candidate, true) == "" then
                key = candidate
                break
            end
        end
    end
    if not key then return false end
    local actionOwner = owner
    if allowAlternate then
        if not CreateFrame or not ClearOverrideBindings then return false end
        if existing and existing.owner == owner and bindings.ClickKey(id) == key then return true end
        if not alternateOwners[id] then alternateOwners[id] = CreateFrame("Frame") end
        actionOwner = alternateOwners[id]
        if existing then
            ClearOverrideBindings(actionOwner)
            assigned[id] = nil
            dirty = true
        end
    end
    local ok = pcall(SetOverrideBindingClick, actionOwner, false, key, spec[3], spec[4])
    if not ok or not SameAction(bindings.Read(key, true), ClickAction(spec)) then
        if actionOwner ~= owner then ClearOverrideBindings(actionOwner) end
        return false
    end
    assigned[id] = {key = key, owner = owner, actionOwner = actionOwner}
    dirty = true
    return bindings.ClickReady(id)
end

function bindings.RecoveryCode()
    return recoveryCodes[bindings.ClickKey("player_recovery")] or 0
end

function bindings.Forget(owner)
    for id, entry in pairs(assigned) do
        if entry.owner == owner then
            if entry.actionOwner ~= owner then ClearOverrideBindings(entry.actionOwner) end
            assigned[id] = nil
        end
    end
    dirty = true
end

function bindings.Invalidate() dirty = true end

function bindings.RegisterCameraWire(frame)
    writeCameraKeys = WowAgent.NewPixelRow(frame, -432)
end

function bindings.PublishCameraKeys(sequence, quest, phase, map, mounted, sky)
    if not InCombatLockdown() then
        for _, id in ipairs({"camera_restore", "camera_probe"}) do
            local entry = assigned[id]
            if entry and entry.actionOwner ~= entry.owner and not bindings.ClickKey(id) then
                ClearOverrideBindings(entry.actionOwner)
                assigned[id] = nil
                dirty = true
            end
        end
    end
    local restore = cameraCodes[bindings.ClickKey("camera_restore")] or 0
    local probe = cameraCodes[bindings.ClickKey("camera_probe")] or 0
    local context = table.concat({quest, phase, map, mounted and 1 or 0, sky and 1 or 0, restore, probe}, ":")
    if dirty or previous ~= context then
        previous, dirty = context, false
        generation = (generation + 1) % 32
    end
    writeCameraKeys(4, sequence * 8192 + generation * 256 + probe * 16 + restore)
end

function bindings.Protected(action)
    return action and (action == "SITORSTAND" or action:match("^PITCH") or action:match("^MOVE")
        or action:match("^TURN") or action:match("^STRAFE") or action:match("^VEHICLE")
        or action == "JUMP") and true or false
end

function bindings.NativeAvailable(key, expected)
    local base, effective = bindings.Read(key, false), bindings.Read(key, true)
    local turnKey = key == "Q" and "LEFT" or key == "E" and "RIGHT"
    local turn = turnKey and "TURN" .. turnKey
    local function Available(action)
        if action == nil then return false end
        if action == expected or not bindings.Protected(action) then return true end
        -- Borrow a duplicate turn only while its original arrow binding is retained.
        return turn and action == turn and bindings.Read(turnKey, false) == turn
            and bindings.Read(turnKey, true) == turn or false
    end
    return Available(base) and Available(effective)
end

function bindings.Report()
    local function Show(key, label, expected, allowed)
        local base, effective = bindings.Read(key, false), bindings.Read(key, true)
        local slot = bindings.ActionSlot(effective) or bindings.ActionSlot(base)
        local function Describe(action)
            if action == nil then return "未知" end
            return action == "" and "空闲" or action
        end
        local state = SameAction(effective, expected) and "已核对" or allowed and "可准备" or "检查冲突"
        local slotInfo = ""
        if slot then
            local read = WowAgent.LayoutSupport and WowAgent.LayoutSupport.ReadAction
            local kind, id
            if read then kind, id = read(slot) end
            slotInfo = " · 格 " .. slot .. " " .. (kind and (kind .. " " .. tostring(id))
                or bindings.SlotEmpty(slot) and "空" or "内容未知")
        end
        print("WowAgent " .. key .. " " .. label .. " · 原 " .. Describe(base)
            .. " · 生效 " .. Describe(effective) .. " · 期望 " .. expected .. slotInfo .. " · " .. state)
    end
    for _, row in ipairs(bindings.native) do
        if row[1] ~= "9" then Show(row[1], row[3], row[2], bindings.Read(row[1], true) == "") end
    end
    for _, spec in ipairs(clicks) do
        if Applicable(spec) then
            local key = assigned[spec[1]] and assigned[spec[1]].key or spec[2]
            Show(key, spec[1], ClickAction(spec), CanUseClick(spec, true, key))
        end
    end
    Show("P", "恢复上一个目标", "TARGETLASTTARGET", bindings.Read("P", true) == "")
end
