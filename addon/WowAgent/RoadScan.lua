-- Bounded capture of the native world map during prepared Forever travel.
if not WowAgent.Forever then return end

local Scan = {}
WowAgent.RoadScan = Scan
local status = CreateFrame("Frame", nil, UIParent)
status:SetSize(142, 160)
status:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 432, 256)
status:SetFrameStrata("TOOLTIP")
status:Hide()
local button = CreateFrame("Button", "WowAgentRoadScanButton", status, "UIPanelButtonTemplate")
button:SetSize(142, 24)
button:SetPoint("BOTTOMLEFT", status, "TOPLEFT", 0, 2)
button:SetText("拍摄主路地图")
button:Hide()
local rows = {}
for i = 1, 10 do rows[i] = WowAgent.NewPixelRow(status, (i - 1) * 16) end

local corners = CreateFrame("Frame", nil, UIParent)
corners:SetFrameStrata("TOOLTIP")
corners:EnableMouse(false)
corners:Hide()
local anchors = {"TOPLEFT", "TOPRIGHT", "BOTTOMLEFT", "BOTTOMRIGHT"}
local colors = {{1, 0, 1}, {0, 1, 1}, {1, 1, 0}, {0, 0, 1}}
local marks = {}
for i = 1, 4 do
    local marker = corners:CreateTexture(nil, "OVERLAY")
    marker:SetSize(10, 10)
    marker:SetColorTexture(unpack(colors[i]))
    marks[i] = marker
end

local request, current
local strips, savedAlpha = {}, {}
function Scan.RegisterStrip(frame)
    strips[#strips + 1] = frame
end

local function RestoreStrips()
    for frame, alpha in pairs(savedAlpha) do
        frame:SetAlpha(alpha)
        savedAlpha[frame] = nil
    end
end
local function Read(fn, ...)
    if type(fn) ~= "function" then return end
    local ok, value = pcall(fn, ...)
    if ok and not (issecretvalue and issecretvalue(value)) then return value end
end

local function Visible(frame)
    return frame and frame:IsShown()
end

local function Safe(stationary)
    return not (InCombatLockdown() or UnitIsDeadOrGhost("player") or IsFlying() or IsFalling()
        or IsSwimming() or stationary and GetUnitSpeed("player") > 0 or SpellIsTargeting()
        or (GetCurrentKeyBoardFocus and GetCurrentKeyBoardFocus())
        or Visible(WowAgentControlPanel) or Visible(WowAgentRoadEditor)
        or Visible(GossipFrame) or Visible(QuestFrame) or Visible(GameMenuFrame))
end

local function Finish()
    local pending = request
    request = nil
    corners:Hide()
    RestoreStrips()
    if pending and Visible(WorldMapFrame) and HideUIPanel then pcall(HideUIPanel, WorldMapFrame) end
end

local function WholeCanvas(map, canvas)
    local x, y, w, h = canvas:GetRect()
    local left, bottom, width, height = map.ScrollContainer:GetRect()
    if not (x and y and w and h and left and bottom and width and height and w >= 300 and h >= 180) then
        return false
    end
    local scale = canvas:GetEffectiveScale() / map.ScrollContainer:GetEffectiveScale()
    return x * scale >= left - 1 and y * scale >= bottom - 1
        and (x + w) * scale <= left + width + 1 and (y + h) * scale <= bottom + height + 1
end

local function Open()
    if not button:IsVisible() or not current or not current.allowed or request or not Safe(true)
        or Visible(WorldMapFrame) then return end
    if Read(C_Map.GetBestMapForUnit, "player") ~= current.map
        or Read(C_Map.GetMapArtID, current.map) ~= current.art then return end
    if not HideUIPanel then return end
    local open = C_Map.OpenWorldMap or OpenWorldMap
    if not open then return end
    request = {map = current.map, art = current.art, deadline = GetTime() + 2.4}
    button:Hide()
    local ok = pcall(open, request.map)
    if not ok then Finish() end
end

button:SetScript("OnClick", Open)
status:SetScript("OnUpdate", function()
    if not request then return end
    local map = WorldMapFrame
    if GetTime() >= request.deadline or not Safe(true)
        or Read(C_Map.GetBestMapForUnit, "player") ~= request.map
        or Read(C_Map.GetMapArtID, request.map) ~= request.art then
        Finish()
    elseif map and map:IsVisible() and map.GetMapID and map:GetMapID() == request.map
        and map.GetCanvas and map.ScrollContainer and map.AreDetailLayersLoaded then
        if not request.reset then
            if not map.ResetZoom or not pcall(map.ResetZoom, map) then Finish(); return end
            request.reset = GetTime()
        end
        local canvas = map:GetCanvas()
        if canvas and canvas:IsVisible() and map:AreDetailLayersLoaded()
            and GetTime() - request.reset >= .2 and WholeCanvas(map, canvas) then
            if not request.stripsHiddenAt then
                for _, frame in ipairs(strips) do
                    savedAlpha[frame] = frame:GetAlpha()
                    frame:SetAlpha(0)
                end
                request.stripsHiddenAt = GetTime()
                return
            end
            if GetTime() - request.stripsHiddenAt < .10 then return end
            for i, marker in ipairs(marks) do
                marker:ClearAllPoints()
                marker:SetPoint(anchors[i], canvas, anchors[i])
            end
            corners:Show()
        else
            corners:Hide()
            RestoreStrips()
            request.stripsHiddenAt = nil
        end
    else
        corners:Hide()
        RestoreStrips()
        request.stripsHiddenAt = nil
    end
end)

function Scan.Stop()
    current = nil
    Finish()
    status:Hide()
end

function Scan.Write(seq, map, flags, prepared)
    if not C_Map then return Scan.Stop() end
    local profile = WowAgent.Roads.ProfileToken()
    local art = map and map > 0 and Read(C_Map.GetMapArtID, map)
    local _, clientBuild = GetBuildInfo()
    local build = tonumber(clientBuild)
    local info = map and map > 0 and Read(C_Map.GetMapInfo, map)
    if not (profile and type(art) == "number" and art > 0 and build and map < 65536 and art < 65536) then
        Finish()
        current = nil
        status:Hide()
        return
    end
    local allowed = prepared and info and info.mapType == 3 and flags % 2 == 1
        and bit.band(flags, 8 + 16 + 32 + 64 + 128) == 0
    current = {map = map, art = art, allowed = allowed}
    local armed = not request and allowed and not Visible(WorldMapFrame) and Safe()
    button:SetShown(not not armed)
    local state = request and (corners:IsShown() and 2 or 1) or 0
    local values = {0xB38, (armed and 1 or 0) + state * 16,
        map % 4096, math.floor(map / 4096), art % 4096, math.floor(art / 4096),
        profile % 4096, math.floor(profile / 4096), build % 4096, math.floor(build / 4096)}
    for i, value in ipairs(values) do rows[i](i - 1, value + seq * 4096) end
    status:Show()
end
