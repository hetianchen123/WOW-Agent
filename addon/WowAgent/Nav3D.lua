-- Independent Nav3D schema 1; travel v15 is deliberately unchanged.
-- All rows carry the same sequence; signed XYZ are 24-bit centimetres.
local frame = CreateFrame("Frame", "WowAgentNav3DFrame", UIParent)
frame:SetSize(142, 416)
frame:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 288, 0)
frame:SetFrameStrata("TOOLTIP")
frame:Hide()
local rows = {}
for i = 1, 26 do rows[i] = WowAgent.NewPixelRow(frame, (i - 1) * 16) end
local function Public(v)
    if issecretvalue and issecretvalue(v) then return nil end
    return v
end
local function Number(v, bound)
    v = Public(v)
    return type(v) == "number" and v == v and math.abs(v) < bound and v or nil
end
local function Signed(v) return math.floor(v * 100 + .5) % 16777216 end
local function Pair(out, v)
    out[#out + 1] = v % 4096
    out[#out + 1] = math.floor(v / 4096) % 4096
end
function WowAgent.WriteNav3D(seq, quest, phase, uiMap, targetMap, gx, gy, travelFlags)
    local nx, ny, z, world
    if UnitPosition then nx, ny, z, world = UnitPosition("player") end
    nx, ny, z, world = Number(nx, 83886), Number(ny, 83886), Number(z, 83886), Number(world, 16777216)
    local heading = GetPlayerFacing and Number(GetPlayerFacing(), math.huge)
    local flags = 0
    if nx and ny and world then flags = flags + 1 end
    -- This bit means public numeric data, NOT that Z is physically reliable.
    if z then flags = flags + 2 end
    if heading then flags = flags + 4 end
    local goalWorld, goalX, goalY
    if Number(targetMap, 16777216) and Number(gx, 2) and Number(gy, 2)
        and C_Map.GetWorldPosFromMapPos and CreateVector2D then
        local ok, map, pos = pcall(C_Map.GetWorldPosFromMapPos, targetMap, CreateVector2D(gx, gy))
        map, pos = Public(map), Public(pos)
        if ok and pos then
            local x, y = pos:GetXY()
            x, y, map = Number(x, 83886), Number(y, 83886), Number(map, 16777216)
            if x and y and map then goalWorld, goalX, goalY = map, -y, x; flags = flags + 8 end
        end
    end
    local unsafe = bit.band(travelFlags, 4 + 8 + 16 + 32 + 128)
    if unsafe == 0 and not UnitIsDeadOrGhost("player") then flags = flags + 16 end
    if bit.band(travelFlags, 4096) ~= 0 then flags = flags + 32 end
    if bit.band(travelFlags, 64) ~= 0 then flags = flags + 64 end
    local _, build = GetBuildInfo()
    local values = { 3329, flags }
    Pair(values, world or 0); Pair(values, tonumber(build) or 0); Pair(values, uiMap or 0)
    Pair(values, Signed(ny and -ny or 0)); Pair(values, Signed(nx or 0)); Pair(values, Signed(z or 0))
    values[#values + 1] = heading and math.floor((heading % (2 * math.pi)) / (2 * math.pi) * 4096) or 0
    Pair(values, quest); values[#values + 1] = phase
    Pair(values, goalWorld or 0); Pair(values, Signed(goalX or 0)); Pair(values, Signed(goalY or 0))
    Pair(values, travelFlags)
    for i, value in ipairs(values) do rows[i]((i - 1) % 16, value + seq * 4096) end
    frame:Show()
end

function WowAgent.HideNav3D() frame:Hide() end
