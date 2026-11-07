-- Read-only player telemetry, explicit out-of-combat macro/loadout commands.
local print = WowAgent.Report or print
local rows, armed, sequence, stateEpoch = {}, nil, 0, 0
local pendingSetup
local screen = CreateFrame("Frame", nil, UIParent)
local secret = issecretvalue or function() return false end

local function Public(value)
    if secret(value) then return nil end
    return value
end

local function Identity()
    local _, _, classID = UnitClass("player")
    local _, _, raceID = UnitRace("player")
    local version, build = GetBuildInfo()
    local specAPI = C_SpecializationInfo
    local getSpec = specAPI and specAPI.GetSpecialization or GetSpecialization
    local specInfo = specAPI and specAPI.GetSpecializationInfo or GetSpecializationInfo
    local index = getSpec and getSpec()
    local specID, specName
    if index and index > 0 and specInfo then specID, specName = specInfo(index) end
    local config = C_ClassTalents and C_ClassTalents.GetActiveConfigID and C_ClassTalents.GetActiveConfigID()
    if not config and GetActiveTalentGroup then config = GetActiveTalentGroup() end
    return {
        class = classID or 0, race = raceID or 0, spec = specID or 0, specName = specName,
        build = tonumber(build) or 0, version = version, project = WOW_PROJECT_ID or 0,
        config = config or 0, level = UnitLevel("player") or 0, locale = GetLocale(),
        treeIndex = GetPrimaryTalentTree and GetPrimaryTalentTree() or nil,
    }
end

local function SpellName(id)
    if C_Spell and C_Spell.GetSpellName then return Public(C_Spell.GetSpellName(id)) end
    if GetSpellInfo then return Public(GetSpellInfo(id)) end
end

local function Spellbook()
    local spells = {}
    local function Add(id, passive)
        if id and id > 0 and not passive then
            local name = SpellName(id)
            if name and name ~= "" then spells[id] = name end
        end
    end
    if C_SpellBook and C_SpellBook.GetNumSpellBookSkillLines then
        local bank = Enum.SpellBookSpellBank.Player
        for i = 1, C_SpellBook.GetNumSpellBookSkillLines() do
            local line = C_SpellBook.GetSpellBookSkillLineInfo(i)
            if line and (not line.offSpecID or line.offSpecID == 0) then
                for slot = line.itemIndexOffset + 1, line.itemIndexOffset + line.numSpellBookItems do
                    local info = C_SpellBook.GetSpellBookItemInfo(slot, bank)
                    if info and not info.isOffSpec and info.itemType == Enum.SpellBookItemType.Spell then
                        Add(info.spellID, info.isPassive)
                    end
                end
            end
        end
    elseif GetNumSpellTabs and GetSpellTabInfo and GetSpellBookItemInfo then
        for i = 1, GetNumSpellTabs() do
            local _, _, offset, count, _, offSpec = GetSpellTabInfo(i)
            if offset and count and (not offSpec or offSpec == 0) then
                for slot = offset + 1, offset + count do
                    local kind, id = GetSpellBookItemInfo(slot, BOOKTYPE_SPELL)
                    if kind == "SPELL" and IsPassiveSpell then Add(id, IsPassiveSpell(slot, BOOKTYPE_SPELL)) end
                end
            end
        end
    end
    return spells
end

local function TalentSnapshot(profile)
    local talents, loadouts = {}, {}
    if profile.config > 0 and C_Traits and C_ClassTalents and C_ClassTalents.GetActiveConfigID then
        local config = C_Traits.GetConfigInfo(profile.config)
        for _, tree in ipairs(config and config.treeIDs or {}) do
            for _, id in ipairs(C_Traits.GetTreeNodes(tree)) do
                local node = C_Traits.GetNodeInfo(profile.config, id)
                local active = node and node.activeEntry
                if active and node.activeRank > 0 then
                    local entry = C_Traits.GetEntryInfo(profile.config, active.entryID)
                    local definition = entry and C_Traits.GetDefinitionInfo(entry.definitionID)
                    talents[#talents + 1] = { node = id, entry = active.entryID,
                        rank = node.activeRank, spell = definition and definition.spellID }
                end
            end
        end
        if profile.spec > 0 then
            for _, id in ipairs(C_ClassTalents.GetConfigIDsBySpecID(profile.spec) or {}) do
                local info = C_Traits.GetConfigInfo(id)
                if info then loadouts[id] = info.name end
            end
        end
    elseif GetNumTalentTabs and GetNumTalents and GetTalentInfo then
        for tab = 1, GetNumTalentTabs() do
            for index = 1, GetNumTalents(tab) do
                local name, _, _, _, rank = GetTalentInfo(tab, index)
                if rank and rank > 0 then
                    talents[#talents + 1] = {tab = tab, index = index, name = name, rank = rank}
                end
            end
        end
    end
    profile.spells, profile.talents, profile.loadouts = Spellbook(), talents, loadouts
    if C_AssistedCombat and C_AssistedCombat.IsAvailable() then
        profile.assisted = C_AssistedCombat.GetRotationSpells()
    end
    return profile
end

local function SpellMacro(name, condition)
    if type(name) ~= "string" or name == "" or name:find("[%z\1-\31\127;%[%]]") then return nil end
    local body = "#showtooltip " .. name .. "\n/cast " .. condition .. " " .. name
    return #body <= 255 and body or nil
end

local function MacroBody(id)
    return SpellMacro(Spellbook()[id], "[harm,nodead]")
end

local function AssistMacro()
    local api = C_AssistedCombat
    if not api or not api.IsAvailable() then return nil end
    local id = api.GetActionSpell()
    if id then return SpellMacro(SpellName(id), "[harm,nodead,nochanneling]") end
end

local function MacroText(body)
    if type(body) ~= "string" then return nil end
    return (body:gsub("\r\n", "\n"):gsub("\r", "\n"):gsub("\n+$", ""))
end

local function MacroLimits()
    local constants = Constants and Constants.MacroConsts or {}
    return constants.MAX_ACCOUNT_MACROS or MAX_ACCOUNT_MACROS or 120,
           constants.MAX_CHARACTER_MACROS or MAX_CHARACTER_MACROS or 18
end

local function WriteMacro(name, body)
    if not body or #body > 255 then
        print("WowAgent：动作不可用或宏内容无效。")
        return
    end
    local accountLimit, characterLimit = MacroLimits()
    local expected = MacroText(body)
    local availableName
    for suffix = 0, accountLimit + characterLimit do
        local candidate = suffix == 0 and name or name .. suffix
        local index = GetMacroIndexByName(candidate)
        if index and index > 0 and MacroText(GetMacroBody(index)) == expected then
            print("WowAgent：宏已就绪 " .. candidate)
            return index
        elseif (not index or index < 1) and not availableName then
            availableName = candidate
        end
    end
    if not availableName then
        print("WowAgent：没有可用的宏名称，请整理宏列表后重试。")
        return
    end
    local perCharacter = true
    if GetNumMacros then
        local account, character = GetNumMacros()
        if character and character >= characterLimit then
            if not account or account >= accountLimit then
                print(string.format("WowAgent：宏位已满，角色 %d/%d，账号 %d/%d。请先在游戏宏面板整理。",
                    character, characterLimit, account or 0, accountLimit))
                return
            end
            perCharacter = nil
            print("WowAgent：角色宏位已满，本次使用账号宏位。")
        end
    end
    local ok, created = pcall(CreateMacro, availableName, 134400, body, perCharacter)
    if not ok then
        print("WowAgent：游戏拒绝创建宏 " .. availableName .. "，原因 " .. tostring(created))
        return
    end
    if type(created) ~= "number" or created < 1 then
        print("WowAgent：创建宏未返回有效编号 " .. availableName .. "，请查看游戏错误提示。")
        return
    end
    local actual = GetMacroBody(created)
    if MacroText(actual) ~= expected then
        print(string.format("WowAgent：宏已创建，但内容核对失败 %s，预期 %q，实际 %q。",
            availableName, body, tostring(actual)))
        return
    end
    print("WowAgent：宏已就绪 " .. availableName)
    return created
end

local function BoundSlot(key)
    local binding = Public(GetBindingAction(tostring(key), true))
    local button = binding and binding:match("^ACTIONBUTTON(%d+)$")
    button = button and _G["ActionButton" .. button]
    return button and Public(button.action)
end

local function MacroIndex(name)
    if not GetNumMacros or not GetMacroInfo then
        return GetMacroIndexByName and GetMacroIndexByName(name)
    end
    local accountCount, characterCount = GetNumMacros()
    local accountLimit = MacroLimits()
    local found, body
    for _, group in ipairs({{0, accountCount}, {accountLimit, characterCount}}) do
        for offset = 1, group[2] do
            local index = group[1] + offset
            local macroName, _, macroBody = GetMacroInfo(index)
            macroName, macroBody = Public(macroName), Public(macroBody)
            if macroName == name and macroBody then
                local normalized = MacroText(macroBody)
                if found and body ~= normalized then return nil end
                found, body = found or index, normalized
            end
        end
    end
    return found
end

local function ReadAction(slot)
    local readAction = C_ActionBar and C_ActionBar.GetActionInfo or GetActionInfo
    if not slot or not readAction then return end
    local kind, id, subtype = readAction(slot)
    kind, id = Public(kind), Public(id)
    if kind == "macro" then
        -- Cast/use macros can report a spell/item ID instead of their macro index.
        local text = C_ActionBar and C_ActionBar.GetActionText or GetActionText
        if text then
            local name = Public(text(slot))
            id = name and name ~= "" and MacroIndex(name) or nil
        elseif secret(subtype) or subtype ~= nil then
            id = nil
        end
    end
    return kind, id
end

local function ActionMatches(key, action, body)
    local kind, id = ReadAction(BoundSlot(key))
    if action == "assist" then
        if not C_AssistedCombat or not C_AssistedCombat.IsAvailable() then return false end
        if kind == "spell" then return id == C_AssistedCombat.GetActionSpell() end
    end
    return kind == "macro" and id and body and MacroText(Public(GetMacroBody(id))) == MacroText(body)
end

local function HostileTarget()
    local attackable = Public(UnitCanAttack("player", "target"))
    local dead = Public(UnitIsDead("target"))
    local guid = Public(UnitGUID("target"))
    if dead == nil or not guid or (not dead and attackable ~= true) then return 0, false end
    local kind, id = guid:match("^(%a+)%-%d+%-%d+%-%d+%-%d+%-(%d+)%-")
    if kind ~= "Creature" or not id then return 0, false end
    return tonumber(id), dead
end

local function Disarm()
    armed = nil
    stateEpoch = (stateEpoch + 1) % 64
end

local function UtilityMacro(kind, id)
    if kind == "mount" or kind == "fly" then
        local api = C_MountJournal
        if api and api.GetMountInfoByID then
            local name, spell, _, _, _, _, _, _, _, hidden, collected = api.GetMountInfoByID(id)
            if collected and not hidden and spell then
                local condition = kind == "fly"
                    and "[nocombat,nomounted,advflyable][nocombat,nomounted,flyable]"
                    or "[nocombat,nomounted]"
                return (kind == "fly" and "WAF" or "WAM") .. id,
                       SpellMacro(SpellName(spell) or name, condition)
            end
        end
        print("WowAgent：坐骑未收集或当前角色不可用。")
    elseif kind == "buff" then
        return "WAB" .. id, SpellMacro(Spellbook()[id], "[@player,nocombat]")
    elseif kind == "supply" then
        local count = C_Item and C_Item.GetItemCount or GetItemCount
        local itemSpell = C_Item and C_Item.GetItemSpell or GetItemSpell
        local available = count and Public(count(id, false, false))
        if not available or available < 1 or not itemSpell or not Public(itemSpell(id)) then
            print("WowAgent：背包中没有该可使用物品，或物品信息尚未载入。")
            return
        end
        return "WAI" .. id, "#showtooltip item:" .. id .. "\n/use [nocombat,nomounted] item:" .. id
    elseif kind == "quest" and WowAgent.QuestAction then
        return "WAQ" .. id, "/run WowAgent.QuestAction(" .. id .. ")"
    end
end

local function ListMounts()
    local api = C_MountJournal
    if not api or not api.GetMountIDs or not api.GetMountInfoByID then
        print("WowAgent：当前客户端没有坐骑收藏接口。")
        return
    end
    local shown = 0
    for _, id in ipairs(api.GetMountIDs()) do
        local name, _, _, _, _, _, favorite, _, _, hidden, collected = api.GetMountInfoByID(id)
        if favorite and collected and not hidden then
            print("WowAgent 坐骑", id, name)
            shown = shown + 1
        end
    end
    if shown == 0 then print("WowAgent：先在坐骑收藏里将测试坐骑设为收藏。") end
end

local function Gear(requested)
    local api = C_EquipmentSet
    if not api or not api.GetEquipmentSetIDs or not api.GetEquipmentSetInfo then
        print("WowAgent：当前客户端没有装备套装接口。")
        return
    end
    local ids = api.GetEquipmentSetIDs()
    if #ids == 0 then print("WowAgent：请先在装备管理器保存一个套装。"); return end
    for _, id in ipairs(ids) do
        if requested == nil or requested == id then
            local name, _, _, equipped, _, _, _, lost = api.GetEquipmentSetInfo(id)
            if name then
                print("WowAgent 套装", id, name, equipped and "已穿戴" or "未穿戴", "缺失", lost or "?")
                if requested == id then
                    if equipped then return end
                    if lost ~= 0 or not api.UseEquipmentSet
                        or (api.EquipmentSetContainsLockedItems and api.EquipmentSetContainsLockedItems(id))
                        or (api.CanUseEquipmentSets and not api.CanUseEquipmentSets()) then
                        print("WowAgent：当前无法完整换装。")
                        return
                    end
                    Disarm()
                    if api.UseEquipmentSet(id) then
                        print("WowAgent：已提交换装，使用 /wagent combat gear 查看结果。")
                    else
                        print("WowAgent：客户端拒绝了换装。")
                    end
                    return
                end
            end
        end
    end
    if requested ~= nil then print("WowAgent：找不到指定套装。") end
end

local function PrepStatus()
    local p = Identity()
    local api = C_ClassTalents
    print("WowAgent 当前专精", p.spec, p.specName or "?", "天赋配置", p.config)
    local specs = C_SpecializationInfo
    if specs and specs.GetNumSpecializations and specs.GetSpecializationInfo then
        for i = 1, specs.GetNumSpecializations() do
            local id, name, _, _, _, _, _, _, _, unlocked = specs.GetSpecializationInfo(i)
            if id and unlocked ~= false then print("WowAgent 可选专精", id, name) end
        end
    end
    if p.spec > 0 and api and api.GetConfigIDsBySpecID and C_Traits and C_Traits.GetConfigInfo then
        local selected = api.GetLastSelectedSavedConfigID and api.GetLastSelectedSavedConfigID(p.spec)
        for _, id in ipairs(api.GetConfigIDsBySpecID(p.spec) or {}) do
            local info = C_Traits.GetConfigInfo(id)
            if info then print("WowAgent 天赋", id, info.name, id == selected and "已选中" or "") end
        end
    end
    print("WowAgent 状态", "生命", Public(UnitHealth("player")) or "?",
          "/", Public(UnitHealthMax("player")) or "?", "资源",
          Public(UnitPower("player")) or "?", "/", Public(UnitPowerMax("player")) or "?",
          IsMounted() and "已上马" or "未上马", IsFlying and IsFlying() and "飞行中" or "")
    Gear()
end

local function Update()
    local p = Identity()
    local target, dead = HostileTarget()
    local flags = dead and 1 or 0
    if armed and ActionMatches(armed.key, armed.spell, armed.body) then flags = flags + 2 end
    if Public(UnitIsDead("player")) ~= false or Public(UnitIsGhost("player")) ~= false
        or (IsFlying and Public(IsFlying()) ~= false) then flags = flags + 4 end
    sequence = (sequence + 1) % 128
    local key = armed and armed.key or 0
    local info = flags + key * 8 + stateEpoch * 128 + sequence * 8192
    local values = {p.class + p.race * 32 + p.project * 8192, p.build,
        p.spec + p.level * 4096, p.config, target, info}
    for i, value in ipairs(values) do
        rows[i](i + 9, value >= 0 and value <= 1048575 and value or 0)
    end
end

local function ArmAction(key, action, body)
    Disarm()
    if not ActionMatches(key, action, body) then
        print("WowAgent：无法核对该键上的动作。")
        return
    end
    armed = {key = tonumber(key), spell = action, body = body}
    screen:Show()
    Update()
    print("WowAgent：战斗绑定已核对，数字键 " .. key)
    return true
end

local function NormalBar()
    local bar = C_ActionBar or {}
    local override = bar.HasOverrideActionBar or HasOverrideActionBar
    local vehicle = bar.HasVehicleActionBar or HasVehicleActionBar
    local possess = bar.IsPossessBarVisible or HasPossessBar
    local temporary = bar.HasTempShapeshiftActionBar or HasTempShapeshiftActionBar
    return not (InCombatLockdown() or (override and override()) or (vehicle and vehicle())
                or (possess and possess()) or (temporary and temporary()))
end

local function MacroDefinition(kind, id)
    if kind == "assist" then
        return "WASAssist", AssistMacro(), "assist"
    elseif kind == "spell" then
        return "WAS" .. id, MacroBody(id), id
    end
    return UtilityMacro(kind, id)
end

local function SetupAction(kind, id, requestedKey)
    if not NormalBar() then
        print("WowAgent：请在脱战后的普通动作栏上配置。")
        return
    end
    local bar = C_ActionBar or {}
    local name, body, action = MacroDefinition(kind, id)
    if not name then return end
    local hasAction = bar.HasAction or HasAction
    local key, slot
    for candidate = requestedKey or 1, requestedKey or 9 do
        local bound = BoundSlot(candidate)
        if ActionMatches(candidate, action, body) then
            if action then return ArmAction(candidate, action, body)
            else print("WowAgent：宏已在数字键 " .. candidate) end
            return true
        end
        if not slot and bound and hasAction and Public(hasAction(bound)) == false then
            key, slot = candidate, bound
        end
    end
    if not slot then
        if requestedKey and not BoundSlot(requestedKey) then
            print("WowAgent：数字键 " .. requestedKey .. " 未绑定当前主动作栏，请检查游戏按键设置。")
        elseif requestedKey then
            print("WowAgent：数字键 " .. requestedKey .. " 对应的主栏格子已有动作，请先空出该格。")
        else
            print("WowAgent：没有绑定到主动作栏的空位，请空出一个数字键对应的格子。")
        end
        return
    end
    local place = bar.PutActionInSlot or PlaceAction
    if not GetCursorInfo or not PickupMacro or not place or not ClearCursor then
        print("WowAgent：客户端缺少自动放置动作栏的接口。")
        return
    end
    if GetCursorInfo() ~= nil then
        print("WowAgent：鼠标上已有物品或动作，请先放下。")
        return
    end
    local index = WriteMacro(name, body)
    if not index then return end
    if GetCursorInfo() ~= nil or BoundSlot(key) ~= slot or Public(hasAction(slot)) ~= false then
        print("WowAgent：动作栏或鼠标状态已变化，请重试。")
        return
    end
    pcall(PickupMacro, index)
    local cursor, picked = GetCursorInfo()
    if Public(cursor) ~= "macro" or Public(picked) ~= index then
        print("WowAgent：客户端未能拿起宏，请在 /macro 面板手动放置。")
        return
    end
    if not InCombatLockdown() and BoundSlot(key) == slot and Public(hasAction(slot)) == false then
        pcall(place, slot)
    end
    cursor, picked = GetCursorInfo()
    if Public(cursor) == "macro" and Public(picked) == index then ClearCursor() end
    if not ActionMatches(key, action, body) then
        local placedKind, placedID = ReadAction(slot)
        print(string.format("WowAgent：数字键 %s 的动作核对未通过，类型 %s，宏或技能编号 %s。请查看准备状态后重试。",
            key, tostring(placedKind or "空"), tostring(placedID or "无法确认")))
    elseif action then
        return ArmAction(key, action, body)
    else
        print("WowAgent：宏已放到数字键 " .. key)
        return true
    end
end

screen:SetSize(142, 96)
screen:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 0, 80)
screen:SetFrameStrata("TOOLTIP")
for i = 1, 6 do rows[i] = WowAgent.NewPixelRow(screen, (i - 1) * 16) end
local elapsed = 0
screen:SetScript("OnUpdate", function(_, dt)
    elapsed = elapsed + dt
    if elapsed < .16 then return end
    elapsed = 0
    Update()
end)
screen:Hide()

local events = CreateFrame("Frame")
for _, name in ipairs({"PLAYER_TARGET_CHANGED", "PLAYER_ENTERING_WORLD", "PLAYER_SPECIALIZATION_CHANGED",
                       "TRAIT_CONFIG_UPDATED", "ACTIVE_TALENT_GROUP_CHANGED", "CHARACTER_POINTS_CHANGED",
                       "SPELLS_CHANGED", "PLAYER_EQUIPMENT_CHANGED"}) do
    pcall(events.RegisterEvent, events, name)
end
events:SetScript("OnEvent", function(_, event, unit)
    if event == "PLAYER_TARGET_CHANGED" then
        stateEpoch = (stateEpoch + 1) % 64
    elseif event ~= "PLAYER_SPECIALIZATION_CHANGED" or unit == "player" then
        Disarm()
    end
end)

local function SwitchLoadout(loadout)
    local p, api = Identity(), C_ClassTalents
    if api and api.LoadConfig and api.GetConfigIDsBySpecID and p.spec > 0 then
        local ids = api.GetConfigIDsBySpecID(p.spec) or {}
        for _, id in ipairs(ids) do
            if id == loadout then
                if api.SwitchToLoadoutByName and C_Traits and C_Traits.GetConfigInfo then
                    local info = C_Traits.GetConfigInfo(id)
                    local name = info and info.name
                    if not name or name == "" then
                        print("WowAgent：指定天赋配置的名称不可用。")
                        return
                    end
                    for _, otherID in ipairs(ids) do
                        if otherID ~= id then
                            local other = C_Traits.GetConfigInfo(otherID)
                            if other and other.name and (other.name == name
                                or (strcmputf8i and strcmputf8i(other.name, name) == 0)) then
                                print("WowAgent：存在同名天赋配置，请先在天赋面板区分名称。")
                                return
                            end
                        end
                    end
                    Disarm()
                    api.SwitchToLoadoutByName(name)
                    print("WowAgent：已请求切换天赋，使用 /wagent combat prep 查看结果。")
                    return true
                end
                Disarm()
                local result, reason = api.LoadConfig(id, true)
                print("WowAgent loadout result:", result, reason)
                return type(result) == "number" and result > 0
            end
        end
    elseif SetActiveTalentGroup and GetNumTalentGroups and loadout >= 1 and loadout <= GetNumTalentGroups() then
        Disarm()
        SetActiveTalentGroup(loadout)
        print("WowAgent talent group request:", loadout)
        return true
    end
    print("WowAgent：当前专精没有这个已保存的天赋配置。")
end

local function SetupLoadout(id, key)
    local api, spec = C_ClassTalents, Identity().spec
    if not api or not api.GetLastSelectedSavedConfigID or not C_Timer or not C_Timer.After or spec <= 0 then
        print("WowAgent：当前客户端无法确认天赋切换结果。")
        return
    end
    if not AssistMacro() then
        print("WowAgent：当前角色的一键辅助不可用。")
        return
    end
    if api.GetLastSelectedSavedConfigID(spec) == id then
        SetupAction("assist", nil, key)
        return
    end
    if not SwitchLoadout(id) then return end
    local request = {}
    pendingSetup = request
    local remaining, confirmed = 40, false
    local function Check()
        if pendingSetup ~= request then return end
        if InCombatLockdown() or Identity().spec ~= spec then
            pendingSetup = nil
            print("WowAgent：状态已改变，动作栏配置已停止。")
            return
        end
        local selected = api.GetLastSelectedSavedConfigID(spec) == id
        if selected and confirmed then
            pendingSetup = nil
            SetupAction("assist", nil, key)
        elseif remaining > 0 then
            confirmed, remaining = selected, remaining - 1
            C_Timer.After(.2, Check)
        else
            pendingSetup = nil
            print("WowAgent：未确认天赋切换，动作栏配置已停止。")
        end
    end
    C_Timer.After(.2, Check)
end

function WowAgent.CombatCommand(args)
    if args == "layout" or args:match("^layout ") then
        pendingSetup = nil
        if WowAgent.ActionLayoutCommand then
            return WowAgent.ActionLayoutCommand(args:sub(8))
        else
            print("WowAgent：请完全退出并重开客户端以加载动作栏布局模块。")
        end
        return
    end
    if args ~= "scan" and args ~= "prep" and args ~= "profile" and args ~= "mounts" and args ~= "gear" then
        pendingSetup = nil
    end
    if args == "scan" then
        screen:Show()
        Update()
    elseif args == "off" then
        Disarm()
        screen:Hide()
    elseif InCombatLockdown() then
        print("WowAgent: leave combat to configure actions")
    elseif args == "profile" then
        WowAgentDB = WowAgentDB or {}
        WowAgentDB.combatProfile = TalentSnapshot(Identity())
        print("WowAgent combat profile captured. Reload to write SavedVariables.")
    elseif args == "prep" then
        PrepStatus()
    elseif args == "mounts" then
        ListMounts()
    elseif args == "gear" then
        Gear()
    elseif args == "macro assist" then
        WriteMacro("WASAssist", AssistMacro())
    else
        local setupKind, setupID, setupKey = args:match("^setup (%a+) (%d+) ([1-9])$")
        if not setupKind then setupKind, setupID = args:match("^setup (%a+) (%d+)$") end
        local assistKey = args == "setup assist" and "" or args:match("^setup assist ([1-9])$")
        local spell = tonumber(args:match("^macro (%d+)$"))
        local loadout = tonumber(args:match("^loadout (%d+)$"))
        local spec = tonumber(args:match("^spec (%d+)$"))
        local set = tonumber(args:match("^gear (%d+)$"))
        local macroKind, macroID = args:match("^macro (%a+) (%d+)$")
        local key, action = args:match("^arm ([1-9]) (%w+)$")
        if assistKey then
            SetupAction("assist", nil, tonumber(assistKey))
        elseif setupKind and setupKind ~= "assist" then
            setupID, setupKey = tonumber(setupID), tonumber(setupKey)
            if setupID < 1 or setupID > 2147483647 then
                print("WowAgent：动作 ID 超出范围。")
            elseif setupKind == "loadout" then
                SetupLoadout(setupID, setupKey)
            elseif setupKind == "spell" or setupKind == "mount" or setupKind == "fly" or setupKind == "quest"
                or setupKind == "buff" or setupKind == "supply" then
                SetupAction(setupKind, setupID, setupKey)
            else
                print("WowAgent：setup 可用 assist、spell、mount、fly、buff、supply 或 loadout。")
            end
        elseif macroKind then
            macroID = tonumber(macroID)
            if macroID < 1 or macroID > 2147483647 then
                print("WowAgent：宏动作 ID 超出范围。")
                return
            end
            if macroKind == "mount" or macroKind == "fly" or macroKind == "buff" or macroKind == "supply" then
                local name, body = UtilityMacro(macroKind, macroID)
                if name then WriteMacro(name, body) end
            else
                print("WowAgent：宏类型可选 mount、fly、buff 或 supply。")
            end
        elseif spell then
            WriteMacro("WAS" .. spell, MacroBody(spell))
        elseif set then
            Gear(set)
        elseif spec then
            local api = C_SpecializationInfo or {}
            local count = api.GetNumSpecializations or GetNumSpecializations
            local info = api.GetSpecializationInfo or GetSpecializationInfo
            local switch = api.SetSpecialization or SetSpecialization
            if not count or not info or not switch then
                print("WowAgent：当前客户端缺少专精切换接口，请从游戏天赋页切换。")
                return
            end
            for index = 1, count() do
                local id, name, _, _, _, _, _, _, _, unlocked = info(index)
                if id == spec then
                    if unlocked == false then
                        print("WowAgent：这个专精尚未解锁。")
                    elseif Identity().spec == spec then
                        print("WowAgent：当前已是" .. (name or "所选专精") .. "。")
                    else
                        Disarm()
                        if switch(index) == false then
                            print("WowAgent：游戏未接受专精切换，请检查游戏提示。")
                        else
                            print("WowAgent：已请求切换至" .. (name or "所选专精") .. "，读条完成后会刷新天赋列表。")
                        end
                    end
                    return
                end
            end
            print("WowAgent：所选专精不在当前角色的专精列表中，请重新选择。")
        elseif loadout then
            SwitchLoadout(loadout)
        elseif key then
            spell = tonumber(action)
            local body = action == "assist" and AssistMacro() or (spell and MacroBody(spell))
            ArmAction(key, spell or action, body)
        else
            print("/wagent combat scan | off | profile | macro <spellID> | spec <id> | loadout <id> | arm <1-9> <assist|spellID>")
            print("/wagent combat prep | mounts | gear [套装ID] | macro assist | macro <mount|fly|buff|supply> <ID>")
            print("/wagent combat setup assist [键] | setup <spell|mount|fly|buff|supply|loadout> <ID> [键]")
        end
    end
end

WowAgent.LayoutSupport = {
    Identity = Identity, Public = Public, NormalBar = NormalBar, ReadAction = ReadAction,
    Disarm = Disarm, MacroDefinition = MacroDefinition, WriteMacro = WriteMacro,
    SwitchLoadout = SwitchLoadout, LearnedSpells = Spellbook,
    SetupAction = SetupAction,
}
