-- Run with Lua 5.1 or lupa.lua51 from the repository root.
local layouts = {[10] = {[1] = {"spell", 133}}, [20] = {[1] = {"spell", 1459}, [2] = {"macro", 2}}}
local macros = {[1] = {"WASAssist", 0, "/cast 一键辅助"}, [2] = {"旧宏", 0, "/say old"}}
local selected, requested, cursor, timer = 10
local now, blocked, moves, locked = 0, false, 0, false
local rejectSlot
local messages = {}

for i = 1, 12 do _G["ActionButton" .. i] = {action = i} end
MultiBarRightButton1 = {action = 25, IsVisible = function() return true end}
C_ActionBar = {
    HasAction = function(slot) return layouts[selected][slot] ~= nil end,
    PutActionInSlot = function(slot)
        if blocked and cursor[1] == "macro" and cursor[2] == 1 then return end
        assert(not layouts[selected][slot])
        layouts[selected][slot] = cursor
        cursor = nil
        moves = moves + 1
    end,
}
C_ClassTalents = {
    GetLastSelectedSavedConfigID = function() return selected end,
    GetConfigIDsBySpecID = function() return {10, 20} end,
}
C_Timer = {After = function(_, cb) timer = cb end}
function GetTime() return now end
function UnitGUID() return "Player-1" end
function GetCursorInfo() if cursor then return unpack(cursor) end end
function ClearCursor() cursor = nil end
function GetMacroInfo(id) if macros[id] then return unpack(macros[id]) end end
function GetMacroIndexByName(name)
    for id, row in pairs(macros) do if row[1] == name then return id end end
end
function PickupAction(slot)
    if slot == rejectSlot then return end
    cursor, layouts[selected][slot] = layouts[selected][slot], nil
    moves = moves + 1
end
function PickupMacro(id) cursor = {"macro", id} end
function PickupSpell(id) cursor = {"spell", id, "spell", id} end
function print(line) messages[#messages + 1] = line end

WowAgent = {LayoutSupport = {
    Identity = function() return {spec = 255, build = 70783} end,
    Public = function(value) return value end,
    NormalBar = function() return not locked end,
    ReadAction = function(slot) if layouts[selected][slot] then return unpack(layouts[selected][slot]) end end,
    Disarm = function() end,
    SwitchLoadout = function(id) requested = id; return true end,
    LearnedSpells = function() return {[133] = "火球术", [1459] = "奥术智慧"} end,
    MacroDefinition = function(kind) if kind == "assist" then return unpack({macros[1][1], macros[1][3]}) end end,
    WriteMacro = function(name, body)
        assert(name == macros[1][1] and body == macros[1][3])
        return 1
    end,
}}
dofile("addon/WowAgent/ActionLayout.lua")
local function cmd(command) WowAgent.ActionLayoutCommand(command) end
local function tick()
    now = now + .2
    local nextStep = timer
    timer = nil
    nextStep()
end
local function finishSwitch()
    selected, requested = requested, nil
    tick()
    assert(timer)
    tick()
end
local function backup() return WowAgentDB.actionLayouts["Player-1"].backup end

cmd("plan visible 20 1=assist r.1=spell:133")
assert(moves == 0)
cmd("apply")
assert(backup() and selected == 10 and requested == 20 and moves == 0)
finishSwitch()
assert(backup().complete and layouts[10][1][2] == 133)
assert(layouts[20][1][2] == 1 and layouts[20][2] == nil and layouts[20][25][2] == 133)
cmd("restore")
assert(layouts[20][1][2] == 1459 and layouts[20][2][2] == 2 and layouts[20][25] == nil)
finishSwitch()
assert(selected == 10 and not backup())

cmd("plan main keep 1=assist")
blocked = true
cmd("apply")
assert(layouts[10][1][1] == "spell" and layouts[10][1][2] == 133 and not backup())
blocked = false
layouts[10][4] = {"equipmentset", 77}
local before = moves
cmd("apply")
assert(moves == before and layouts[10][4][2] == 77 and not backup())
layouts[10][4] = nil

cmd("plan main 20 1=assist")
cmd("apply")
cmd("cancel")
selected, requested = requested, nil
tick()
assert(moves == before and backup() and not backup().complete)
cmd("restore")
finishSwitch()
assert(selected == 10 and not backup())

cursor = {"item", 123}
cmd("apply")
assert(cursor[2] == 123 and moves == before)

-- Clearing uses the current main bar and keeps its recoverable snapshot.
cmd("clear")
assert(cursor[2] == 123 and moves == before)
ClearCursor()
layouts[10][2], layouts[10][25] = {"macro", 2}, {"spell", 1459}
local plan = WowAgentDB.actionLayouts["Player-1"].plan
locked = true
cmd("clear")
assert(moves == before and not backup())
locked = false
cmd("clear")
assert(selected == 10 and not requested and backup().scope == "main" and backup().complete)
assert(WowAgentDB.actionLayouts["Player-1"].plan == plan)
for slot = 1, 12 do assert(layouts[10][slot] == nil) end
assert(layouts[10][25][2] == 1459 and macros[2][1] == "旧宏")
before = moves
local saved = backup()
cmd("clear")
assert(moves == before and backup() == saved)
cmd("restore")
assert(not backup() and layouts[10][1][2] == 133 and layouts[10][2][2] == 2)

-- A rejected removal rolls the already-cleared slots back.
rejectSlot = 2
cmd("clear")
assert(not backup() and layouts[10][1][2] == 133 and layouts[10][2][2] == 2)
rejectSlot = nil

-- A paged main bar clears the displayed slots and restores on that page.
for i = 1, 12 do _G["ActionButton" .. i].action = i + 12 end
layouts[10][13] = {"spell", 133}
cmd("clear")
assert(backup() and backup().slots[1].slot == 13)
assert(not layouts[10][13] and layouts[10][1][2] == 133 and layouts[10][25][2] == 1459)
for i = 1, 12 do _G["ActionButton" .. i].action = i end
cmd("restore")
assert(backup() and not layouts[10][13])
for i = 1, 12 do _G["ActionButton" .. i].action = i + 12 end
cmd("restore")
assert(not backup() and layouts[10][13][2] == 133)
