-- Game UI for the existing WowAgent commands.
local panel, logView
local chatPrint = print

function WowAgent.Report(...)
    chatPrint(...)
    if not logView then return end
    local words = {}
    for i = 1, select("#", ...) do
        local value = select(i, ...)
        words[i] = issecretvalue and issecretvalue(value) and "?" or tostring(value)
    end
    logView:AddMessage(table.concat(words, " "), .87, .91, 1)
end

local function Combat(command)
    if WowAgent.CombatCommand then return WowAgent.CombatCommand(command) end
    WowAgent.Report("WowAgent：战斗模块尚未加载。")
end

local function Quest(command)
    if WowAgent.QuestCommand then return WowAgent.QuestCommand(command) end
    WowAgent.Report("WowAgent：当前客户端没有任务色条接口。")
end

local function Choice(id, label)
    return {value = tostring(id), label = label or tostring(id)}
end

local function Identity()
    local support = WowAgent.LayoutSupport
    return support and support.Identity() or {}
end

local function Specs()
    local result, api = {}, C_SpecializationInfo or {}
    local count, info = api.GetNumSpecializations or GetNumSpecializations, api.GetSpecializationInfo or GetSpecializationInfo
    local current = Identity().spec
    if count and info then
        for index = 1, count() do
            local id, name, _, _, _, _, _, _, _, unlocked = info(index)
            if id and id > 0 and name and unlocked ~= false then
                result[#result + 1] = Choice(id, name .. (id == current and " · 当前" or ""))
            end
        end
    end
    return result
end

local function SelectedLoadout()
    local api, spec = C_ClassTalents, Identity().spec
    if api and api.GetLastSelectedSavedConfigID and spec and spec > 0 then
        return api.GetLastSelectedSavedConfigID(spec)
    end
    if not (api and api.GetConfigIDsBySpecID) and GetActiveTalentGroup then return GetActiveTalentGroup() end
end

local function Loadouts(includeCurrent)
    local result = includeCurrent and {Choice("keep", "保留当前天赋")} or {}
    local api, spec, selected = C_ClassTalents, Identity().spec, SelectedLoadout()
    if api and api.GetConfigIDsBySpecID and C_Traits and C_Traits.GetConfigInfo then
        if not spec or spec <= 0 then return result end
        for _, id in ipairs(api.GetConfigIDsBySpecID(spec) or {}) do
            local info = C_Traits.GetConfigInfo(id)
            if info and info.name then
                result[#result + 1] = Choice(id, info.name .. (id == selected and " · 当前" or ""))
            end
        end
    elseif GetNumTalentGroups then
        for group = 1, GetNumTalentGroups() do
            result[#result + 1] = Choice(group, (group == 1 and "主天赋组" or "副天赋组")
                .. (group == selected and " · 当前" or ""))
        end
    end
    return result
end

local function OpenNativeTalents()
    local util = PlayerSpellsUtil
    if util and util.OpenToClassTalentsTab then
        util.OpenToClassTalentsTab()
        return PlayerSpellsFrame and PlayerSpellsFrame.TalentsFrame:IsVisible()
    end
end

local function Mounts()
    local result, api = {}, C_MountJournal
    if api and api.GetMountIDs and api.GetMountInfoByID then
        for _, id in ipairs(api.GetMountIDs()) do
            local name, _, _, _, _, _, favorite, _, _, hidden, collected = api.GetMountInfoByID(id)
            if favorite and collected and not hidden then result[#result + 1] = Choice(id, name .. "  #" .. id) end
        end
    end
    return result
end

local function Equipment()
    local result, api = {}, C_EquipmentSet
    if api and api.GetEquipmentSetIDs and api.GetEquipmentSetInfo then
        for _, id in ipairs(api.GetEquipmentSetIDs()) do
            local name, _, _, equipped = api.GetEquipmentSetInfo(id)
            if name then result[#result + 1] = Choice(id, name .. "  #" .. id .. (equipped and "  已穿戴" or "")) end
        end
    end
    return result
end

local function Spells()
    local result, support = {}, WowAgent.LayoutSupport
    for id, name in pairs(support and support.LearnedSpells() or {}) do
        result[#result + 1] = Choice(id, name .. "  #" .. id)
    end
    table.sort(result, function(a, b) return a.label < b.label end)
    return result
end

local function Supplies()
    local result, seen, api, bag = {}, {}, C_Item, C_Container
    if not api or not api.GetItemSpell or not bag or not bag.GetContainerItemID
        or not bag.GetContainerNumSlots then return result end
    for index = 0, (NUM_BAG_SLOTS or 4) + (NUM_REAGENTBAG_SLOTS or 0) do
        for slot = 1, bag.GetContainerNumSlots(index) do
            local id = bag.GetContainerItemID(index, slot)
            if id and not (issecretvalue and issecretvalue(id)) and not seen[id] then
                seen[id] = true
                local spell = api.GetItemSpell(id)
                if spell then
                    local name = api.GetItemNameByID and api.GetItemNameByID(id)
                    result[#result + 1] = Choice(id, (name or spell) .. "  #" .. id)
                end
            end
        end
    end
    table.sort(result, function(a, b) return a.label < b.label end)
    return result
end

local function Macros()
    local result = {}
    if not GetNumMacros or not GetMacroInfo then return result end
    local global, character = GetNumMacros()
    local function Add(first, count)
        for index = first, first + count - 1 do
            local name = GetMacroInfo(index)
            if name then result[#result + 1] = Choice(index, name .. "  #" .. index) end
        end
    end
    Add(1, global)
    if MAX_ACCOUNT_MACROS then Add(MAX_ACCOUNT_MACROS + 1, character) end
    return result
end

local function Quests()
    local result, api, seen = {}, C_QuestLog, {}
    local function Add(id, title)
        if type(id) == "number" and id > 0 and not seen[id] then
            seen[id] = true
            result[#result + 1] = Choice(id, (title or "任务") .. "  #" .. id)
        end
    end
    local current = GetQuestID and GetQuestID()
    if current and current > 0 then
        Add(current, GetTitleText and GetTitleText())
    end
    if C_GossipInfo and C_GossipInfo.GetAvailableQuests then
        for _, quest in ipairs(C_GossipInfo.GetAvailableQuests()) do Add(quest.questID, quest.title) end
    end
    if api and api.GetNumQuestLogEntries and api.GetInfo then
        for index = 1, api.GetNumQuestLogEntries() do
            local info = api.GetInfo(index)
            if info and not info.isHeader and not info.isHidden then Add(info.questID, info.title) end
        end
    end
    return result
end

local function OpenChoices(owner, entries, selectValue)
    if #entries == 0 then
        WowAgent.Report("WowAgent：当前没有可选项。")
    elseif MenuUtil and MenuUtil.CreateContextMenu then
        MenuUtil.CreateContextMenu(owner, function(_, root)
            if #entries > 12 and root.SetScrollMode then root:SetScrollMode(280) end
            for _, entry in ipairs(entries) do
                local value = entry.value
                root:CreateButton(entry.label, function() selectValue(value) end)
            end
        end)
    else
        for _, entry in ipairs(entries) do WowAgent.Report(entry.label) end
    end
end

local function Label(parent, value, x, y, width)
    local label = parent:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    label:SetPoint("TOPLEFT", x, -y)
    label:SetText(value)
    if width then
        label:SetWidth(width)
        label:SetJustifyH("LEFT")
    end
    return label
end

local function Button(parent, value, x, y, width, action)
    local button = CreateFrame("Button", nil, parent, "UIPanelButtonTemplate")
    button:SetSize(width, 25)
    button:SetPoint("TOPLEFT", x, -y)
    button:SetText(value)
    button:SetScript("OnClick", action)
    return button
end

local function Profile()
    local support = WowAgent.LayoutSupport
    local guid = support and support.Public(UnitGUID("player"))
    if not guid then return end
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.panel = WowAgentDB.panel or {}
    WowAgentDB.panel[guid] = WowAgentDB.panel[guid] or {}
    return WowAgentDB.panel[guid]
end

local function BuildPanel()
    local frame = CreateFrame("Frame", "WowAgentControlPanel", UIParent, "BasicFrameTemplateWithInset")
    frame:SetSize(650, 560)
    frame:SetPoint("CENTER")
    frame:SetFrameStrata("DIALOG")
    frame:SetClampedToScreen(true)
    frame:SetMovable(true)
    frame:EnableMouse(true)
    frame:RegisterForDrag("LeftButton")
    frame:SetScript("OnDragStart", frame.StartMoving)
    frame:SetScript("OnDragStop", frame.StopMovingOrSizing)
    if frame.TitleText then frame.TitleText:SetText("WowAgent 控制面板")
    else Label(frame, "WowAgent 控制面板", 14, 6) end
    if UISpecialFrames then UISpecialFrames[#UISpecialFrames + 1] = frame:GetName() end

    local output = CreateFrame("ScrollingMessageFrame", nil, frame)
    output:SetPoint("BOTTOMLEFT", 20, 20)
    output:SetPoint("BOTTOMRIGHT", -22, 20)
    output:SetHeight(103)
    output:SetFontObject(GameFontHighlightSmall)
    output:SetJustifyH("LEFT")
    output:SetFading(false)
    output:SetMaxLines(100)
    output:EnableMouseWheel(true)
    output:SetScript("OnMouseWheel", function(self, delta)
        if delta > 0 then self:ScrollUp() else self:ScrollDown() end
    end)
    logView = output
    Label(frame, "操作记录 · 滚轮查看", 20, 420)
    Button(frame, "清空记录", 532, 413, 96, function() output:Clear() end)

    local tabs, fields, defaults, selectors = {}, {}, {}, {}
    local profile, loading, preview
    local apply
    local function Invalidate()
        preview = nil
        if apply then apply:Disable() end
    end
    local function ClearMain()
        Invalidate()
        Combat("layout clear")
    end
    local function Field(parent, name, x, y, width, default, invalidate, multiline)
        local scroll, box
        if multiline then
            scroll = CreateFrame("ScrollFrame", nil, parent)
            scroll:SetSize(width, 65)
            scroll:SetPoint("TOPLEFT", x, -y)
            local backdrop = scroll:CreateTexture(nil, "BACKGROUND")
            backdrop:SetAllPoints()
            backdrop:SetColorTexture(.05, .07, .10, .88)
            box = CreateFrame("EditBox", nil, scroll)
            scroll:SetScrollChild(box)
            scroll:EnableMouseWheel(true)
            scroll:SetScript("OnMouseWheel", function(self, delta)
                local nextOffset = self:GetVerticalScroll() - 18 * delta
                self:SetVerticalScroll(math.max(0, math.min(self:GetVerticalScrollRange(), nextOffset)))
            end)
        else
            box = CreateFrame("EditBox", nil, parent, "InputBoxTemplate")
            box:SetPoint("TOPLEFT", x, -y)
        end
        box:SetSize(width, multiline and 65 or 25)
        box:SetAutoFocus(false)
        box:SetMaxLetters(multiline and 2048 or 80)
        if multiline then
            box:SetMultiLine(true)
            box:SetFontObject(GameFontHighlightSmall)
            box:SetTextInsets(5, 5, 3, 3)
        end
        box:SetScript("OnEscapePressed", box.ClearFocus)
        if not multiline then box:SetScript("OnEnterPressed", box.ClearFocus) end
        box:SetScript("OnTextChanged", function(self)
            if profile and not loading then profile[name] = self:GetText() end
            if scroll then
                local _, lines = self:GetText():gsub("\n", "")
                self:SetHeight(math.max(65, (lines + 1) * 16 + 6))
            end
            if invalidate then Invalidate() end
        end)
        defaults[name], fields[name] = default or "", box
        return box
    end
    local function Text(box)
        return box:GetText():match("^%s*(.-)%s*$")
    end
    local function Select(parent, box, x, y, provider)
        return Button(parent, "选择 ▾", x, y, 72, function(button)
            OpenChoices(button, provider(), function(value) box:SetText(value); box:ClearFocus() end)
        end)
    end
    local function NamedSelect(parent, name, x, y, width, provider, fallback, changed)
        local button
        local function Refresh()
            local saved = profile and profile[name]
            local default = tostring(fallback())
            local selected
            for _, entry in ipairs(provider()) do
                if entry.value == tostring(saved) then selected = entry; break end
                if entry.value == default then selected = entry end
            end
            button:SetText((selected and selected.label or "请选择") .. " ▾")
            if selected then return selected.value, selected.label end
        end
        button = Button(parent, "请选择 ▾", x, y, width, function(owner)
            OpenChoices(owner, provider(), function(value)
                if profile then profile[name] = value end
                Refresh()
                if changed then changed() end
            end)
        end)
        selectors[#selectors + 1] = Refresh
        return Refresh
    end
    local function NewTab(index, title)
        local page = CreateFrame("Frame", nil, frame)
        page:SetPoint("TOPLEFT", 20, -66)
        page:SetPoint("BOTTOMRIGHT", -20, 142)
        tabs[index] = page
        Button(frame, title, 20 + (index - 1) * 123, 31, 112, function()
            for i, tab in ipairs(tabs) do tab:SetShown(i == index) end
        end)
        page:SetShown(index == 1)
        return page
    end
    local function NumberCommand(box, prefix, allowZero, suffix)
        local value = Text(box)
        if not value:match("^%d+$") or tonumber(value) < (allowZero and 0 or 1)
            or tonumber(value) > 2147483647 then
            return WowAgent.Report("WowAgent：请先选择或输入有效的 ID。")
        end
        box:ClearFocus()
        return Combat(prefix .. value .. (suffix or ""))
    end
    local function SelectionCommand(value, prefix, suffix)
        if not value then return WowAgent.Report("WowAgent：请先从下拉菜单选择。") end
        return Combat(prefix .. value .. (suffix or ""))
    end

    local ready = NewTab(1, "战斗准备")
    Label(ready, "脱战配置 · 切换专精后，下方列表显示该专精的天赋", 0, 5)
    Label(ready, "输出键位", 0, 45)
    local key = Field(ready, "key", 99, 40, 53)
    local function OutputKey()
        local value = Text(key)
        if value == "" or value:match("^[1-9]$") then
            key:ClearFocus()
            return value == "" and "" or " " .. value
        else WowAgent.Report("WowAgent：输出键留空自动选择，或填 1–9。") end
    end
    Button(ready, "配置一键输出", 170, 40, 117, function()
        local suffix = OutputKey()
        if suffix then Combat("setup assist" .. suffix) end
    end)
    Button(ready, "只建输出宏", 294, 40, 100, function() Combat("macro assist") end)
    Label(ready, "留空自动选主栏空位", 407, 45)
    Label(ready, "专精", 0, 85)
    local spec = NamedSelect(ready, "spec", 99, 80, 174, Specs, function() return Identity().spec end)
    Button(ready, "切换专精", 281, 80, 107, function() SelectionCommand(spec(), "spec ") end)
    Label(ready, "天赋配置", 0, 119)
    local loadout = NamedSelect(ready, "loadout", 99, 114, 174, Loadouts, SelectedLoadout)
    Button(ready, "切换天赋", 281, 114, 107, function() SelectionCommand(loadout(), "loadout ") end)
    Button(ready, "切天赋并配置输出", 399, 114, 147, function()
        local suffix = OutputKey()
        if suffix then SelectionCommand(loadout(), "setup loadout ", suffix) end
    end)
    Label(ready, "装备套装", 0, 153)
    local gear = Field(ready, "gear", 99, 148, 87)
    Select(ready, gear, 197, 148, Equipment)
    Button(ready, "换装", 281, 148, 107, function() NumberCommand(gear, "gear ", true) end)
    Label(ready, "坐骑", 0, 193)
    local mount = Field(ready, "mount", 99, 188, 87)
    Select(ready, mount, 197, 188, Mounts)
    Button(ready, "放置上马宏", 281, 188, 107, function() NumberCommand(mount, "setup mount ") end)
    Button(ready, "放置飞行宏", 399, 188, 117, function() NumberCommand(mount, "setup fly ") end)
    Label(ready, "增益技能 ID", 0, 227)
    local buff = Field(ready, "buff", 99, 222, 87)
    Select(ready, buff, 197, 222, Spells)
    Button(ready, "放置自用增益宏", 281, 222, 132, function() NumberCommand(buff, "setup buff ") end)
    Label(ready, "补给物品 ID", 0, 261)
    local supply = Field(ready, "supply", 99, 256, 87)
    Select(ready, supply, 197, 256, Supplies)
    Button(ready, "放置补给宏", 281, 256, 132, function() NumberCommand(supply, "setup supply ") end)
    Button(ready, "查看准备状态", 0, 304, 114, function() Combat("prep") end)
    Button(ready, "显示战斗色条", 122, 304, 114, function() Combat("scan") end)
    Button(ready, "停用战斗色条", 244, 304, 114, function() Combat("off") end)
    Button(ready, "清空主栏", 366, 304, 114, ClearMain)
    Button(ready, "恢复备份", 488, 304, 114, function() Combat("layout restore"); Invalidate() end)

    local quests = NewTab(2, "任务与色条")
    Label(quests, "从任务列表选一项，或用空格分隔多个任务 ID。", 0, 8)
    local ids = Field(quests, "quests", 6, 42, 335)
    Select(quests, ids, 352, 42, Quests)
    Button(quests, "开始追踪", 433, 42, 108, function()
        local value = Text(ids)
        if value:match("^%d[%d ]*$") then
            ids:ClearFocus()
            Quest("track " .. value)
        else WowAgent.Report("WowAgent：请选择任务，或输入用空格分隔的任务 ID。") end
    end)
    Button(quests, "扫描任务日志", 0, 87, 116, function() Quest("scan") end)
    Button(quests, "隐藏任务色条", 125, 87, 116, function() Quest("off") end)
    Button(quests, "读取当前位置", 250, 87, 116, function()
        local _, packed = WowAgent.BuildPos()
        local map = C_Map and C_Map.GetBestMapForUnit and C_Map.GetBestMapForUnit("player")
        if packed then
            WowAgent.Report(string.format("地图 %s  航点 [%.4f, %.4f]", tostring(map or "?"),
                                          math.floor(packed / 1024) / 1023, (packed % 1024) / 1023))
        else WowAgent.Report("WowAgent：当前地图无法读取坐标。") end
    end)
    Button(quests, "拖动主色条", 0, 144, 116, function()
        WowAgent.SetMovable(true)
        WowAgent.Report("WowAgent：可拖动主色条，完成后点锁定。")
    end)
    Button(quests, "锁定主色条", 125, 144, 116, function() WowAgent.SetMovable(false) end)
    Button(quests, "准备自动接任务", 0, 198, 140, function()
        local value = Text(ids)
        if value == "" or value:match("^%d+$") then Quest("prepare" .. (value == "" and "" or " " .. value))
        else WowAgent.Report("WowAgent：自动接任务测试每次选择一个任务。") end
    end)
    Label(quests, "先打开 NPC 任务对话，准备按钮会将该任务的操作宏放到数字键 9。", 0, 241, 590)
    Label(quests, "然后启动本地接任务测试。StartLux 决策，脚本按键，任务日志确认成功。", 0, 278, 590)

    local layout = NewTab(3, "动作栏布局")
    Label(layout, "范围", 0, 8)
    local scope = Field(layout, "scope", 43, 3, 80, "main", true)
    Select(layout, scope, 134, 3, function() return {Choice("main", "当前主栏"), Choice("visible", "可见标准动作栏")} end)
    Label(layout, "目标天赋", 232, 8)
    local target = NamedSelect(layout, "target", 308, 3, 232, function() return Loadouts(true) end,
        function() return "keep" end, Invalidate)
    Label(layout, "格子", 0, 50)
    local cell = Field(layout, "cell", 43, 45, 58, "1")
    Select(layout, cell, 112, 45, function()
        local result = {}
        local labels = WowAgent.GetActionLayoutLabels and WowAgent.GetActionLayoutLabels(Text(scope)) or {}
        for _, label in ipairs(labels) do result[#result + 1] = Choice(label) end
        return result
    end)
    Label(layout, "动作", 202, 50)
    local kind = Field(layout, "kind", 245, 45, 71, "assist")
    local kinds = {
        {"assist", "一键辅助"}, {"spell", "技能"}, {"macro", "现有宏"},
        {"cast", "输出宏"}, {"mount", "上马宏"}, {"fly", "飞行宏"},
        {"buff", "增益宏"}, {"supply", "补给宏"},
    }
    Select(layout, kind, 327, 45, function()
        local result = {}
        for _, entry in ipairs(kinds) do result[#result + 1] = Choice(entry[1], entry[2]) end
        return result
    end)
    Label(layout, "ID", 416, 50)
    local actionID = Field(layout, "actionID", 442, 45, 105)
    Select(layout, actionID, 262, 87, function()
        local action = Text(kind)
        if action == "mount" or action == "fly" then return Mounts() end
        if action == "supply" then return Supplies() end
        if action == "macro" then return Macros() end
        if action ~= "assist" then return Spells() end
        return {}
    end):SetText("选择 ID ▾")
    Label(layout, "待预览的格子 · 未指定的格子会清空", 0, 131)
    local entries = Field(layout, "entries", 0, 150, 592, "", true, true)
    local function ChangeEntry(remove)
        local label, action, id = Text(cell), Text(kind), Text(actionID)
        if not label:match("^[%w.]+$") then return WowAgent.Report("WowAgent：请先选择格子。") end
        local updated = {}
        for token in Text(entries):gmatch("%S+") do
            if token:match("^([^=]+)=") ~= label then updated[#updated + 1] = token end
        end
        if not remove then
            if action == "assist" then
                updated[#updated + 1] = label .. "=assist"
            else
                local known
                for _, entry in ipairs(kinds) do if action == entry[1] then known = true end end
                if not known or not id:match("^%d+$") then
                    return WowAgent.Report("WowAgent：请选择动作并填写对应 ID。")
                end
                updated[#updated + 1] = label .. "=" .. action .. ":" .. id
            end
        end
        entries:SetText(table.concat(updated, "\n"))
    end
    Button(layout, "添加或更新格子", 0, 87, 133, function() ChangeEntry(false) end)
    Button(layout, "移除格子", 142, 87, 100, function() ChangeEntry(true) end)
    local function PlanCommand()
        return "layout plan " .. Text(scope) .. " " .. target() .. " " .. Text(entries):gsub("%s+", " ")
    end
    Button(layout, "预览布局", 0, 236, 100, function()
        entries:ClearFocus()
        Invalidate()
        local command = PlanCommand()
        if Combat(command) then preview = command; apply:Enable() end
    end)
    apply = Button(layout, "确认重排", 110, 236, 108, function()
        local command = PlanCommand()
        if not preview or preview ~= command then return Invalidate() end
        Invalidate()
        if Combat(command) then Combat("layout apply") end
    end)
    apply:Disable()
    Button(layout, "恢复备份", 228, 236, 100, function() Combat("layout restore"); Invalidate() end)
    Button(layout, "取消等待", 338, 236, 100, function() Combat("layout cancel"); Invalidate() end)
    Button(layout, "采用布局", 448, 236, 100, function()
        if StaticPopupDialogs and StaticPopup_Show then
            StaticPopupDialogs.WOWAGENT_ACCEPT_LAYOUT = {
                text = "采用当前布局并删除本次恢复备份？", button1 = ACCEPT, button2 = CANCEL,
                OnAccept = function() Combat("layout accept"); Invalidate() end,
                timeout = 0, whileDead = false, hideOnEscape = true,
            }
            StaticPopup_Show("WOWAGENT_ACCEPT_LAYOUT")
        else WowAgent.Report("WowAgent：请用 /wagent combat layout accept 确认。") end
    end)
    Button(layout, "清空主栏", 0, 281, 100, ClearMain)
    Label(layout, "清空当前主栏 12 格，自动备份。可用「恢复备份」还原。", 110, 287, 490)

    local talents = NewTab(4, "天赋推荐")
    Label(talents, "按当前专精和玩法推荐。跨专精时先在「战斗准备」切换。", 0, 5, 595)
    local contexts = {
        Choice("solo", "单刷 / 地下堡"),
        Choice("raid", "团本单目标"),
        Choice("cleave", "团本多目标"),
        Choice("mythic", "大秘境"),
    }
    local importNames = {solo = "WA 单刷", raid = "WA 团本单体", cleave = "WA 团本多目标", mythic = "WA 大秘境"}
    local refreshTalents
    Label(talents, "玩法", 0, 45)
    local context = NamedSelect(talents, "talentContext", 99, 40, 174, function() return contexts end,
        function() return "solo" end, function() refreshTalents() end)
    local active = Label(talents, "", 0, 88, 595)
    local recommendation = Label(talents, "", 0, 119, 595)
    local detail = Label(talents, "", 0, 148, 595)
    local codeBox = CreateFrame("EditBox", nil, talents, "InputBoxTemplate")
    codeBox:SetSize(579, 25)
    codeBox:SetPoint("TOPLEFT", 7, -232)
    codeBox:SetAutoFocus(false)
    codeBox:SetScript("OnEscapePressed", codeBox.ClearFocus)
    codeBox:SetScript("OnEnterPressed", codeBox.ClearFocus)

    local openImport, selectCode
    refreshTalents = function()
        local identity, db = Identity(), WowAgent.RetailTalentGuides
        local mode, modeLabel = context()
        local retail = WOW_PROJECT_MAINLINE and identity.project == WOW_PROJECT_MAINLINE
        local guide = retail and db and db.specs[identity.spec]
        local code, summary, note
        active:SetText("当前专精 · " .. (identity.specName or "未解锁"))
        if guide and identity.version == db.patch and identity.level >= db.minLevel then
            code = guide.codes[mode]
            summary = "推荐 · " .. guide.hero .. " · " .. modeLabel
            note = db.sourceName .. " · 正式服 " .. db.patch
        elseif retail then
            summary = C_ClassTalents and C_ClassTalents.GetHasStarterBuild and C_ClassTalents.GetHasStarterBuild()
                and "建议在游戏天赋页的下拉菜单选择「起始配置」。" or "可打开游戏天赋页查看可用配置。"
            if not guide then
                note = "当前专精暂未收录场景攻略。"
            elseif identity.version ~= db.patch then
                note = "推荐适用 " .. db.patch .. "，当前客户端版本为 " .. tostring(identity.version or "未知") .. "。"
            else
                note = "这套攻略适用 " .. db.minLevel .. " 级满级角色。"
            end
        else
            summary = "场景推荐适用于正式服。"
            note = ""
        end
        recommendation:SetText(summary)
        detail:SetText(note)
        codeBox:SetShown(code ~= nil)
        if code and codeBox:GetText() ~= code then codeBox:SetText(code) end
        openImport:SetShown(code ~= nil)
        selectCode:SetShown(code ~= nil)
        return code, importNames[mode]
    end
    openImport = Button(talents, "打开原生导入", 0, 187, 135, function()
        local code, name = refreshTalents()
        if not code then return end
        if InCombatLockdown and InCombatLockdown() then return WowAgent.Report("WowAgent：请脱战后导入。") end
        if OpenNativeTalents() then
            local dialog = ClassTalentLoadoutImportDialog
            if dialog and dialog:IsShown() then
                return WowAgent.Report("WowAgent：请先关闭已打开的导入对话框。")
            end
            if dialog and dialog.ShowDialog and dialog.ImportControl and dialog.NameControl then
                dialog:ShowDialog()
                dialog.ImportControl:GetEditBox():SetText(code)
                dialog.NameControl:GetEditBox():SetText(name)
                frame:Hide()
                return
            end
        end
        WowAgent.Report("WowAgent：请复制导入码，在游戏天赋页手动导入。")
    end)
    selectCode = Button(talents, "选中导入码", 144, 187, 120, function()
        if not refreshTalents() then return end
        codeBox:SetFocus()
        codeBox:HighlightText()
    end)
    Button(talents, "打开游戏天赋页", 274, 187, 142, function()
        if InCombatLockdown and InCombatLockdown() then return WowAgent.Report("WowAgent：请脱战后打开天赋页。") end
        if OpenNativeTalents() then frame:Hide()
        else WowAgent.Report("WowAgent：请用游戏菜单或快捷键打开天赋页。") end
    end)
    Label(talents, "在游戏窗口确认导入。导入后可在「战斗准备」按名称找到方案。", 0, 275, 595)
    talents:SetScript("OnShow", refreshTalents)

    local navigation = NewTab(5, "任务寻路")
    Label(navigation, "继续当前对话、执行已接任务，或寻找同地图的附近任务。", 0, 7)
    Label(navigation, "任务", 0, 45)
    local npc, itemMode
    local navQuest = NamedSelect(navigation, "navQuest", 104, 40, 385, Quests, function()
        local active = Quests()
        if #active == 1 then return active[1].value end
        return C_SuperTrack and C_SuperTrack.GetSuperTrackedQuestID and C_SuperTrack.GetSuperTrackedQuestID()
    end, function()
        if npc then npc:SetText("") end
        if profile then profile.navItem, profile.navNPCQuest = nil, nil end
        if itemMode then itemMode() end
    end)
    local function TravelMounts(kind)
        local list = {Choice(0, "自动选择可用坐骑")}
        for _, entry in ipairs(WowAgent.GetNavigationMounts and WowAgent.GetNavigationMounts(kind) or {}) do
            list[#list + 1] = entry
        end
        return list
    end
    Label(navigation, "飞行坐骑", 0, 85)
    local fly = NamedSelect(navigation, "navFly", 104, 80, 300, function() return TravelMounts("fly") end,
        function() return 0 end)
    Label(navigation, "地面坐骑", 0, 119)
    local ground = NamedSelect(navigation, "navGround", 104, 114, 300, function() return TravelMounts("ground") end,
        function() return 0 end)
    Label(navigation, "任务键位", 0, 209)
    local navBindings = NamedSelect(navigation, "navBindings", 104, 204, 300, function()
        return {Choice(0, "沿用游戏键位"), Choice(1, "准备时启用临时任务键位")}
    end, function() return 0 end)
    local interactFlags = Enum and Enum.SoftTargetEnableFlags
    local getCVar, setCVar = C_CVar and C_CVar.GetCVar, C_CVar and C_CVar.SetCVar
    if interactFlags and getCVar and setCVar then
        local function InteractEnabled()
            return tonumber(getCVar("softTargetInteract")) == interactFlags.Any
        end
        local function RefreshInteract(button)
            button:SetText(InteractEnabled() and "停用游戏互动键" or "启用游戏互动键")
        end
        local interact = Button(navigation, "", 416, 204, 150, function(button)
            if InCombatLockdown and InCombatLockdown() then
                return WowAgent.Report("WowAgent：请脱战后更改游戏互动键设置。")
            end
            local enable = not InteractEnabled()
            local ok = pcall(setCVar, "softTargetInteract", enable and interactFlags.Any or interactFlags.Gamepad)
            RefreshInteract(button)
            if not ok or InteractEnabled() ~= enable then
                return WowAgent.Report("WowAgent：客户端未确认游戏互动键设置。")
            end
            WowAgent.Report(enable and "WowAgent：游戏互动键已启用。靠近并面向 NPC 后可按 F 互动。"
                or "WowAgent：游戏互动键已停用。")
        end)
        interact:SetScript("OnShow", RefreshInteract)
        RefreshInteract(interact)
    end
    Button(navigation, "准备任务寻路", 0, 158, 141, function()
        if WowAgent.PrepareQuestNavigation then
            WowAgent.PrepareQuestNavigation(tonumber((navQuest())), tonumber((fly())), tonumber((ground())),
                tonumber((navBindings())) == 1)
        else WowAgent.Report("WowAgent：当前客户端无法读取任务寻路接口。") end
    end)
    Button(navigation, "关闭寻路色条", 151, 158, 136, function()
        if WowAgent.StopQuestNavigation then WowAgent.StopQuestNavigation() end
    end)
    Button(navigation, "查看任务键位", 298, 158, 136, function()
        if WowAgent.ReportQuestBindings then WowAgent.ReportQuestBindings() end
    end)
    Button(navigation, "一键准备自动任务", 444, 158, 150, function()
        if WowAgent.PrepareQuestNavigation then
            local open = WowAgent.OpenQuestOfferID and WowAgent.OpenQuestOfferID()
            local resume = WowAgent.GetQuestRoundResumeID and WowAgent.GetQuestRoundResumeID()
            local quest = open and tostring(open) or resume and tostring(resume) or navQuest()
            if not (tonumber(quest) and C_QuestLog.IsOnQuest(tonumber(quest)))
                and not (open and tonumber(quest) == open) then
                local active = WowAgent.GetActiveSupportedQuestID()
                if active then quest = tostring(active) end
            end
            local useSavedOptions = profile and (profile.navNPCQuest == quest
                or (not profile.navNPCQuest and not open))
            if useSavedOptions and quest and Text(npc) ~= "" then
                profile.navQuest, profile.navNPCQuest = quest, quest
            end
            WowAgent.PrepareQuestNavigation(tonumber(quest), tonumber((fly())), tonumber((ground())),
                true, true, useSavedOptions and Text(npc) or nil,
                useSavedOptions and tonumber((itemMode())) == 1, true)
        end
    end)
    Button(navigation, "准备附近任务", 416, 80, 150, function()
        local open = WowAgent.OpenQuestOfferID and WowAgent.OpenQuestOfferID()
        local id = open or (WowAgent.GetNearbyQuestID and WowAgent.GetNearbyQuestID())
            or (WowAgent.GetQuestRoundResumeID and WowAgent.GetQuestRoundResumeID())
            or (WowAgent.GetNearbyActiveQuestID and WowAgent.GetNearbyActiveQuestID())
        if not id then
            return WowAgent.Report("WowAgent：当前没有查到附近可自动执行的任务。")
        end
        WowAgent.PrepareQuestNavigation(id, tonumber((fly())), tonumber((ground())), true, true, nil, nil, true)
    end)
    Label(navigation, "交付 NPC", 0, 246)
    npc = Field(navigation, "navNPC", 104, 241, 140)
    Button(navigation, "读取当前目标", 252, 241, 112, function()
        local support = WowAgent.LayoutSupport
        local guid = support and support.Public(UnitGUID("target"))
        local name = support and UnitName and support.Public(UnitName("target"))
        if not guid or not guid:match("^Creature%-") or not name or support.Public(UnitIsDead("target")) ~= false then
            return WowAgent.Report("WowAgent：请先选中活着的交付 NPC，或填写准确名字。")
        end
        npc:SetText(name)
    end)
    itemMode = NamedSelect(navigation, "navItem", 372, 241, 222, function()
        return {Choice(0, "只绑定任务道具"), Choice(1, "自动使用无目标道具")}
    end, function() return 0 end)
    Label(navigation, "W/S/方向键/空格移动 · Tab 选敌 · K 选 NPC · F 互动和拾取 · J 任务道具", 0, 277, 595)
    Label(navigation, "1 输出 · 5–8 坐骑 · 9 对话。准备成功后从「WoW 键鼠」授权并运行。", 0, 303, 595)

    local function RefreshSelections()
        for _, refresh in ipairs(selectors) do refresh() end
        if talents:IsShown() then refreshTalents() end
    end
    frame:SetScript("OnShow", function()
        profile, loading = Profile(), true
        for name, box in pairs(fields) do box:SetText(profile and profile[name] or defaults[name]) end
        loading = false
        RefreshSelections()
        Invalidate()
    end)
    for _, event in ipairs({"PLAYER_SPECIALIZATION_CHANGED", "TRAIT_CONFIG_LIST_UPDATED",
                            "TRAIT_CONFIG_UPDATED", "PLAYER_LEVEL_UP"}) do
        pcall(frame.RegisterEvent, frame, event)
    end
    frame:SetScript("OnEvent", function(_, event, unit)
        if event == "PLAYER_SPECIALIZATION_CHANGED" and unit and unit ~= "player" then return end
        if frame:IsShown() then RefreshSelections() end
        if event == "PLAYER_SPECIALIZATION_CHANGED" then Invalidate() end
    end)
    frame:Hide()
    return frame
end

function WowAgent_TogglePanel()
    panel = panel or BuildPanel()
    panel:SetShown(not panel:IsShown())
end

local launcher = CreateFrame("Button", "WowAgentPanelLauncher", UIParent, "UIPanelButtonTemplate")
launcher:SetSize(92, 23)
launcher:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 16, -135)
launcher:SetText("WowAgent")
launcher:SetClampedToScreen(true)
launcher:SetMovable(true)
launcher:RegisterForDrag("RightButton")
launcher:SetScript("OnDragStart", launcher.StartMoving)
launcher:SetScript("OnDragStop", launcher.StopMovingOrSizing)
launcher:SetScript("OnClick", WowAgent_TogglePanel)
launcher:SetScript("OnEnter", function(self)
    if not GameTooltip then return end
    GameTooltip:SetOwner(self, "ANCHOR_BOTTOMRIGHT")
    GameTooltip:SetText("WowAgent 控制面板")
    GameTooltip:AddLine("左键打开，右键拖动按钮。", 1, 1, 1)
    GameTooltip:Show()
end)
launcher:SetScript("OnLeave", function() if GameTooltip then GameTooltip:Hide() end end)
