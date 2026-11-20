-- Camera outcomes must describe observed distance, never an assumed angle.
local now, zoom, view, requestedView = 0, 6, 3, nil
local timers, binding = {}, ""
local button, driver
local combat, flying, mounted, ghost, mouse, indoors, swimming = false, false, false, false, false, false, false
local wall, missingZoom, refuseView, special = false, false, false, false
local zoomCalls, pitchStarts, pitchStops, viewCalls = 0, 0, 0, 0
local maxFactor, factorWrites, refuseFactor, missingFactor = 1.9, 0, false, false
local spec = 253
WowAgent = {Report = function() end}
function GetTime() return now end
function UnitGUID() return "Player-1" end
function IsFlying() return flying end
function IsMounted() return mounted end
function IsSwimming() return swimming end
function UnitIsGhost() return ghost end
function UnitIsDeadOrGhost() return ghost end
function UnitInVehicle() return special end
function InCombatLockdown() return combat end
function IsMouselooking() return mouse end
function GetSpecialization() return 1 end
function GetSpecializationInfo() return spec end
function GetCameraZoom() if not missingZoom then return zoom end end
function SetView(slot)
    viewCalls, requestedView = viewCalls + 1, slot
    if not refuseView then view = slot end
end
local savedSlots = {}
function SaveView(slot) assert(slot == 5 or slot == 4); savedSlots[#savedSlots+1] = slot end
function CameraZoomOut(delta)
    zoomCalls = zoomCalls + 1
    assert(delta <= 3)
    if not wall then zoom = math.min(maxFactor * 15, zoom + delta) end
end
function CameraZoomIn(delta)
    zoomCalls = zoomCalls + 1
    if not wall then zoom = zoom - delta end
end
function MoveViewDownStart(speed) assert(speed > 0); pitchStarts = pitchStarts + 1 end
function MoveViewDownStop() pitchStops = pitchStops + 1 end
function MoveViewUpStart(speed) assert(speed > 0); pitchStarts = pitchStarts + 1 end
function MoveViewUpStop() pitchStops = pitchStops + 1 end
C_CVar = {GetCVar = function(key)
    if key == "cameraDistanceMaxZoomFactor" then return not missingFactor and tostring(maxFactor) or nil end
    return ({cameraView=tostring(view), cameraPitchMoveSpeed="90", cameraFov="90"})[key]
end, SetCVar = function(key, value)
    assert(key == "cameraDistanceMaxZoomFactor" and value == 2.6)
    factorWrites = factorWrites + 1
    if not refuseFactor then maxFactor = value end
end}
C_Timer = {After = function(delay, fn) timers[#timers + 1] = {now + delay, fn} end}
function CreateFrame(_, name)
    local f = {RegisterForClicks=function() end, Show=function() end, RegisterEvent=function() end}
    function f:SetScript(event, fn) self[event] = fn end
    if name then button = f elseif not driver then driver = f end
    return f
end
function GetBindingAction() return binding end
function ClearOverrideBindings() end
function SetOverrideBindingClick(_, _, key, target, mouseButton)
    assert(key == "F8" and mouseButton == "LeftButton")
    binding = "CLICK " .. target .. ":LeftButton"
end
dofile("addon/WowAgent/TaskBindings.lua")
dofile("addon/WowAgent/QuestCamera.lua")
local camera = WowAgent.QuestCamera
camera.prepare({})
assert(camera.ready())
local observation = {phase=2, action=3, outdoors=true, distance=200}
local function observe()
    observation.flying = flying
    observation.outdoors = not indoors
    camera.observe(observation)
end
local function step(dt, fresh)
    now = now + dt
    if fresh ~= false then observe() end
    local old = timers
    timers = {}
    for _, t in ipairs(old) do
        if t[1] <= now then t[2]() else timers[#timers + 1] = t end
    end
    if driver.OnUpdate then driver.OnUpdate() end
end
local function press()
    observe()
    button.OnClick(button, "LeftButton", true)
end
local function release() button.OnClick(button, "LeftButton", false) end
local function finish()
    for i=1,85 do if camera.result ~= 1 then break end; step(.1) end
end

-- Fallback and old saved slots both need actual distance convergence.
press(); assert(requestedView == 2 and camera.result == 1)
finish(); assert(camera.result == 2 and math.abs(zoom - 39) <= 1 and pitchStarts == 0)
assert(maxFactor == 2.6 and factorWrites == 1 and WowAgentDB.questCameraOriginalMaxFactor == 1.9)
release(); assert(camera.result == 2)
WowAgentDB = {questCamera = {["Player-1"] = 20}}
zoom, camera.applied = 7, 0
press(); assert(requestedView == 5)
finish(); assert(camera.result == 2 and zoom >= 38 and pitchStarts == 0 and factorWrites == 1)
release()

-- A first-person view can reach the new limit within one bounded transaction.
zoom = 0
press(); finish(); assert(camera.result == 2 and zoom >= 38); release()

-- A rejected write or unreadable cap cannot be reported as fully configured.
maxFactor, refuseFactor, zoom = 1.9, true, 12
press(); finish(); assert(camera.result == 3 and zoom <= 28.5); release()
refuseFactor, missingFactor, zoom = false, true, 32
press(); finish(); assert(camera.result == 3); release()
missingFactor = false

-- Changes made by another addon are checked again on the next scene request.
maxFactor = 1.9
local writes = factorWrites
press(); finish(); assert(camera.result == 2 and maxFactor == 2.6 and factorWrites == writes + 1); release()

-- Saving a short-distance view calibrates pitch, not the outdoor zoom target.
assert(camera.target(1) == 39 and camera.target(4) == 39 and camera.target(8) == 39)
assert(camera.target(3) == 24 and camera.target(5) == 32 and camera.target(7) == 34)

-- A wall must terminate the transaction, not acknowledge a slot as success.
zoom, wall = 7, true
local calls = zoomCalls
press(); finish()
assert(camera.result == 3 and zoomCalls == calls + 1 and zoom == 7)
step(8); assert(zoomCalls == calls + 1)
release(); wall = false

missingZoom = true
press(); finish(); assert(camera.result == 4)
release(); missingZoom = false
refuseView, view, camera.applied = true, 3, 13
local epoch = camera.epoch
press(); finish(); assert(camera.result == 6 and camera.epoch ~= epoch)
release(); refuseView = false

-- Saved presets preserve the user's angle; automatic requests never nudge pitch.
zoom = 20
WowAgent.SaveQuestCamera()
assert(type(WowAgentDB.questCamera["Player-1"]) == "table")
spec, observation.action, observation.distance = 255, 1, 20
local priorViews = viewCalls
press(); assert(camera.scene == 7)
finish(); assert(camera.result == 2 and pitchStarts == 0 and viewCalls == priorViews)
release()
step(5); assert(pitchStops == 0)

-- Airborne and in-combat requests change zoom only; they never call SetView.
for _, mode in ipairs({"combat", "flying", "ghost"}) do
    combat, flying, ghost = mode == "combat", mode == "flying", mode == "ghost"
    zoom = 20
    local views, pitches = viewCalls, pitchStarts
    press(); finish()
    assert(camera.result == 2 and viewCalls == views and pitchStarts == pitches)
    release()
end
combat, flying, ghost = false, false, false

-- Losing the safe ground window, context stream or lease cancels pitch/zoom.
press(); combat = true; step(.1)
assert(camera.result == 5); release(); combat = false
press(); step(.8, false); assert(camera.result == 5); release()
press(); release(); local count = zoomCalls; step(1)
assert(camera.result == 5 and zoomCalls == count)

-- Manual observation pauses even between transactions; resuming is explicit.
mouse = true; observe(); assert(camera.result == 7)
assert(not WowAgent.RestoreQuestCamera(true))
mouse = false; WowAgent.ToggleQuestCamera(); assert(camera.result == 0)

observation.action, observation.distance = 3, 200
indoors = true; observe(); assert(camera.scene == 3)
indoors, mounted = false, true; observe(); assert(camera.scene == 2)
observation.flightMount = true; observe(); assert(camera.scene == 8)
flying, observation.distance, observation.speed = true, 30, 20
observe(); assert(camera.scene == 9)
flying, mounted, observation.flightMount = false, false, false
observation.phase, observation.distance = 3, 4; observe(); assert(camera.scene == 5)
swimming = true; observe(); assert(camera.scene == 11 and not WowAgent.RestoreQuestCamera(true))
swimming, special = false, true; observe(); assert(camera.scene == 12)
special = false
local a, b = camera.telemetry(127)
assert(a <= 1048575 and b <= 1048575 and math.floor(a / 8192) == 127 and math.floor(b / 8192) == 127)

-- F6 is optional: preserve existing keys, then verify the actual override.
local probeBinding = "USER_ACTION"
function GetBindingAction(key) return key == "F6" and probeBinding or binding end
function SetOverrideBindingClick(_, _, key, target, mouseButton)
    if key == "F6" then
        assert(mouseButton == "RightButton")
        probeBinding = "CLICK " .. target .. ":RightButton"
    else
        assert(key == "F8")
        binding = "CLICK " .. target .. ":LeftButton"
    end
end
camera.prepare({})
assert(probeBinding == "USER_ACTION" and not camera.probeReady())
probeBinding = "PITCHDOWN"
camera.prepare({})
assert(probeBinding == "PITCHDOWN" and not camera.probeReady())
probeBinding = ""
camera.prepare({})
assert(camera.probeReady())
observe()
local pitchesBefore = pitchStarts
assert(WowAgent.RestoreQuestCamera(true, true))
assert(camera.applied == 13)
for i = 1, 70 do step(.1) end
assert(camera.result == 2 and zoom == 24 and pitchStarts == pitchesBefore)
assert(pitchStarts == pitchStops)
-- A fresh baseline is loaded on every transaction; no cumulative pitch.
observe()
assert(WowAgent.RestoreQuestCamera(true, true))
step(.7)
button.OnClick(button, "RightButton", false)
assert(camera.result == 5 and pitchStarts == pitchStops)
combat = true; observe(); assert(not WowAgent.RestoreQuestCamera(true, true))
combat = false; flying = true; observe(); assert(not WowAgent.RestoreQuestCamera(true, true))
flying = false

-- Both calibrated and uncalibrated probes restore a native preset, never relative pitch.
WowAgentDB = nil
camera.applied = 5
zoom = 32
observe()
local prior = viewCalls
assert(WowAgent.RestoreQuestCamera(true, true))
finish()
assert(requestedView == 2 and viewCalls == prior + 1 and pitchStarts == 0 and camera.result == 2)
release()
-- Returning from PROBE must reload the baseline even when distance matches.
zoom = 32
prior = viewCalls
press(); finish()
assert(viewCalls == prior + 1 and pitchStarts == 0 and camera.applied == 5)
release()
-- A nearby NPC is one stable interaction scene, including the previous 10-yard failure.
observation.phase, observation.distance, observation.action = 3, 10.3, 0
camera.scene = 1
observe(); assert(camera.scene == 5)
observation.distance = 24; observe(); assert(camera.scene == 5)
observation.distance = 27; observe(); assert(camera.scene == 1)

-- Overhead is an explicit user-saved preset; never a guessed timed pitch.
assert(not camera.overheadReady())
zoom = 32
mounted = true; WowAgent.SaveOverheadQuestCamera(); assert(not camera.overheadReady())
mounted = false
function GetUnitSpeed() return 3 end
WowAgent.SaveOverheadQuestCamera(); assert(not camera.overheadReady())
function GetUnitSpeed() return 0 end
WowAgent.SaveOverheadQuestCamera()
assert(camera.overheadReady() and savedSlots[#savedSlots] == 4)
assert(WowAgentDB.questOverheadCamera["Player-1"].zoom == 32)
assert(not WowAgentDB.questCamera)
observe()
assert(WowAgent.RestoreQuestCamera(true, true))
finish()
assert(camera.applied == 14 and view == 4 and zoom == 32 and pitchStarts == 0)
button.OnClick(button, "RightButton", false)
prior = viewCalls
press(); finish(); release()
assert(viewCalls == prior+1 and view == 2 and camera.applied == 1)
zoom = 32
WowAgent.SaveQuestCamera()
assert(savedSlots[#savedSlots] == 5 and camera.overheadReady())
observe(); assert(WowAgent.RestoreQuestCamera(true, true)); finish()
button.OnClick(button, "RightButton", false)
observation.distance = 10; observe()
press(); finish(); release()
assert(camera.applied == 5 and view == 5 and zoom == 32 and pitchStarts == 0)

-- Automated mouse orbit requires an explicit, expiring, key-held lease.
local orbitBinding = "USER_ACTION"
function GetBindingAction(key)
    if key == "F4" then return orbitBinding end
    return key == "F6" and probeBinding or binding
end
function SetOverrideBindingClick(_, _, key, target, mouseButton)
    if key == "F4" then orbitBinding = "CLICK " .. target .. ":" .. mouseButton
    elseif key == "F6" then probeBinding = "CLICK " .. target .. ":RightButton"
    else binding = "CLICK " .. target .. ":LeftButton" end
end
camera.prepare({}); assert(not camera.orbitReady() and orbitBinding == "USER_ACTION")
orbitBinding = ""; camera.prepare({}); assert(camera.orbitReady())
observe(); assert(WowAgent.RestoreQuestCamera(true, true)); finish()
button.OnClick(button, "RightButton", false)
local requestBefore = camera.request
button.OnClick(button, "MiddleButton", true)
assert(camera.orbitActive() and camera.request == (requestBefore+1)%8)
mouse = true; observe(); assert(camera.result ~= 7 and camera.orbitActive())
mouse = false; button.OnClick(button, "MiddleButton", false); assert(not camera.orbitActive())
button.OnClick(button, "MiddleButton", true); now=now+.7
mouse = true; observe(); assert(camera.result == 7 and not camera.orbitActive())
mouse = false; WowAgent.ToggleQuestCamera(); observe()
mounted = true; button.OnClick(button, "MiddleButton", true); assert(not camera.orbitActive())
mounted = false
WowAgentDB.questOverheadCamera = nil
assert(camera.overheadReady() and camera.overheadZoom() == nil)
observe(); assert(WowAgent.RestoreQuestCamera(true, true)); finish()
assert(camera.applied == 14 and view == 4)
button.OnClick(button, "RightButton", false)

-- Forever keeps the progression key even when camera preparation runs first.
WowAgent.Forever = {}
local previousBinding = GetBindingAction
orbitBinding = "TARGETPARTYMEMBER3"
function GetBindingAction(key)
    if key == "LEFT" then return "TURNLEFT" end
    if key == "RIGHT" then return "TURNRIGHT" end
    return previousBinding(key)
end
camera.prepare({})
assert(orbitBinding == "TARGETPARTYMEMBER3")
assert(not camera.orbitReady() and camera.overheadReady() and camera.turnKeysReady())
assert(WowAgent.TaskBindings.BindClick({}, "forever_progress"))
assert(WowAgent.TaskBindings.ClickReady("forever_progress") and not camera.orbitReady())
