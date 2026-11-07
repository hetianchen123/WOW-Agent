-- Exercise panel callbacks without a running WoW client.
local frames, combat, quests, menu = {}, {}, {}, {}
local guid = "Player-1"
local identity = {spec = 255, specName = "生存", version = "12.1.5", project = 1, level = 90}
local methods = {}
function methods:SetScript(event, script) self.scripts[event] = script end
function methods:SetPoint(_, x, y) self.x, self.y = x, y end
function methods:SetText(value)
    self.text = value
    if self.scripts.OnTextChanged then self.scripts.OnTextChanged(self) end
end
function methods:GetText() return self.text or "" end
function methods:SetShown(shown)
    local changed = self.shown ~= shown
    self.shown = shown
    if shown and changed and self.scripts.OnShow then self.scripts.OnShow(self) end
end
function methods:Hide() self:SetShown(false) end
function methods:IsShown() return self.shown end
function methods:Enable() self.disabled = false end
function methods:Disable() self.disabled = true end
function methods:GetName() return self.name end
function methods:CreateFontString() return CreateFrame("FontString", nil, self) end
function methods:CreateTexture() return CreateFrame("Texture", nil, self) end
function methods:AddMessage(value) self.lastMessage = value end
local noop = function() end

function CreateFrame(kind, name, parent, template)
    local frame = setmetatable({kind = kind, name = name, parent = parent, scripts = {}, disabled = false, shown = true}, {
        __index = function(_, key) return methods[key] or noop end,
    })
    if template == "BasicFrameTemplateWithInset" then frame.TitleText = frame:CreateFontString() end
    if name then _G[name] = frame end
    frames[#frames + 1] = frame
    return frame
end

function UnitGUID() return guid end
function GetQuestID() return 90882 end
function GetTitleText() return "若是那豺狼来了" end
function print() end
UIParent, UISpecialFrames = {}, {}
WOW_PROJECT_MAINLINE = 1
C_SpecializationInfo = {
    GetSpecializationInfo = function(index)
        if index == 1 then return 253, "野兽控制" end
        return 255, "生存"
    end,
}
function GetNumSpecializations() return 2 end
C_ClassTalents = {
    GetConfigIDsBySpecID = function(spec) return spec == 255 and {10, 20} or {} end,
    GetLastSelectedSavedConfigID = function(spec) return spec == 255 and 10 or nil end,
    GetHasStarterBuild = function() return true end,
}
C_Traits = {GetConfigInfo = function(id) return {name = id == 10 and "日常" or "地下城"} end}
local importCode, importName, toggles = nil, nil, 0
PlayerSpellsUtil = {
    OpenToClassTalentsTab = function() toggles = toggles + 1 end,
}
PlayerSpellsFrame = {TalentsFrame = {IsVisible = function() return toggles > 0 end}}
local function control(set)
    return {GetEditBox = function() return {SetText = function(_, text) set(text) end} end}
end
local inCombat, dialogOpen
function InCombatLockdown() return inCombat end
Enum = {SoftTargetEnableFlags = {Any = 3, Gamepad = 1}}
local softTargetInteract = "1"
C_CVar = {
    GetCVar = function(key) assert(key == "softTargetInteract"); return softTargetInteract end,
    SetCVar = function(key, value) assert(key == "softTargetInteract"); softTargetInteract = tostring(value) end,
}
ClassTalentLoadoutImportDialog = {
    IsShown = function() return dialogOpen end,
    ShowDialog = function() dialogOpen = true end,
    ImportControl = control(function(text) importCode = text end),
    NameControl = control(function(text) importName = text end),
}
MenuUtil = {CreateContextMenu = function(_, setup)
    menu = {}
    setup(nil, {CreateButton = function(_, title, select) menu[title] = select end})
end}
WowAgent = {
    LayoutSupport = {Public = function(value) return value end, Identity = function() return identity end},
    CombatCommand = function(command)
        combat[#combat + 1] = command
        return command == "layout plan main keep 1=assist"
    end,
    QuestCommand = function(command) quests[#quests + 1] = command end,
}

dofile("addon/WowAgent/RetailTalents.lua")
dofile("addon/WowAgent/Panel.lua")
local function button(text, x, y)
    for _, frame in ipairs(frames) do
        if frame.kind == "Button" and frame.text == text and (not x or frame.x == x)
            and (not y or frame.y == y) then return frame end
    end
    error("missing button " .. text)
end
local function click(text, x, y)
    local widget = button(text, x, y)
    if not widget.disabled then widget.scripts.OnClick(widget) end
end
local function field(x, y)
    for _, frame in ipairs(frames) do
        if frame.kind == "EditBox" and frame.x == x and frame.y == y then return frame end
    end
    error("missing field")
end

click("WowAgent")
click("配置一键输出")
assert(combat[#combat] == "setup assist")
field(99, -40):SetText("6")
click("配置一键输出")
assert(combat[#combat] == "setup assist 6")
click("生存 · 当前 ▾")
assert(menu["生存 · 当前"] and menu["野兽控制"] and not menu["野兽控制  #253"])
menu["野兽控制"]()
click("切换专精")
assert(combat[#combat] == "spec 253")
click("日常 · 当前 ▾")
assert(menu["地下城"] and not menu["地下城  #20"])
menu["地下城"]()
click("切天赋并配置输出")
assert(combat[#combat] == "setup loadout 20 6")
local main = WowAgentControlPanel
identity.spec, identity.specName = 253, "野兽控制"
main.scripts.OnEvent(main, "PLAYER_SPECIALIZATION_CHANGED", "player")
assert(button("请选择 ▾", 99, -114))
identity.spec, identity.specName = 255, "生存"
main.scripts.OnEvent(main, "PLAYER_SPECIALIZATION_CHANGED", "player")
click("任务与色条")
click("选择 ▾", 352, -42)
menu["若是那豺狼来了  #90882"]()
click("开始追踪")
assert(quests[1] == "track 90882")
click("准备自动接任务")
assert(quests[2] == "prepare 90882")

click("动作栏布局")
assert(button("确认重排").disabled)
click("添加或更新格子")
click("预览布局")
assert(not button("确认重排").disabled)
click("清空主栏", 0, -281)
assert(combat[#combat] == "layout clear" and button("确认重排").disabled)
click("预览布局")
assert(not button("确认重排").disabled)
field(43, -3):SetText("visible")
assert(button("确认重排").disabled)
field(43, -3):SetText("main")
click("预览布局")
click("确认重排")
assert(combat[#combat] == "layout apply" and button("确认重排").disabled)
click("保留当前天赋 ▾")
assert(menu["地下城"] and menu["保留当前天赋"])

click("WowAgent")
guid = "Player-2"
click("WowAgent")
assert(field(99, -40):GetText() == "")
click("WowAgent")
guid = "Player-1"
click("WowAgent")
assert(field(99, -40):GetText() == "6")

click("天赋推荐")
click("单刷 / 地下堡 ▾")
menu["大秘境"]()
local import = button("打开原生导入")
assert(import:IsShown())
identity.level = 89
main.scripts.OnEvent(main, "PLAYER_LEVEL_UP")
assert(not import:IsShown())
identity.level, identity.version = 90, "12.1.7"
main.scripts.OnEvent(main, "PLAYER_LEVEL_UP")
assert(not import:IsShown())
identity.version = "12.1.5"
inCombat = true
click("打开原生导入")
assert(not dialogOpen and toggles == 0)
inCombat = false
click("打开原生导入")
assert(dialogOpen and not main:IsShown() and toggles == 1)
assert(importCode == WowAgent.RetailTalentGuides.specs[255].codes.mythic and importName == "WA 大秘境")
click("WowAgent")
click("战斗准备")
click("清空主栏", 366, -304)
assert(combat[#combat] == "layout clear")
click("恢复备份", 488, -304)
assert(combat[#combat] == "layout restore")
click("任务寻路")
inCombat = true
click("启用游戏互动键")
assert(softTargetInteract == "1")
inCombat = false
click("启用游戏互动键")
assert(softTargetInteract == "3" and button("停用游戏互动键"))
click("停用游戏互动键")
assert(softTargetInteract == "1" and button("启用游戏互动键"))
