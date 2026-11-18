-- Lua 5.1: persistence, map boundaries, and editor lifecycle without game input.
WowAgent, WowAgentDB = {}, nil
local realm = "测试服"
WOW_PROJECT_ID = 1
function GetRealmName() return realm end
function GetBuildInfo() return "12.test" end
function time() return 123 end
dofile("addon/WowAgent/Roads.lua")
local roads = WowAgent.Roads
local draft = roads.Draft()
draft.name, draft.from, draft.to = "闪金镇主路", "闪金镇", "西部荒野"
assert(not roads.Save())
assert(not roads.AddPoint(1, 0/0, .5))
assert(not roads.AddPoint(1.5, .2, .3))
assert(not roads.AddPoint(1, 1.1, .3))
assert(roads.AddPoint(1, .2, .3, "map_click", false, 10))
assert(not roads.AddPoint(1, .2, .3))
assert(roads.AddPoint(1, .4, .5, "map_click", false, 10))
assert(not roads.AddPoint(2, .1, .3))
assert(roads.AddPoint(2, .1, .3, "map_click", true, 20))
assert(roads.AddPoint(2, .2, .4, "map_click", false, 20))
local points, segments = roads.VisibleSegments(draft, 1, 10)
assert(#points == 2 and #segments == 1)
points, segments = roads.VisibleSegments(draft, 2, 20)
assert(#points == 2 and #segments == 1 and points[1].point.transition == "boundary")
points, segments = roads.VisibleSegments(draft, 1, 99)
assert(#points == 0 and #segments == 0)
local saved = assert(roads.Save())
assert(saved.id == 1 and saved.evidence == "manual_unverified")
roads.Undo()
assert(#roads.Draft().points == 3 and #saved.points == 4)
assert(roads.Load(saved.id))
assert(#roads.Draft().points == 4)
roads.Draft().bidirectional = false
assert(roads.Save().id == 1 and #roads.Store().routes == 1)
roads.New()
assert(#roads.Draft().points == 0)
assert(roads.RestoreDraft().id == 1)
assert(roads.Delete())
assert(#roads.Store().routes == 0 and #roads.Draft().points == 4)
assert(roads.Save().id == 2)
realm = "另一服"
assert(#roads.Store().routes == 0 and #roads.Draft().points == 0)
realm = "测试服"
assert(#roads.Store().routes == 1)
WOW_PROJECT_ID = 2
assert(#roads.Store().routes == 0)
WOW_PROJECT_ID = 1
-- Simulate addon reload: preserved SavedVariables, new module closures.
dofile("addon/WowAgent/Roads.lua")
roads = WowAgent.Roads
assert(roads.Draft().id == 2 and #roads.Store().routes == 1)
local bad = roads.Draft()
bad.points[3].transition = nil
assert(not roads.Save())
bad.points[3].transition = "boundary"
local previousLimit = roads.MAX_POINTS
roads.MAX_POINTS = 4
assert(not roads.AddPoint(2, .3, .5))
roads.MAX_POINTS = previousLimit
WowAgentDB.roads.schema = 2
assert(not roads.Store())
WowAgentDB.roads.schema = 1
assert(not roads.PlayerPoint())
local playerPosition = {GetXY = function() return .6, .7 end}
C_Map = {
    GetBestMapForUnit = function() return 2 end,
    GetPlayerMapPosition = function() return playerPosition end,
    GetMapArtID = function(map) return map == 1 and 10 or 20 end,
}
assert(roads.PlayerPoint().source == "player_sample")
assert(roads.Draft().evidence == "manual_unverified")
playerPosition = nil
assert(not roads.PlayerPoint())

-- Deliberately strict UI stub: unsupported calls fail instead of becoming noops.
local frames, methods = {}, {}
for _, method in ipairs({"SetWidth", "SetHeight", "SetJustifyH", "SetThickness", "SetColorTexture",
    "SetTextColor", "SetStartPoint", "SetEndPoint", "SetAllPoints", "ClearAllPoints", "SetPoint",
    "SetFrameStrata", "SetClampedToScreen", "SetMovable", "RegisterForDrag", "SetAutoFocus",
    "SetMaxLetters", "ClearFocus", "StartMoving", "StopMovingOrSizing"}) do
    methods[method] = function() end
end
function methods:SetSize(w, h) self.width, self.height = w, h end
function methods:GetSize() return self.width or 1000, self.height or 600 end
function methods:GetFrameLevel() return self.level or 1 end
function methods:SetFrameLevel(level) self.level = level end
function methods:EnableMouse(value) self.mouse = value end
function methods:GetName() return self.name end
function methods:SetScript(event, fn) self.scripts[event] = fn end
function methods:HookScript(event, fn)
    local previous = self.scripts[event]
    self.scripts[event] = function(...) if previous then previous(...) end; fn(...) end
end
function methods:SetText(text)
    self.text = text
    if self.scripts.OnTextChanged then self.scripts.OnTextChanged(self) end
end
function methods:GetText() return self.text end
function methods:SetColorTexture(r, g, b, a) self.color = {r, g, b, a} end
function methods:IsShown() return self.shown end
function methods:IsVisible()
    return self.shown and (not self.parent or not self.parent.IsVisible or self.parent:IsVisible())
end
function methods:GetRect()
    if self.layoutReady then return 20, 30, 700, 500 end
end
function methods:SetShown(value)
    local changed = value ~= self.shown
    self.shown = value
    local event = value and "OnShow" or "OnHide"
    if changed and self.scripts[event] then self.scripts[event](self) end
end
function methods:Show() self:SetShown(true) end
function methods:Hide() self:SetShown(false) end
function methods:CreateFontString() return CreateFrame("FontString", nil, self) end
function methods:CreateLine() return CreateFrame("Line", nil, self) end
function CreateFrame(kind, name, parent, template)
    local frame = setmetatable({kind=kind, name=name, parent=parent, shown=true, scripts={}}, {__index=methods})
    if template == "BasicFrameTemplateWithInset" then frame.TitleText = frame:CreateFontString() end
    if name then _G[name] = frame end
    frames[#frames + 1] = frame
    return frame
end
UIParent, UISpecialFrames, SlashCmdList = {}, {}, {}
local messages = {}
WowAgent.Report = function(text) messages[#messages + 1] = text end
dofile("addon/WowAgent/RoadMap.lua")
WowAgent.ToggleRoadMap()
assert(not WowAgentRoadEditor and #messages == 1) -- missing map API, no exception
WorldMapFrame = CreateFrame("Frame")
WorldMapFrame.ScrollContainer = CreateFrame("Frame", nil, WorldMapFrame)
local canvas = CreateFrame("Frame", nil, WorldMapFrame.ScrollContainer)
local mapID, cursorX, cursorY = 2, .8, .8
function WorldMapFrame:GetCanvas() return canvas end
function WorldMapFrame:GetMapID() return mapID end
function WorldMapFrame:SetMapID(id) mapID = id end
function WorldMapFrame:GetNormalizedCursorPosition() return cursorX, cursorY end
local nativeCalls, nativeBehavior = 0, "ready"
local finishOpening
C_Map.OpenWorldMap = function(id)
    nativeCalls = nativeCalls + 1
    if nativeBehavior == "error" then error("native open rejected") end
    local function finish()
        WorldMapFrame.ScrollContainer.layoutReady = true
        WorldMapFrame:Show()
        mapID = id or 2
    end
    if nativeBehavior == "ready" then finish()
    elseif nativeBehavior == "deferred" then finishOpening = finish end
end
-- Reproduce shown-but-unlaid-out native panels. Frame:Show() alone is
-- intentionally insufficient; the old implementation fails this assertion.
WowAgent.ToggleRoadMap()
assert(nativeCalls == 1 and WorldMapFrame.ScrollContainer.layoutReady)
assert(WowAgentRoadEditor:IsShown() and WorldMapFrame:IsVisible())
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, .2)
local function click(text)
    for _, frame in ipairs(frames) do
        if frame.kind == "Button" and frame.text == text then frame.scripts.OnClick(frame); return end
    end
    error("missing button: " .. text)
end
local capture
for _, frame in ipairs(frames) do if frame.scripts.OnMouseUp then capture = frame end end
assert(capture and not capture.mouse)
click("开始地图标点")
assert(capture.mouse)
local n = #roads.Draft().points
capture.scripts.OnMouseUp(capture, "LeftButton")
assert(#roads.Draft().points == n + 1 and roads.Draft().points[n+1].x == .8)
capture.scripts.OnMouseUp(capture, "RightButton")
assert(#roads.Draft().points == n)
mapID, cursorX, cursorY = 1, .5, .6
capture.scripts.OnMouseUp(capture, "LeftButton")
assert(#roads.Draft().points == n) -- changing viewed maps is not a road connection
click("跨图连接：关闭")
capture.scripts.OnMouseUp(capture, "LeftButton")
assert(#roads.Draft().points == n + 1 and roads.Draft().points[n+1].transition == "boundary")
click("步行")
assert(roads.Draft().mode == "walk")
click("保存路线")
assert(roads.Store().routes[1].mode == "walk")
click("新建")
assert(not capture.mouse and #roads.Draft().points == 0)
click("恢复草稿")
assert(#roads.Draft().points == n + 1)
click("开始地图标点")
WorldMapFrame:Hide()
assert(not capture.mouse)
WorldMapFrame:Show()
click("开始地图标点")
WowAgent.ToggleRoadMap()
assert(not capture.mouse and not WowAgentRoadEditor:IsShown())
WowAgent.ToggleRoadMap()
assert(not capture.mouse and #roads.Draft().points == n + 1)
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, .2)
local visibleLines = 0
for _, frame in ipairs(frames) do
    if frame.kind == "Line" and frame:IsShown() then visibleLines = visibleLines + 1 end
end
assert(visibleLines == 1) -- map 1 has first segment; cross-map return is disconnected
click("上一条")
assert(roads.Draft().id == 2 and mapID == 1)
-- Every mode survives Save/Load and a module reload, with distinct map colors
-- and text markers. Retained ground keys continue to mean riding.
for _, mode in ipairs({"ground", "fly", "walk"}) do
    local style = roads.MODES[mode]
    click(style.label)
    assert(WowAgentRoadEditor.modeButtons[mode]:GetText() == "[" .. style.label .. "]")
    click("保存路线")
    assert(roads.Store().routes[1].mode == mode)
    roads.New()
    assert(roads.Load(2).mode == mode)
    dofile("addon/WowAgent/Roads.lua")
    assert(WowAgent.Roads.Draft().mode == mode)
    local markerFound = false
    for _, frame in ipairs(frames) do
        if frame.kind == "Line" and frame:IsShown() then
            for i = 1, 3 do assert(frame.color[i] == style.color[i]) end
        elseif frame.kind == "FontString" and frame:IsShown() and frame.text == "●" .. style.marker .. "1" then
            markerFound = true
        end
    end
    assert(markerFound)
end
roads.Draft().mode = "teleport"
assert(not roads.Save())
roads.Draft().mode = "walk"

-- Every UI entry uses the same opener; a visible map is never toggled closed.
local calls = nativeCalls
click("打开世界地图")
assert(nativeCalls == calls and WorldMapFrame:IsVisible())
WorldMapFrame:Hide()
click("打开世界地图")
assert(nativeCalls == calls + 1 and WorldMapFrame:IsVisible())
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, .2)
WorldMapFrame:Hide()
click("显示末点地图")
assert(mapID == roads.Draft().points[#roads.Draft().points].mapID)
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, .2)

-- Start marking opens a closed map and waits for its real geometry.
WorldMapFrame:Hide()
WorldMapFrame.ScrollContainer.layoutReady = false
nativeBehavior = "deferred"
click("开始地图标点")
assert(not capture.mouse)
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, .2)
assert(not capture.mouse)
finishOpening()
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, .2)
assert(capture.mouse)
local beforePoint = #roads.Draft().points
cursorX, cursorY = .9, .9
click("跨图连接：关闭")
capture.scripts.OnMouseUp(capture, "LeftButton")
assert(#roads.Draft().points == beforePoint + 1)

-- No-op native call is not success; show a bounded error and never capture.
WorldMapFrame:Hide()
nativeBehavior = "noop"
click("开始地图标点")
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, 2.1)
assert(not capture.mouse and messages[#messages]:find("地图未显示"))
assert(WowAgentRoadEditor.status:GetText():find("地图未显示"))
nativeBehavior = "error"
click("打开世界地图")
assert(messages[#messages]:find("native open rejected") and not capture.mouse)

-- Older clients use their native panel manager, never direct Frame:Show().
C_Map.OpenWorldMap = nil
local panelCalls, inCombat = 0, false
function InCombatLockdown() return inCombat end
function ShowUIPanel(frame)
    assert(frame == WorldMapFrame)
    panelCalls = panelCalls + 1
    frame.ScrollContainer.layoutReady = true
    frame:Show()
end
inCombat = true
click("打开世界地图")
assert(panelCalls == 0 and messages[#messages]:find("战斗中"))
inCombat = false
click("显示末点地图")
assert(panelCalls == 1 and WorldMapFrame:IsVisible())
WowAgentRoadEditor.scripts.OnUpdate(WowAgentRoadEditor, .2)
assert(mapID == roads.Draft().points[#roads.Draft().points].mapID)
