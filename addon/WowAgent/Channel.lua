-- WowAgent.Channel: renders the 8-block pixel state channel.
-- Protocol mirror of bot/perception/channel_spec.py (keep both in sync!).
--
-- Frame: 8 blocks of 16px + 2px gaps = 142x16, bottom-left anchored.
--   block1 SYNC=0xA | block2 TYPE | block3..7 payload 20bit | block8 checksum
-- Nibble color: hi2 -> RED level (0/85/170/255), lo2 -> GREEN, blue=0.
-- Tick schedule at 20Hz (tick%20):
--   7,13 FASTEV | 10,17 STATUS | 4,14 BAGSUM | 19 slow-rotation
--   else POS(even)/HDG(odd)

local ADDON = ...

local BLOCKS = 8
local BLOCK = 16
local GAP = 2
local SYNC = 0xA
local CHANNEL_FPS = 20
local SLOWROT_LEN = 18 -- ZONE, GOLD, BAG_1..BAG_16

WowAgent = WowAgent or {}
WowAgent.events = WowAgent.events or {}   -- fastev queue {kind, seq, payload}
WowAgent.seq = WowAgent.seq or 0

-- precomputed nibble colors (0..1 floats for SetColorTexture)
local COLORS = {}
for v = 0, 15 do
    local hi = math.floor(v / 4)
    local lo = v % 4
    COLORS[v + 1] = { hi * 85 / 255, lo * 85 / 255, 0 }
end

local frame = CreateFrame("Frame", "WowAgentChannelFrame", UIParent)
frame:SetSize(142, 16)
frame:SetFrameStrata("TOOLTIP")
frame:SetClampedToScreen(true)

if WowAgentDB and WowAgentDB.point then
    frame:SetPoint(unpack(WowAgentDB.point))
else
    frame:SetPoint("BOTTOMLEFT", UIParent, "BOTTOMLEFT", 40, 40)
end

-- value is 20 bits
local function EncodeFrame(mtype, value)
    local nibs = { SYNC, mtype }
    for shift = 16, 0, -4 do
        nibs[#nibs + 1] = math.floor(value / 2 ^ shift) % 16
    end
    local sum = 0
    for i = 2, 7 do sum = sum + nibs[i] end
    nibs[8] = sum % 16
    return nibs
end

function WowAgent.NewPixelRow(parent, offset)
    local textures = {}
    for i = 1, BLOCKS do
        local t = parent:CreateTexture(nil, "OVERLAY")
        t:SetSize(BLOCK, BLOCK)
        t:SetPoint("TOPLEFT", parent, "TOPLEFT", (i - 1) * (BLOCK + GAP), -offset)
        t:SetColorTexture(1, 0, 1)
        textures[i] = t
    end
    return function(kind, value)
        local nibs = EncodeFrame(kind, value)
        for i = 1, BLOCKS do
            local c = COLORS[nibs[i] + 1]
            textures[i]:SetColorTexture(c[1], c[2], c[3], 1)
        end
    end
end

local WriteRow = WowAgent.NewPixelRow(frame, 0)
local function FastEvKind_NONE() return 0 end

local tick = 0
local elapsed = 0
frame:SetScript("OnUpdate", function(self, dt)
    elapsed = elapsed + dt
    if elapsed < 1 / CHANNEL_FPS then return end
    elapsed = 0
    tick = tick + 1

    local mtype, value
    local t = tick % 20
    if t == 7 or t == 13 then
        mtype, value = WowAgent.BuildFastEv()
    elseif t == 10 or t == 17 then
        mtype, value = WowAgent.BuildStatus()
    elseif t == 4 or t == 14 then
        mtype, value = WowAgent.BuildBagSum()
    elseif t == 19 then
        local idx = math.floor(tick / 20) % SLOWROT_LEN
        mtype, value = WowAgent.BuildSlowRot(idx)
    elseif t % 2 == 0 then
        mtype, value = WowAgent.BuildPos()
    else
        mtype, value = WowAgent.BuildHdg()
    end

    if mtype then
        WriteRow(mtype, value)
    end
end)

-- drag support for placing the channel in a quiet screen corner
function WowAgent.SetMovable(on)
    frame:SetMovable(on)
    frame:EnableMouse(on)
    if on then
        frame:SetScript("OnMouseDown", function(self) self:StartMoving() end)
        frame:SetScript("OnMouseUp", function(self)
            self:StopMovingOrSizing()
            local p = { self:GetPoint() }
            WowAgentDB = WowAgentDB or {}
            WowAgentDB.point = p
            print("|cff00ff00WowAgent: channel position saved.|r")
        end)
    else
        frame:SetScript("OnMouseDown", nil)
        frame:SetScript("OnMouseUp", nil)
    end
end
