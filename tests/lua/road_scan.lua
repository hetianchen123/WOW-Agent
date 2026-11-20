WowAgent, UIParent, WowAgentChannelFrame = {Forever = {}}, {}, {}
local advance, time, speed, battle = nil, 1, 0, false
local rows = {}
bit = {band = function(a, b)
    local n, place = 0, 1
    while a > 0 and b > 0 do
        n = n + (a % 2) * (b % 2) * place
        a, b, place = math.floor(a / 2), math.floor(b / 2), place * 2
    end
    return n
end}
function GetBuildInfo() return "1.60.1", "70959", "date", 16001 end
function GetRealmName() return "无限服" end
function GetTime() return time end
function GetUnitSpeed() return speed end
function InCombatLockdown() return battle end
function UnitIsDeadOrGhost() return false end
function IsFlying() return false end
function IsFalling() return false end
function IsSwimming() return false end
function SpellIsTargeting() return false end

local methods = {}
function methods:SetSize() end
function methods:SetPoint() end
function methods:SetText() end
function methods:SetShown(value) self.visible = value end
function methods:GetAlpha() return self.alpha or 1 end
function methods:SetAlpha(value) self.alpha = value end
function methods:ClearAllPoints() end
function methods:SetFrameStrata() end
function methods:EnableMouse() end
function methods:SetColorTexture() end
function methods:Show() self.visible = true end
function methods:Hide() self.visible = false end
function methods:IsShown() return self.visible end
function methods:IsVisible() return self.visible end
function methods:GetName() return self.name end
function methods:GetRect() return 100, 50, 800, 520 end
function methods:GetEffectiveScale() return 1 end
function methods:SetScript(event, handler)
    self[event] = handler
    if event == "OnUpdate" then advance = handler end
end
function methods:CreateTexture() return CreateFrame("Texture") end
function CreateFrame(_, name)
    local frame = setmetatable({name = name}, {__index = methods})
    if name then _G[name] = frame end
    return frame
end
function WowAgent.NewPixelRow(_, offset)
    return function(_, value) rows[offset / 16 + 1] = value end
end

local mapID, art = 1429, 303
WorldMapFrame = CreateFrame("Frame")
WorldMapFrame.ScrollContainer = CreateFrame("Frame")
local canvas = CreateFrame("Frame")
canvas:Show()
local loaded, zooms = false, 0
function WorldMapFrame:GetCanvas() return canvas end
function WorldMapFrame:GetMapID() return mapID end
function WorldMapFrame:ResetZoom() zooms = zooms + 1 end
function WorldMapFrame:AreDetailLayersLoaded() return loaded end
function HideUIPanel(frame) frame:Hide() end
C_Map = {
    GetMapArtID = function() return art end,
    GetBestMapForUnit = function() return mapID end,
    GetMapInfo = function() return {mapType = 3} end,
    OpenWorldMap = function(id) assert(id == mapID); WorldMapFrame:Show() end,
}
dofile("addon/WowAgent/Roads.lua")
dofile("addon/WowAgent/RoadScan.lua")
local scan = WowAgent.RoadScan
local strip = CreateFrame("Frame")
strip:SetAlpha(.6)
scan.RegisterStrip(strip)
scan.Write(4, 1429, 1, true)
assert(rows[2] % 4096 == 1 and math.floor(rows[2] / 4096) == 4)
assert(rows[9] % 4096 + (rows[10] % 4096) * 4096 == 70959)
speed = 7
WowAgentRoadScanButton.OnClick()
assert(not WorldMapFrame:IsShown())
speed = 0
WowAgentRoadScanButton.OnClick()
assert(WorldMapFrame:IsShown())
advance()
scan.Write(5, 1429, 1, true)
assert(rows[2] % 4096 == 16 and zooms == 1)
loaded, time = true, 1.4
advance()
assert(strip:GetAlpha() == 0)
time = 1.6
advance()
scan.Write(6, 1429, 1, true)
assert(rows[2] % 4096 == 32)
art = art + 1
advance()
assert(not WorldMapFrame:IsShown())
assert(strip:GetAlpha() == .6)
WowAgentRoadScanButton.OnClick()
assert(not WorldMapFrame:IsShown())
scan.Write(7, 1429, 1, true)
WowAgentRoadScanButton.OnClick()
assert(WorldMapFrame:IsShown())
time = 4
advance()
assert(not WorldMapFrame:IsShown())
scan.Stop()
WowAgentRoadScanButton.OnClick()
assert(not WorldMapFrame:IsShown())
print("road scan checks passed")
