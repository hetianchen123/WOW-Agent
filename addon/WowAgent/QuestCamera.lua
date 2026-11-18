-- Holding F8 leases one bounded camera transaction. Key release, including
-- the helper's emergency release, cancels remaining camera work.
local camera = {key = "F8", probeKey = "F6", orbitKey = "F4", epoch = 0, request = 0, result = 0, scene = 1, applied = 0}
WowAgent.QuestCamera = camera
local button, driver, pending, stopNudge
local buttonName = "WowAgentTaskCamera"
local action = ("CLICK %s:LeftButton"):format(buttonName)
local probeAction = ("CLICK %s:RightButton"):format(buttonName)
local orbitAction = ("CLICK %s:MiddleButton"):format(buttonName)
local orbitUntil
local secret = issecretvalue or function() return false end
local report = WowAgent.Report or print
local savedView, defaultView, overheadView = 5, 2, 4
local maxFactor, transactionSeconds, maxZoomSteps = 2.6, 8, 14
local paused, cinematic, observedAt, context = false, false, nil, {}
-- IDs shared with travel_channel.py. Automatic angle changes use native
-- presets only; relative pitch remains a manual, bounded panel action.
-- Distances are scene targets, independent of old short-distance saved views.
local profiles = {
    [1] = {"步行", 39}, [2] = {"地面坐骑", 39}, [3] = {"室内窄道", 24},
    [4] = {"搜索", 39}, [5] = {"交互", 32}, [6] = {"远程", 39},
    [7] = {"近战", 34}, [8] = {"飞行", 39}, [9] = {"进近观察", 32},
    [10] = {"跑尸", 39}, [11] = {"游泳", 0}, [12] = {"游戏接管", 0},
    [13] = {"近处地面观察", 24},
    [14] = {"俯视通路观察", 32},
}
local melee = {[71]=true,[72]=true,[73]=true,[70]=true,[66]=true,[103]=true,[104]=true,
    [250]=true,[251]=true,[252]=true,[255]=true,[259]=true,[260]=true,[261]=true,
    [263]=true,[268]=true,[269]=true,[577]=true,[581]=true}
local ranged = {[62]=true,[63]=true,[64]=true,[102]=true,[253]=true,[254]=true,
    [258]=true,[262]=true,[265]=true,[266]=true,[267]=true,[1467]=true,[1473]=true}
local function Number(value)
    if not secret(value) and type(value) == "number" and value >= 0 and value < math.huge then return value end
end
local function Read(fn, ...)
    if not fn then return nil end
    local ok, value = pcall(fn, ...)
    if ok and not secret(value) then return value end
end
local function CVar(key) return Read(C_CVar and C_CVar.GetCVar or GetCVar, key) end
local function Player()
    local guid = Read(UnitGUID, "player")
    return type(guid) == "string" and guid or nil
end
local function Config()
    local guid = Player()
    return guid and WowAgentDB and WowAgentDB.questCamera and WowAgentDB.questCamera[guid]
end
function camera.zoom() return Number(Read(GetCameraZoom)) end
function camera.savedZoom()
    local config = Config()
    return Number(type(config) == "table" and config.zoom or config)
end
function camera.overheadZoom()
    local guid = Player()
    local config = guid and WowAgentDB and WowAgentDB.questOverheadCamera and WowAgentDB.questOverheadCamera[guid]
    local zoom = Number(type(config) == "table" and config.zoom)
    return zoom and zoom >= 20 and zoom <= 39 and config.view == overheadView
        and config.confirmedByUser == true and zoom or nil
end
function camera.target(scene)
    if scene == 14 then return camera.overheadZoom() or profiles[14][2] end
    return profiles[scene or camera.scene][2]
end
local function LimitVerified()
    local value = tonumber(CVar("cameraDistanceMaxZoomFactor"))
    return value and math.abs(value - maxFactor) < .001
end
local function ConfigureLimit()
    if LimitVerified() then return end
    local previous = tonumber(CVar("cameraDistanceMaxZoomFactor"))
    if previous then
        WowAgentDB = WowAgentDB or {}
        if WowAgentDB.questCameraOriginalMaxFactor == nil then
            WowAgentDB.questCameraOriginalMaxFactor = previous
        end
    end
    local set = C_CVar and C_CVar.SetCVar or SetCVar
    if set then pcall(set, "cameraDistanceMaxZoomFactor", maxFactor) end
    -- A successful SetCVar call alone is not evidence that the limit changed.
    if not LimitVerified() then
        report("WowAgent：镜头距离上限 2.6 未确认；本次按实际缩放反馈处理。")
    end
end
local function Airborne()
    return Read(IsFlying) or Read(IsFalling)
        or (observedAt and GetTime() - observedAt <= .75 and context.flying)
end
local function Casting() return Read(UnitCastingInfo, "player") or Read(UnitChannelInfo, "player") end
local function Exclusive()
    return cinematic or Read(UnitInVehicle, "player") or Read(UnitOnTaxi, "player")
        or Read(UnitIsDead, "player") or Read(IsSwimming)
        or (MovieFrame and Read(MovieFrame.IsShown, MovieFrame))
end
local function GroundSafe()
    return not Exclusive() and not Airborne() and not Read(InCombatLockdown)
        and not Read(UnitIsDeadOrGhost, "player") and not Casting()
end
function camera.orbitReady()
    return Read(GetBindingAction, camera.orbitKey, true) == orbitAction and not paused
end
function camera.orbitActive()
    return orbitUntil and GetTime() < orbitUntil and GroundSafe() and not Read(IsMounted)
        and not paused and observedAt and GetTime()-observedAt <= .75
        and Read(GetUnitSpeed, "player") == 0 and true or false
end
local function CombatScene()
    local index = Number(Read(GetSpecialization))
    local spec = index and Number(Read(GetSpecializationInfo, index))
    return melee[spec] and 7 or ranged[spec] and 6 or nil
end
function camera.observe(value)
    context, observedAt = value, GetTime()
    if Read(IsMouselooking) and not paused and not camera.orbitActive() then
        paused = true
        camera.cancel()
        camera.result = 7
    end
    local scene
    if Exclusive() then scene = Read(IsSwimming) and 11 or 12
    elseif Read(UnitIsGhost, "player") then scene = 10
    elseif value.flying then
        -- Observation envelope only: never a command to pitch the mount.
        local near, slow = camera.scene == 9 and 100 or 80, camera.scene == 9 and 45 or 35
        scene = value.distance and value.distance < near and value.speed and value.speed < slow and 9 or 8
    elseif Read(InCombatLockdown) then scene = CombatScene() or 1
    elseif value.outdoors == false then scene = 3
    elseif Read(IsMounted) then scene = value.flightMount and 8 or 2
    elseif value.distance and value.distance <= (camera.scene == 5 and 26 or 20)
        and (value.phase ~= 2 or value.action == 2 or value.action == 5) then
        scene = 5
    elseif value.phase == 2 and value.action == 1 and value.distance
        and value.distance <= ((camera.scene == 6 or camera.scene == 7) and 48 or 40) then
        scene = CombatScene() or 4
    elseif value.phase == 2 and value.distance and value.distance <= 30 then scene = 4
    else scene = 1 end
    camera.scene = scene
end
local function StopPitch()
    if stopNudge then pcall(stopNudge); stopNudge = nil end
end
local function Finish(result)
    StopPitch()
    if not pending then return end
    pending = nil
    camera.result, camera.epoch = result, 1 - camera.epoch
    if driver then driver:SetScript("OnUpdate", nil) end
end
function camera.cancel() orbitUntil = nil; StopPitch(); Finish(5) end
function WowAgent.ToggleQuestCamera()
    paused = not paused
    camera.cancel()
    camera.result = paused and 7 or 0
    report(paused and "WowAgent：自动镜头已暂停。" or "WowAgent：自动镜头已恢复。")
end
local function ManualTakeover()
    paused = true
    camera.cancel()
    camera.result = 7
end
local function Update()
    local state, now = pending, GetTime()
    if not state then return end
    if paused or Read(IsMouselooking) then ManualTakeover(); return end
    if Exclusive() or Casting() or (state.leased and (not observedAt or now - observedAt > .75))
        or (state.leased and not state.probe and camera.scene ~= state.scene)
        or (state.ground and not GroundSafe()) then Finish(5); return end
    if now >= state.expires then Finish(6); return end
    if now < state.checkAt then return end
    if state.view and tonumber(CVar("cameraView")) ~= state.view then return end
    local zoom = camera.zoom()
    if not zoom then Finish(4); return end
    local error = state.target - zoom
    if math.abs(error) <= 1 then
        if state.stableAt and now - state.stableAt >= .3 and math.abs(zoom - state.stableZoom) <= .5 then
            Finish(LimitVerified() and 2 or 3)
        else state.stableAt, state.stableZoom, state.checkAt = now, zoom, now + .35 end
        return
    end
    state.stableAt = nil
    if state.lastZoom and math.abs(zoom - state.lastZoom) < .25 or state.steps >= maxZoomSteps then Finish(3); return end
    local fn = error > 0 and CameraZoomOut or CameraZoomIn
    if not fn then Finish(4); return end
    state.steps, state.lastZoom, state.checkAt = state.steps + 1, zoom, now + .4
    camera.ownZoom = true
    local ok = pcall(fn, math.min(3, math.abs(error)))
    camera.ownZoom = false
    if not ok then Finish(4) end
end
local function EnsureButton()
    if button then return end
    button = CreateFrame("Button", buttonName, UIParent, "SecureActionButtonTemplate")
    button:RegisterForClicks("LeftButtonDown", "LeftButtonUp", "RightButtonDown", "RightButtonUp", "MiddleButtonDown", "MiddleButtonUp")
    button:SetScript("OnClick", function(_, mouseButton, down)
        if mouseButton == "MiddleButton" then
            if not down then orbitUntil = nil; return end
            if pending or not camera.orbitReady() or not GroundSafe() or Read(IsMounted)
                or Read(IsMouselooking) or Read(GetUnitSpeed, "player") ~= 0
                or camera.applied ~= 14 or tonumber(CVar("cameraView")) ~= overheadView
                or not observedAt or GetTime()-observedAt > .75 then return end
            orbitUntil = GetTime() + .6
            camera.request = (camera.request + 1) % 8
            return
        end
        if down then WowAgent.RestoreQuestCamera(true, mouseButton == "RightButton") else camera.cancel() end
    end)
    driver = CreateFrame("Frame")
    driver:Show()
    driver:SetScript("OnEvent", function(_, event)
        cinematic = event == "CINEMATIC_START"
        camera.cancel()
    end)
    if driver.RegisterEvent then
        for _, event in ipairs({"CINEMATIC_START", "CINEMATIC_STOP", "PLAYER_LEAVING_WORLD"}) do
            driver:RegisterEvent(event)
        end
    end
    if hooksecurefunc then
        for _, name in ipairs({"CameraZoomIn", "CameraZoomOut"}) do
            if _G[name] then hooksecurefunc(name, function()
                if not camera.ownZoom then ManualTakeover() end
            end) end
        end
    end
end
function WowAgent.RestoreQuestCamera(leased, probe)
    leased = leased == true
    probe = probe == true
    if pending or paused or Exclusive() or Casting() or Read(IsMouselooking) then return false end
    if probe and (not GroundSafe() or Read(IsMounted) or not SetView) then return false end
    local now = GetTime()
    if leased and (not observedAt or now - observedAt > .75) then return false end
    EnsureButton()
    local scene = probe and (camera.overheadReady() and 14 or 13) or leased and camera.scene or 1
    if scene == 11 or scene == 12 then return false end
    ConfigureLimit()
    local state = {scene = scene, target = camera.target(scene), checkAt = now + .35,
        expires = now + transactionSeconds, steps = 0, leased = leased, probe = probe, ground = GroundSafe()}
    -- Live 0.2.65 evidence disproved fixed relative pitch as a safe framing rule.
    -- Restore a native preset; Python must verify composition separately.
    -- Ordinary scene transitions only change zoom, preserving the existing angle.
    if GroundSafe() and SetView and (probe or camera.applied == 13 or camera.applied == 14 or camera.applied == 0 or not leased) then
        state.view = scene == 14 and overheadView or camera.savedZoom() and savedView or defaultView
        state.checkAt = now + .65
    end
    pending = state
    camera.request, camera.result, camera.applied = (camera.request + 1) % 8, 1, scene
    if state.view and not pcall(SetView, state.view) then Finish(4); return false end
    driver:SetScript("OnUpdate", Update)
    C_Timer.After(transactionSeconds + .1, function() if pending == state then Finish(6) end end)
    return true
end
function WowAgent.SaveOverheadQuestCamera()
    if pending or not GroundSafe() or Read(IsMounted) or Read(IsMouselooking)
        or (Read(GetUnitSpeed, "player") or 0) > 0 then
        return report("WowAgent：请在安全地面停稳、下马脱战后保存俯视镜头。")
    end
    local zoom, guid = camera.zoom(), Player()
    if not zoom or zoom < 20 or zoom > 39 or not guid or not SaveView then
        return report("WowAgent：请调好能看清角色、障碍两端及目标的俯视镜头，距离需在 20–39。")
    end
    camera.cancel()
    if not pcall(SaveView, overheadView) then return report("WowAgent：客户端未能保存俯视镜头 4。") end
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.questOverheadCamera = WowAgentDB.questOverheadCamera or {}
    WowAgentDB.questOverheadCamera[guid] = {zoom = zoom, view = overheadView, confirmedByUser = true}
    paused, camera.result = false, 0
    report("WowAgent：已保存俯视观察到视角 4；F6 切换观察，F8 恢复任务镜头。图像坐标仍需标定。")
end
function WowAgent.SaveQuestCamera()
    if not GroundSafe() or Read(IsMounted) then
        return report("WowAgent：请在安全地面下马脱战后保存任务视角。")
    end
    local zoom, guid = camera.zoom(), Player()
    if not zoom or zoom < 12 or not guid or not SaveView then
        return report("WowAgent：请先拉远镜头，从角色后方调到约 20°，确认脚下和前路可见。")
    end
    camera.cancel()
    if not pcall(SaveView, savedView) then return report("WowAgent：客户端未能保存视角 5。") end
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.questCamera = WowAgentDB.questCamera or {}
    WowAgentDB.questCamera[guid] = {zoom = zoom, calibrated = true, fov = CVar("cameraFov")}
    paused, camera.result = false, 0
    report("WowAgent：已保存视角 5 作为角度基准；距离按场景自动拉远，实际取景待实测。")
end
function WowAgent.NudgeQuestCamera(down)
    if pending or not GroundSafe() or not C_Timer or not C_Timer.After then return false end
    local start = down and MoveViewDownStart or MoveViewUpStart
    local stop = down and MoveViewDownStop or MoveViewUpStop
    local speed = tonumber(CVar("cameraPitchMoveSpeed"))
    if not start or not stop or not speed or speed <= 0 then return false end
    ManualTakeover()
    local token = function() stop() end
    stopNudge = token
    if not pcall(start, 32 / speed) then StopPitch(); return false end
    C_Timer.After(.18, function() if stopNudge == token then StopPitch() end end)
    return true
end
function camera.ready()
    local binding = Read(GetBindingAction, camera.key, true)
    return type(binding) == "string" and binding:lower() == action:lower()
end
function camera.prepare(owner)
    if not SetOverrideBindingClick or not C_Timer then return end
    EnsureButton()
    pcall(SetOverrideBindingClick, owner, false, camera.key, buttonName, "LeftButton")
    local binding = Read(GetBindingAction, camera.probeKey, true)
    -- The task profile may temporarily borrow an action-bar key; clearing the
    -- owner's overrides restores it. Never borrow movement/flight/control keys.
    local barKey = type(binding) == "string" and (binding:match("^ACTIONBUTTON%d+$")
        or binding:match("^MULTIACTIONBAR%d+BUTTON%d+$"))
    if binding == "" or barKey or type(binding) == "string" and binding:lower() == probeAction:lower() then
        pcall(SetOverrideBindingClick, owner, false, camera.probeKey, buttonName, "RightButton")
    end
    local orbitBinding = Read(GetBindingAction, camera.orbitKey, true)
    local orbitBar = type(orbitBinding) == "string" and (orbitBinding:match("^ACTIONBUTTON%d+$")
        or orbitBinding:match("^MULTIACTIONBAR%d+BUTTON%d+$"))
    if orbitBinding == "" or orbitBar or orbitBinding == orbitAction then
        pcall(SetOverrideBindingClick, owner, false, camera.orbitKey, buttonName, "MiddleButton")
    end
end
function camera.probeReady()
    local binding = Read(GetBindingAction, camera.probeKey, true)
    return type(binding) == "string" and binding:lower() == probeAction:lower() and not paused
end
function camera.overheadReady()
    -- Automatic calibration may inspect native view 4 without manual point or
    -- angle confirmation. Image geometry must pass before any route is moved.
    return camera.probeReady() and SetView ~= nil and (camera.overheadZoom() ~= nil or camera.orbitReady())
end
function camera.probeStatus()
    local binding = Read(GetBindingAction, camera.probeKey, true)
    return camera.overheadReady() and (camera.probeKey .. " 俯视观察可用；" .. camera.orbitKey
        .. (camera.orbitReady() and " 自动标定镜头键已核对" or " 自动标定镜头键不可用"))
        or camera.probeReady() and (camera.probeKey .. " 近场镜头已核对，俯视预设未保存")
        or (camera.probeKey .. " 近场镜头不可用，当前绑定：" .. (type(binding) == "string" and binding or "未知"))
end
function camera.telemetry(sequence)
    local zoom = camera.zoom()
    local encoded = zoom and math.min(127, math.floor(zoom * 2 + .5) + 1) or 0
    local view = tonumber(CVar("cameraView")) or 0
    view = math.max(0, math.min(7, math.floor(view)))
    local config = Config()
    return encoded + camera.result * 128 + view * 1024 + sequence * 8192,
        camera.scene + camera.applied * 16 + camera.request * 256 + (paused and 2048 or 0)
        + (type(config) == "table" and config.calibrated and 4096 or 0) + sequence * 8192
end
function camera.status()
    local results = {[0]="空闲", "调整中", "距离已确认", "缩放受限", "反馈未知", "已中断", "已超时", "已暂停"}
    return ("场景 %s；缩放 %s / 目标 %s；上限倍率 %s；%s%s。角度和遮挡仍需画面确认。"):format(
        profiles[camera.scene][1], tostring(camera.zoom() or "未知"), tostring(camera.target()),
        tostring(CVar("cameraDistanceMaxZoomFactor") or "未知"), results[camera.result], paused and "（暂停）" or "")
end
