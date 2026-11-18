-- World-map road editor. Optional APIs are checked at entry for older clients.
local Roads = WowAgent.Roads
local editor, layer, capture, attachedMap
local labels, lines = {}, {}
local editing, crossing, loading = false, false, false
local Refresh, SyncFields, OpenMap
local pendingOpen, mapMessage

local function MapReady(mapID)
    local map = attachedMap
    if not map or not map:IsVisible() or not map.ScrollContainer:IsVisible() then return false end
    local x, y, w, h = map.ScrollContainer:GetRect()
    local cw, ch = map:GetCanvas():GetSize()
    local current = map:GetMapID()
    return x ~= nil and y ~= nil and w and h and w > 0 and h > 0
        and cw and ch and cw > 0 and ch > 0 and type(current) == "number" and current > 0
        and (not mapID or current == mapID)
end

local function Say(message)
    local output = WowAgent.Report or print
    output(message)
end

local function Report(ok, err)
    if not ok then Say("WowAgent：" .. (err or "操作失败。")) end
    if Refresh then Refresh() end
    return ok
end

local function Label(parent, text, x, y, width)
    local label = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    label:SetPoint("TOPLEFT", x, -y)
    label:SetWidth(width or 300)
    label:SetJustifyH("LEFT")
    label:SetText(text)
    return label
end

local function Button(text, x, y, width, action)
    local button = CreateFrame("Button", nil, editor, "UIPanelButtonTemplate")
    button:SetPoint("TOPLEFT", x, -y)
    button:SetSize(width, 25)
    button:SetText(text)
    button:SetScript("OnClick", action)
    return button
end

local function SetEditing(value)
    if value and not MapReady() then OpenMap(nil, true); return end
    editing = value
    if capture then capture:EnableMouse(editing and editor:IsShown()) end
    if editor then editor.editButton:SetText(editing and "结束标点 / 浏览地图" or "开始地图标点") end
end

-- WorldMapFrame is managed by the native panel system. Calling :Show()
-- directly can leave it shown without panel positioning / usable map bounds.
OpenMap = function(mapID, startEditing)
    SetEditing(false)
    pendingOpen = nil
    if MapReady(mapID) then
        mapMessage = nil
        if startEditing then SetEditing(true) end
        if Refresh then Refresh() end
        return true
    end
    local ok, err
    if C_Map and C_Map.OpenWorldMap then
        ok, err = pcall(C_Map.OpenWorldMap, mapID)
    elseif InCombatLockdown and InCombatLockdown() then
        ok, err = false, "战斗中请先用游戏地图键打开地图，或脱战后重试。"
    elseif OpenWorldMap then
        ok, err = pcall(OpenWorldMap, mapID)
    elseif ShowUIPanel then
        ok, err = pcall(function()
            ShowUIPanel(attachedMap)
            if mapID then attachedMap:SetMapID(mapID) end
        end)
    else
        ok, err = false, "当前客户端没有原生地图打开接口。"
    end
    if not ok then
        mapMessage = "地图打开失败：" .. tostring(err)
        return Report(nil, mapMessage)
    end
    -- Native opening can finish layout on a subsequent frame. A successful
    -- call alone is not confirmation, and no clicks are captured until ready.
    pendingOpen = {mapID = mapID, edit = startEditing, elapsed = 0}
    mapMessage = "正在打开世界地图…"
    if Refresh then Refresh() end
    return true
end

local function CheckMapOpen(dt)
    local request = pendingOpen
    if not request then return end
    request.elapsed = request.elapsed + dt
    if MapReady(request.mapID) then
        pendingOpen, mapMessage = nil, nil
        if request.edit then SetEditing(true) end
        if Refresh then Refresh() end
    elseif request.elapsed >= 2 then
        pendingOpen = nil
        mapMessage = "地图未显示，请用游戏地图键打开后重试。"
        Report(nil, mapMessage)
    end
end

local function Changed(ok, err)
    if ok then crossing = false end
    Report(ok, err)
end

local function Attach(map)
    if attachedMap and attachedMap == map then return true end
    if not (map and map.GetCanvas and map.GetMapID and map.GetNormalizedCursorPosition
        and map.ScrollContainer) then return nil, "当前客户端没有兼容的世界地图画布。" end
    local canvas = map:GetCanvas()
    if not canvas or not canvas.CreateLine then return nil, "当前客户端不支持地图路线绘制。" end
    attachedMap = map
    layer = CreateFrame("Frame", nil, canvas)
    layer:SetAllPoints(canvas)
    layer:SetFrameLevel(canvas:GetFrameLevel() + 30)
    layer:EnableMouse(false)
    -- Separate input surface deliberately consumes clicks only in edit mode.
    -- Browsing restores normal map dragging, zooming and native pin clicks.
    capture = CreateFrame("Frame", nil, map.ScrollContainer)
    capture:SetAllPoints(map.ScrollContainer)
    capture:SetFrameLevel(layer:GetFrameLevel() + 40)
    capture:EnableMouse(false)
    capture:SetScript("OnMouseUp", function(_, button)
        if not editing or not editor:IsShown() or not MapReady() then return end
        if button == "RightButton" then Roads.Undo(); Refresh(); return end
        if button ~= "LeftButton" then return end
        local x, y = map:GetNormalizedCursorPosition()
        local mapID = map:GetMapID()
        Changed(Roads.AddPoint(mapID, x, y, "map_click", crossing,
            C_Map and C_Map.GetMapArtID and C_Map.GetMapArtID(mapID)))
    end)
    map:HookScript("OnHide", function() SetEditing(false) end)
    return true
end

local function Render()
    if not layer then return end
    for _, label in ipairs(labels) do label:Hide() end
    for _, line in ipairs(lines) do line:Hide() end
    layer:SetShown(editor:IsShown())
    if not editor:IsShown() or not MapReady() then return end
    local draft = Roads.Draft()
    if not draft then return end
    local mode = Roads.MODES[draft.mode]
    if not mode then return end
    local r, g, b = unpack(mode.color)
    local mapID = attachedMap:GetMapID()
    local artID = C_Map and C_Map.GetMapArtID and C_Map.GetMapArtID(mapID)
    local points, segments = Roads.VisibleSegments(draft, mapID, artID)
    local w, h = attachedMap:GetCanvas():GetSize()
    for i, pair in ipairs(segments) do
        local line = lines[i]
        if not line then line = layer:CreateLine(nil, "OVERLAY"); lines[i] = line end
        line:SetThickness(3)
        line:SetColorTexture(r, g, b, .9)
        line:SetStartPoint("TOPLEFT", layer, pair[1].x * w, -pair[1].y * h)
        line:SetEndPoint("TOPLEFT", layer, pair[2].x * w, -pair[2].y * h)
        line:Show()
    end
    for i, entry in ipairs(points) do
        local label = labels[i]
        if not label then
            label = layer:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
            labels[i] = label
        end
        label:ClearAllPoints()
        label:SetPoint("CENTER", layer, "TOPLEFT", entry.point.x * w, -entry.point.y * h)
        label:SetText((entry.point.transition and "◆" or "●") .. mode.marker .. entry.index)
        label:SetTextColor(r, g, b)
        label:Show()
    end
end

local function Build()
    editor = CreateFrame("Frame", "WowAgentRoadEditor", UIParent, "BasicFrameTemplateWithInset")
    editor:SetSize(340, 560)
    editor:SetPoint("RIGHT", UIParent, "RIGHT", -25, 0)
    editor:SetFrameStrata("DIALOG")
    editor:SetClampedToScreen(true)
    editor:SetMovable(true)
    editor:EnableMouse(true)
    editor:RegisterForDrag("LeftButton")
    editor:SetScript("OnDragStart", editor.StartMoving)
    editor:SetScript("OnDragStop", editor.StopMovingOrSizing)
    editor.TitleText:SetText("WowAgent · 路线标注")
    if UISpecialFrames then UISpecialFrames[#UISpecialFrames + 1] = editor:GetName() end
    Label(editor, "沿主路依次标点；转弯、桥头、隘口多加点。\n标点模式：左键添加，右键撤销。", 16, 36)
    editor.fields = {}
    for i, field in ipairs({{"name", "路线"}, {"from", "起点区域"}, {"to", "终点区域"}}) do
        local key = field[1]
        Label(editor, field[2], 16, 83 + (i - 1) * 32, 75)
        local box = CreateFrame("EditBox", nil, editor, "InputBoxTemplate")
        box:SetPoint("TOPLEFT", 100, -(77 + (i - 1) * 32))
        box:SetSize(215, 25)
        box:SetAutoFocus(false)
        box:SetMaxLetters(60)
        box:SetScript("OnEscapePressed", box.ClearFocus)
        box:SetScript("OnEnterPressed", box.ClearFocus)
        box:SetScript("OnTextChanged", function(self)
            if not loading then
                local draft = Roads.Draft()
                if draft then draft[key] = self:GetText() end
            end
        end)
        editor.fields[key] = box
    end
    editor.modeButtons = {}
    for i, value in ipairs({"walk", "ground", "fly"}) do
        local mode = value
        editor.modeButtons[mode] = Button("", 16 + (i - 1) * 68, 178, 62, function()
            local draft = Roads.Draft()
            draft.mode = mode
            Refresh()
        end)
    end
    editor.directionButton = Button("", 220, 178, 104, function()
        local draft = Roads.Draft()
        draft.bidirectional = not draft.bidirectional
        Refresh()
    end)
    editor.editButton = Button("开始地图标点", 16, 211, 190, function() SetEditing(not editing) end)
    Button("撤销末点", 214, 211, 110, function() Roads.Undo(); Refresh() end)
    Button("添加脚下位置", 16, 244, 150, function() Changed(Roads.PlayerPoint(crossing)) end)
    editor.crossButton = Button("", 174, 244, 150, function() crossing = not crossing; Refresh() end)
    Label(editor, "跨图：先标出口 → 结束标点 → 切换地图\n→ 开启跨图连接 → 在相邻地图标入口。", 16, 278)
    Button("保存路线", 16, 320, 96, function()
        local route, err = Roads.Save()
        if Report(route, err) then
            SyncFields()
            Say("WowAgent：路线已保存；/reload 或正常退出后写入磁盘。")
        end
    end)
    Button("新建", 120, 320, 96, function()
        Roads.New(); crossing = false; SetEditing(false); SyncFields(); Refresh()
    end)
    Button("恢复草稿", 224, 320, 100, function()
        Report(Roads.RestoreDraft()); crossing = false; SetEditing(false); SyncFields()
    end)
    Button("上一条", 16, 353, 96, function() editor:Cycle(-1) end)
    Button("下一条", 120, 353, 96, function() editor:Cycle(1) end)
    Button("删除已存路线", 224, 353, 100, function()
        if Report(Roads.Delete()) then
            Say("WowAgent：已移出路线库，当前草稿保留；点保存可恢复。")
        end
    end)
    editor.status = Label(editor, "", 16, 392)
    editor.status:SetHeight(65)
    Label(editor, "绿色：步行 · 黄色：骑马 · 蓝色：飞行\n标记适用于整条路线，通行尚未验证。\n当前编辑器不驱动角色移动。", 16, 464)
    Button("显示末点地图", 16, 523, 150, function()
        local draft = Roads.Draft()
        local point = draft.points[#draft.points]
        if point then
            OpenMap(point.mapID)
        else
            Report(nil, "尚未标点，请先打开世界地图。")
        end
    end)
    Button("打开世界地图", 174, 523, 150, function() OpenMap() end)
    function editor:Cycle(delta)
        local store = Roads.Store()
        if #store.routes == 0 then return Report(nil, "还没有已保存的路线。") end
        local index = delta > 0 and 0 or 1
        for i, route in ipairs(store.routes) do if route.id == Roads.Draft().id then index = i; break end end
        index = (index - 1 + delta) % #store.routes + 1
        Report(Roads.Load(store.routes[index].id))
        crossing = false
        SetEditing(false)
        SyncFields()
        local point = Roads.Draft().points[1]
        if point then OpenMap(point.mapID) end
        Refresh()
    end
    SyncFields = function()
        loading = true
        local draft = Roads.Draft()
        for key, box in pairs(editor.fields) do box:SetText(draft[key]); box:ClearFocus() end
        loading = false
    end
    Refresh = function()
        local draft, store = Roads.Draft(), Roads.Store()
        if not draft then return end
        for mode, button in pairs(editor.modeButtons) do
            local label = Roads.MODES[mode].label
            button:SetText(draft.mode == mode and ("[" .. label .. "]") or label)
        end
        editor.directionButton:SetText(draft.bidirectional and "方向：双向" or "起点 → 终点")
        editor.crossButton:SetText(crossing and "跨图连接：已开启" or "跨图连接：关闭")
        local point = draft.points[#draft.points]
        editor.status:SetText(string.format("当前：%s · %d 个路点 · 已存 %d 条\n%s\n%s",
            draft.id and ("#" .. draft.id) or "新草稿", #draft.points, #store.routes,
            point and string.format("末点：地图 %d · %.2f, %.2f", point.mapID, point.x * 100, point.y * 100)
                or "尚未标点", mapMessage or "草稿自动保留；切换前草稿可恢复。"))
        Render()
    end
    local elapsed = 0
    editor:SetScript("OnUpdate", function(_, dt)
        CheckMapOpen(dt)
        elapsed = elapsed + dt
        if elapsed >= .15 then elapsed = 0; Render() end
    end)
    editor:SetScript("OnHide", function()
        pendingOpen = nil
        SetEditing(false)
        if layer then layer:Hide() end
        for _, box in pairs(editor.fields) do box:ClearFocus() end
    end)
    editor:SetScript("OnShow", function() SyncFields(); Refresh() end)
    editor:Hide()
end

function WowAgent.ToggleRoadMap()
    if editor and editor:IsShown() then editor:Hide(); return end
    local store, err = Roads.Store()
    if not store then return Report(nil, err) end
    if not WorldMapFrame then
        local load = C_AddOns and C_AddOns.LoadAddOn or LoadAddOn
        if load then pcall(load, "Blizzard_WorldMap") end
    end
    if not Report(Attach(WorldMapFrame)) then return end
    if not editor then Build() end
    editor:Show()
    OpenMap()
end

SLASH_WOWAGENTROADS1 = "/waroad"
SlashCmdList.WOWAGENTROADS = WowAgent.ToggleRoadMap
