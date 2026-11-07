-- Explicit, reversible replacement of the current standard action bars.
local support = WowAgent.LayoutSupport
local print = WowAgent.Report or print
local pending
local bars = {
    {"", "ActionButton"}, {"bl.", "MultiBarBottomLeftButton"},
    {"br.", "MultiBarBottomRightButton"}, {"r.", "MultiBarRightButton"},
    {"l.", "MultiBarLeftButton"}, {"b5.", "MultiBar5Button"},
    {"b6.", "MultiBar6Button"}, {"b7.", "MultiBar7Button"},
}

local function Say(message)
    print("WowAgent：" .. message)
end

local function Safe()
    return support.NormalBar() and GetCursorInfo and GetCursorInfo() == nil
end

local function Slots(scope)
    local slots, seen = {}, {}
    for bar, definition in ipairs(bars) do
        if scope == "main" and bar > 1 then break end
        for index = 1, 12 do
            local button = _G[definition[2] .. index]
            if button and (bar == 1 or button:IsVisible()) then
                local slot = support.Public(button.action)
                if type(slot) ~= "number" or slot < 1 or seen[slot] then return nil end
                seen[slot] = true
                slots[#slots + 1] = {label = definition[1] .. index, slot = slot}
            elseif bar == 1 then
                return nil
            end
        end
    end
    return slots
end

function WowAgent.GetActionLayoutLabels(scope)
    if scope ~= "main" and scope ~= "visible" then return {} end
    local labels = {}
    for _, button in ipairs(Slots(scope) or {}) do labels[#labels + 1] = button.label end
    return labels
end

local function Has(slot)
    local read = (C_ActionBar and C_ActionBar.HasAction) or HasAction
    return read and support.Public(read(slot))
end

local function Macro(id)
    local ok, name, _, body = pcall(GetMacroInfo, id)
    if ok and name and body then return {kind = "macro", id = id, name = name, body = body} end
end

local function Snapshot(slots)
    local actions = {}
    for _, button in ipairs(slots) do
        local present = Has(button.slot)
        local kind, id = support.ReadAction(button.slot)
        if present == false then
            actions[button.slot] = false
        elseif present and type(id) == "number" and id > 0 then
            if kind == "macro" then
                actions[button.slot] = Macro(id)
            elseif kind == "spell" or kind == "item" or kind == "flyout" then
                actions[button.slot] = {kind = kind, id = id}
            end
        end
        if actions[button.slot] == nil then
            return nil, "格子 " .. button.label .. " 的动作无法可靠备份（" .. tostring(kind) .. "）。"
        end
    end
    return actions
end

local function Same(a, b)
    if a == false or b == false then return a == b end
    if a.kind ~= b.kind then return false end
    if a.kind == "macro" then return a.name == b.name and a.body == b.body end
    return a.id == b.id
end

local function SpellBookPickup(kind, id)
    local api = C_SpellBook
    if not api or not api.PickupSpellBookItem or not api.GetNumSpellBookSkillLines then return end
    local bank = Enum.SpellBookSpellBank.Player
    local itemType = kind == "flyout" and Enum.SpellBookItemType.Flyout or Enum.SpellBookItemType.Spell
    for i = 1, api.GetNumSpellBookSkillLines() do
        local line = api.GetSpellBookSkillLineInfo(i)
        if line and (not line.offSpecID or line.offSpecID == 0) then
            for slot = line.itemIndexOffset + 1, line.itemIndexOffset + line.numSpellBookItems do
                local info = api.GetSpellBookItemInfo(slot, bank)
                if info and info.itemType == itemType and not info.isOffSpec
                    and (info.actionID == id or (kind == "spell" and info.spellID == id)) then
                    return api.PickupSpellBookItem, slot, bank
                end
            end
        end
    end
end

local function Pickup(action)
    local pick, id, bank = nil, action.id, nil
    if action.kind == "macro" then
        local found = Macro(id)
        if not found or not Same(found, action) then
            id = GetMacroIndexByName(action.name)
            found = id and id > 0 and Macro(id)
            if not found or not Same(found, action) then return false end
        end
        pick = PickupMacro
    elseif action.kind == "spell" then
        pick = C_Spell and C_Spell.PickupSpell or PickupSpell
        if not pick then pick, id, bank = SpellBookPickup("spell", id) end
    elseif action.kind == "item" then
        pick = C_Item and C_Item.PickupItem or PickupItem
    elseif action.kind == "flyout" then
        pick, id, bank = SpellBookPickup("flyout", id)
    end
    if not pick or not Safe() then return false end
    if bank then pcall(pick, id, bank) else pcall(pick, id) end
    local kind, value, _, spell = GetCursorInfo()
    kind, value, spell = support.Public(kind), support.Public(value), support.Public(spell)
    if kind ~= action.kind then return false end
    if kind == "macro" or kind == "item" then return value == id end
    if kind == "spell" and type(spell) == "number" then return spell == action.id end
    return true
end

local function Preflight(actions)
    for _, action in pairs(actions) do
        if action then
            if not Safe() then return false end
            local picked = Pickup(action)
            -- Pickup starts with an empty cursor and only tries this known action.
            if GetCursorInfo() ~= nil then ClearCursor() end
            if not picked then return false end
        end
    end
    return true
end

local function ReplaceSlots(slots, target)
    local current, errorMessage = Snapshot(slots)
    if not current then return false, errorMessage end
    local place = C_ActionBar and C_ActionBar.PutActionInSlot or PlaceAction
    for _, button in ipairs(slots) do
        local slot = button.slot
        if not Same(current[slot], target[slot]) then
            if not Safe() then return false, "状态变化，中止动作栏更新。" end
            if current[slot] then
                pcall(PickupAction, slot)
                if GetCursorInfo() ~= nil then ClearCursor() end
                if Has(slot) ~= false then return false, "客户端拒绝清空格子 " .. button.label .. "。" end
            end
            if target[slot] then
                local picked = Pickup(target[slot])
                if picked and Has(slot) == false and support.NormalBar() then pcall(place, slot) end
                if GetCursorInfo() ~= nil then ClearCursor() end
                local confirmed = Snapshot({button})
                if not picked or not confirmed or not Same(confirmed[slot], target[slot]) then
                    return false, "客户端拒绝放置格子 " .. button.label .. "。"
                end
            end
        end
    end
    return true
end

local function Replace(slots, target)
    local ok, changed, reason = pcall(ReplaceSlots, slots, target)
    if not ok then return false, "客户端拒绝更新动作栏。" end
    return changed, reason
end

local function Selected(spec)
    local api = C_ClassTalents
    return api and api.GetLastSelectedSavedConfigID and api.GetLastSelectedSavedConfigID(spec)
end

local function OnLoadout(spec, id, nextStep, failed)
    if not Safe() or support.Identity().spec ~= spec then return failed() end
    if not id or Selected(spec) == id then return nextStep() end
    if not C_Timer or not C_Timer.After or not support.SwitchLoadout(id) then return failed() end
    local request, deadline, stable = pending, GetTime() + 8, false
    local function Check()
        if pending ~= request then return end
        if not Safe() or support.Identity().spec ~= spec or GetTime() >= deadline then return failed() end
        local selected = Selected(spec) == id
        if selected and stable then return nextStep() end
        stable = selected
        C_Timer.After(.2, Check)
    end
    C_Timer.After(.2, Check)
end

local function Desired(plan, slots)
    local actions, byLabel = {}, {}
    for _, button in ipairs(slots) do
        byLabel[button.label], actions[button.slot] = button.slot, false
    end
    local learned
    for _, entry in ipairs(plan.entries) do
        local slot = byLabel[entry.label]
        if not slot then return nil, "格子 " .. entry.label .. " 不在本次范围内。" end
        if entry.kind == "spell" then
            learned = learned or support.LearnedSpells()
            if not learned[entry.id] then return nil, "当前天赋未学会技能 " .. entry.id .. "。" end
            actions[slot] = {kind = "spell", id = entry.id}
        elseif entry.kind == "macro" then
            actions[slot] = Macro(entry.id)
        else
            local kind = entry.kind == "cast" and "spell" or entry.kind
            local name, body = support.MacroDefinition(kind, entry.id)
            local index = name and support.WriteMacro(name, body)
            actions[slot] = index and Macro(index)
        end
        if not actions[slot] then return nil, "无法准备格子 " .. entry.label .. " 的技能或宏。" end
    end
    return actions
end

local function SameSlots(a, b)
    if not a or #a ~= #b then return false end
    for i, button in ipairs(a) do
        if button.slot ~= b[i].slot or button.label ~= b[i].label then return false end
    end
    return true
end

local function FinishRestore(data, backup)
    OnLoadout(backup.spec, backup.source, function()
        data.backup, pending = nil, nil
        Say("已恢复原动作栏和天赋。")
    end, function()
        pending = nil
        Say("动作栏已处理，但原天赋尚未确认。保留备份，可以重试 restore。")
    end)
end

local function Restore(data, identity)
    local backup = data.backup
    if not backup or backup.spec ~= identity.spec or backup.build ~= identity.build then
        return Say("当前专精和客户端没有可恢复的备份。")
    end
    pending = {}
    local function restoreBar()
        if backup.actions then
            local slots = Slots(backup.scope)
            if not SameSlots(slots, backup.slots) or not Preflight(backup.actions) then
                pending = nil
                return Say("请恢复原动作栏页和可见栏范围，或补回原技能与宏，再重试 restore。")
            end
            local current, errorMessage = Snapshot(slots)
            if not current or not Preflight(current) then
                pending = nil
                return Say(errorMessage or "当前格子无法备份，暂未恢复。")
            end
            support.Disarm()
            local ok, reason = Replace(slots, backup.actions)
            if not ok then
                Replace(slots, current)
                pending = nil
                return Say(reason .. " 备份仍保留，可以重试 restore。")
            end
            backup.actions = nil
        end
        FinishRestore(data, backup)
    end
    OnLoadout(backup.spec, backup.actions and backup.target, restoreBar, function()
        pending = nil
        Say("无法切换到备份所属天赋，备份仍保留。")
    end)
end

local function Apply(data, identity, plan)
    plan = plan or data.plan
    if data.backup then return Say("先用 restore 恢复上一次布局，或用 accept 确认采用现布局。") end
    if not plan or plan.spec ~= identity.spec then return Say("请先用 plan 预览当前专精的布局。") end
    local source = Selected(identity.spec)
    local target = plan.loadout or source
    if target ~= source and not source then return Say("请先保存当前天赋，以便切换后可以恢复。") end
    local backup = {spec = identity.spec, build = identity.build, source = source, target = target, scope = plan.scope}
    data.backup, pending = backup, {}
    OnLoadout(identity.spec, target, function()
        local slots = Slots(plan.scope)
        local original, reason
        if slots then original, reason = Snapshot(slots) end
        if not original or not Preflight(original) then
            Say(reason or "当前动作栏含无法恢复的动作，已跳过清空。")
            return FinishRestore(data, backup)
        end
        local desired, errorMessage = Desired(plan, slots)
        if not desired or not Preflight(desired) then
            Say(errorMessage or "新动作无法全部放置，已跳过清空。")
            return FinishRestore(data, backup)
        end
        if not Safe() or not SameSlots(Slots(plan.scope), slots) then
            Say("动作栏状态发生变化，已跳过清空。")
            return FinishRestore(data, backup)
        end
        backup.slots, backup.actions = slots, original
        support.Disarm()
        local ok, failure = Replace(slots, desired)
        if not ok then
            Say(failure)
            if Replace(slots, original) then
                backup.actions = nil
                return FinishRestore(data, backup)
            end
            pending = nil
            return Say("保留了原布局备份，请用 restore 恢复。")
        end
        pending = nil
        backup.complete = true
        Say((#plan.entries == 0 and "当前主动作栏的 12 格已清空。" or "布局已按计划更新。")
            .. "可点「恢复备份」还原，或点「采用布局」确认采用。")
    end, function()
        pending = nil
        Say("客户端未确认切换天赋。用 restore 返回原天赋。")
    end)
end

local function Plan(data, identity, scope, target, tokens)
    if (scope ~= "main" and scope ~= "visible") or not target then
        return Say("用法：layout plan <main|visible> <keep|天赋ID> 1=assist 2=spell:<ID> …")
    end
    local loadout = tonumber(target)
    if target ~= "keep" then
        local found = false
        local api = C_ClassTalents
        for _, id in ipairs(api and api.GetConfigIDsBySpecID and api.GetConfigIDsBySpecID(identity.spec) or {}) do
            if loadout == id then found = true; break end
        end
        if not found then return Say("天赋 ID 必须属于当前专精的已保存配置。") end
    end
    local slots, allowed = Slots(scope), {}
    if not slots then return Say("当前动作栏无法识别。") end
    for _, button in ipairs(slots) do allowed[button.label] = true end
    local entries = {}
    local kinds = {spell=true, cast=true, macro=true, mount=true, fly=true, buff=true, supply=true}
    for _, token in ipairs(tokens) do
        local label, value = token:match("^([%w.]+)=([%w:]+)$")
        local kind, id
        if value == "assist" then kind = value
        elseif value then kind, id = value:match("^(%a+):(%d+)$"); id = tonumber(id) end
        if not label or not allowed[label] or not (kind == "assist" or
            (kinds[kind] and id and id >= 1 and id <= 2147483647)) then
            return Say("格子无效、重复或动作格式错误 " .. token)
        end
        allowed[label] = nil
        entries[#entries + 1] = {label = label, kind = kind, id = id}
    end
    if #entries == 0 then return Say("请至少安排一个格子。") end
    data.plan = {scope = scope, spec = identity.spec, loadout = loadout, entries = entries}
    Say(string.format("已预览 %d 个格子，摆放 %d 个动作，其余留空。目标天赋 %s。",
                     #slots, #entries, loadout or "保留当前"))
    for _, token in ipairs(tokens) do print("  " .. token) end
    Say("确认后输入 /wagent combat layout apply。")
    return true
end

function WowAgent.ActionLayoutCommand(args)
    if args == "cancel" then
        if pending then
            pending = nil
            return Say("已取消后续动作栏步骤，已有备份可用 restore 恢复。")
        end
        return Say("当前没有等待中的动作栏操作。")
    end
    if pending then return Say("动作栏操作正在进行，请等客户端确认。") end
    local guid = support.Public(UnitGUID("player"))
    if not guid or not Safe() or not PickupAction or not ClearCursor or not GetMacroInfo
        or not ((C_ActionBar and C_ActionBar.PutActionInSlot) or PlaceAction) then
        return Say("请脱战，回到普通动作栏并清空鼠标后再配置。")
    end
    local identity = support.Identity()
    if identity.spec <= 0 then return Say("无法确认当前专精。") end
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.actionLayouts = WowAgentDB.actionLayouts or {}
    local data = WowAgentDB.actionLayouts[guid] or {}
    WowAgentDB.actionLayouts[guid] = data
    local tokens = {}
    for token in args:gmatch("%S+") do tokens[#tokens + 1] = token end
    local command = table.remove(tokens, 1)
    if command == "plan" then
        local scope, target = table.remove(tokens, 1), table.remove(tokens, 1)
        return Plan(data, identity, scope, target, tokens)
    elseif command == "apply" and #tokens == 0 then
        Apply(data, identity)
    elseif command == "clear" and #tokens == 0 then
        Apply(data, identity, {scope = "main", spec = identity.spec, entries = {}})
    elseif command == "restore" and #tokens == 0 then
        Restore(data, identity)
    elseif command == "accept" and #tokens == 0 then
        if not data.backup or data.backup.spec ~= identity.spec or not data.backup.complete then
            return Say("当前专精没有可确认采用的完整布局。")
        end
        data.backup = nil
        Say("已确认采用当前布局。")
    else
        Say("layout plan <main|visible> <keep|天赋ID> <格子=动作 …> | apply | clear | restore | accept | cancel")
    end
end
