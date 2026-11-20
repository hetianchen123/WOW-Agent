-- Independent Nav3D schema 1; travel v15 is deliberately unchanged.
-- All rows carry the same sequence; signed XYZ are 24-bit centimetres.
local frame = CreateFrame("Frame", "WowAgentNav3DFrame", UIParent)
frame:SetSize(142, 416)
frame:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 288, WowAgent.Forever and 256 or 0)
frame:SetFrameStrata("TOOLTIP")
if WowAgent.RoadScan then WowAgent.RoadScan.RegisterStrip(frame) end
frame:Hide()
local rows = {}
for i = 1, 26 do rows[i] = WowAgent.NewPixelRow(frame, (i - 1) * 16) end
local supportFrame = CreateFrame("Frame", "WowAgentMeshSupportFrame", frame)
supportFrame:SetSize(142, 96)
supportFrame:SetPoint("TOPLEFT", frame, "BOTTOMLEFT", 0, 0)
local supportRows = {}
for i = 1, 6 do supportRows[i] = WowAgent.NewPixelRow(supportFrame, (i - 1) * 16) end
local poseFrame = CreateFrame("Frame", "WowAgentFastPoseFrame", UIParent)
poseFrame:SetSize(142, 176)
poseFrame:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 0, 480)
poseFrame:SetFrameStrata("TOOLTIP")
if WowAgent.RoadScan then WowAgent.RoadScan.RegisterStrip(poseFrame) end
poseFrame:Hide()
local poseRows = {}
for i = 1, 11 do poseRows[i] = WowAgent.NewPixelRow(poseFrame, (i - 1) * 16) end
local poseSequence, poseElapsed, poseBase, verifyMotion = 0, 0
local poseInterval = 1 / 15
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
local function Motion()
    local nx, ny, z, world
    if UnitPosition then nx, ny, z, world = UnitPosition("player") end
    nx, ny, z, world = Number(nx, 83886), Number(ny, 83886), Number(z, 83886), Number(world, 16777216)
    local heading = GetPlayerFacing and Number(GetPlayerFacing(), math.huge)
    return nx, ny, z, world, heading
end

local supportSession, supportMotion, supportX, supportY, supportUnsafe, supportAt
local supportMonitor = CreateFrame("Frame", "WowAgentMeshSupportMonitor")
local function ResetSupport()
    local clock = GetTimePreciseSec or GetTime
    supportSession = math.floor(clock() * 1000000) % 68719476735 + 1
    supportMotion, supportX, supportY, supportUnsafe, supportAt = 0, nil, nil, nil, nil
end
local function TrackSupport(nx, ny, now)
    local unsafe = not nx or not ny or IsFalling() or IsFlying() or IsSwimming()
        or Public(UnitIsDeadOrGhost("player")) ~= false
    if supportUnsafe ~= unsafe or (supportX and nx and ny
        and ((nx - supportX)^2 + (ny - supportY)^2 > .15^2)) or (supportAt and now - supportAt > .5) then
        supportMotion = (supportMotion + 1) % 16777216
        supportX, supportY, supportUnsafe = nx, ny, unsafe
    end
    supportAt = now
end
if WowAgent.Forever then
    ResetSupport()
    supportMonitor:RegisterEvent("PLAYER_ENTERING_WORLD")
    supportMonitor:RegisterEvent("PLAYER_LOGOUT")
    supportMonitor:SetScript("OnEvent", function(_, event, _, reloading)
        local nx, ny, _, world = Motion()
        local db = WowAgentDB
        if event == "PLAYER_LOGOUT" then
            if not db then return end
            TrackSupport(nx, ny, GetTime())
            db.meshSupportReload = not supportUnsafe and world and
                {session=supportSession, revision=supportMotion, x=nx, y=ny, world=world, at=time()} or nil
            return
        end
        ResetSupport()
        local saved = db and db.meshSupportReload
        if db then db.meshSupportReload = nil end
        if reloading and type(saved) == "table" and type(saved.at) == "number"
            and 0 <= time()-saved.at and time()-saved.at <= 60
            and Number(saved.session, 68719476736) and Number(saved.revision, 16777216)
            and Number(saved.x, 83886) and Number(saved.y, 83886)
            and world and world == saved.world and nx and ny
            and ((nx-saved.x)^2 + (ny-saved.y)^2 <= .15^2)
            and not (IsFalling() or IsFlying() or IsSwimming())
            and Public(UnitIsDeadOrGhost("player")) == false then
            supportSession, supportMotion = saved.session, saved.revision
            supportX, supportY, supportUnsafe, supportAt = nx, ny, false, GetTime()
        end
    end)
    supportMonitor:SetScript("OnUpdate", function()
        local now = GetTime()
        if not supportAt or now - supportAt >= .067 then
            local nx, ny = Motion()
            TrackSupport(nx, ny, now)
        end
    end)
else
    supportFrame:Hide()
end

local function WritePose(nx, ny, world, heading, uiMap)
    local combat = UnitAffectingCombat and Public(UnitAffectingCombat("player"))
    local grounded = (Public(UnitIsDeadOrGhost("player")) == false and not IsMounted()
        and not IsFlying() and not IsFalling() and not IsSwimming()
        and not (UnitCastingInfo and Public(UnitCastingInfo("player")))
        and not (UnitChannelInfo and Public(UnitChannelInfo("player")))
        and not (GetCurrentKeyBoardFocus and GetCurrentKeyBoardFocus())
        and not (WorldMapFrame and WorldMapFrame:IsShown()) and not (GameMenuFrame and GameMenuFrame:IsShown()))
    local position = nx and ny and world and uiMap
    local flags = (grounded and 128 or 0) + (verifyMotion() and 256 or 0)
        + (combat ~= false and 512 or 0) + (position and 1024 or 0) + (heading and 2048 or 0)
    local values = { 3361, poseBase + flags }
    Pair(values, world or 0); Pair(values, uiMap or 0)
    Pair(values, Signed(ny and -ny or 0)); Pair(values, Signed(nx or 0))
    values[#values + 1] = heading and math.floor((heading % (2 * math.pi)) / (2 * math.pi) * 4096) or 0
    poseSequence = (poseSequence + 1) % 256
    for i, value in ipairs(values) do poseRows[i](i - 1, value + poseSequence * 4096) end
end

poseFrame:SetScript("OnUpdate", function(_, dt)
    poseElapsed = poseElapsed + dt
    if not verifyMotion or poseElapsed < poseInterval then return end
    poseElapsed = poseElapsed % poseInterval
    local nx, ny, _, world, heading = Motion()
    WritePose(nx, ny, world, heading, Number(C_Map.GetBestMapForUnit("player"), 16777216))
end)

function WowAgent.WriteNav3D(seq, quest, phase, uiMap, targetMap, gx, gy, travelFlags, encounterFlags, motionCheck)
    local nx, ny, z, world, heading = Motion()
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
    local values = { 3329, flags + (encounterFlags or 0) }
    Pair(values, world or 0); Pair(values, tonumber(build) or 0); Pair(values, uiMap or 0)
    Pair(values, Signed(ny and -ny or 0)); Pair(values, Signed(nx or 0)); Pair(values, Signed(z or 0))
    values[#values + 1] = heading and math.floor((heading % (2 * math.pi)) / (2 * math.pi) * 4096) or 0
    Pair(values, quest); values[#values + 1] = phase
    Pair(values, goalWorld or 0); Pair(values, Signed(goalX or 0)); Pair(values, Signed(goalY or 0))
    Pair(values, travelFlags)
    for i, value in ipairs(values) do rows[i]((i - 1) % 16, value + seq * 4096) end
    if supportSession then
        TrackSupport(nx, ny, GetTime())
        local support = {3377}
        Pair(support, supportSession % 16777216)
        support[4] = math.floor(supportSession / 16777216)
        Pair(support, supportMotion)
        for i, value in ipairs(support) do supportRows[i](i - 1, value + seq * 4096) end
    end
    frame:Show()
    if motionCheck then
        verifyMotion, poseBase, poseElapsed = motionCheck, seq, 0
        WritePose(nx, ny, world, heading, Number(uiMap, 16777216))
        poseFrame:Show()
    elseif verifyMotion then
        verifyMotion = nil
        poseFrame:Hide()
    end
end

function WowAgent.HideNav3D()
    verifyMotion = nil
    frame:Hide()
    poseFrame:Hide()
end
