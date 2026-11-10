-- Run from the repository root with Lua 5.1 or lupa.lua51.
local macros, macroIDs, calls, rows, frames = {}, {}, {}, {}, {}
local locked, gearLocked, assist = false, false, true
local actionKind, actionID, activeSpec = "macro", 1, 1
local currentLoadout, equippedSet
local createdScopes, readbackSuffix, createError = {}, "", nil

UIParent = {}
WowAgentChannelFrame = {}
WowAgent = {
    NewPixelRow = function(_, offset)
        return function(_, value) rows[offset] = value end
    end,
}
function CreateFrame()
    local frame = {scripts = {}}
    function frame:SetScript(event, callback) self.scripts[event] = callback end
    frames[#frames + 1] = frame
    return setmetatable(frame, {__index = function() return function() end end})
end
function UnitClass() return "Mage", "MAGE", 8 end
function UnitRace() return "Human", "Human", 1 end
function UnitLevel() return 80 end
function GetLocale() return "zhCN" end
function GetBuildInfo() return "12.1.5", "70783" end
function InCombatLockdown() return locked end
function UnitCanAttack() return false end
function UnitIsDead() return false end
function UnitIsGhost() return false end
function UnitGUID() return nil end
function IsFlying() return false end
function IsMounted() return false end
function UnitHealth() return 100 end
function UnitHealthMax() return 100 end
function UnitPower() return 80 end
function UnitPowerMax() return 100 end
function GetBindingAction() return "ACTIONBUTTON4" end
function GetActionInfo() return actionKind, actionID end
ActionButton4 = {action = 4}

local spellNames = {[133] = "火球术", [1459] = "奥术智慧", [121183] = "一键辅助",
                    [458] = "棕马", [59569] = "青铜幼龙"}
C_Spell = {GetSpellName = function(id) return spellNames[id] end}
Enum = {SpellBookSpellBank = {Player = 0}, SpellBookItemType = {Spell = 1}}
C_SpellBook = {
    GetNumSpellBookSkillLines = function() return 1 end,
    GetSpellBookSkillLineInfo = function() return {itemIndexOffset = 0, numSpellBookItems = 2} end,
    GetSpellBookItemInfo = function(slot) return {itemType = 1, spellID = slot == 1 and 133 or 1459} end,
}
C_AssistedCombat = {
    IsAvailable = function() return assist end,
    GetActionSpell = function() return 121183 end,
}
C_MountJournal = {
    GetMountIDs = function() return {6, 20, 99} end,
    GetMountInfoByID = function(id)
        local spell = ({[6] = 458, [20] = 59569, [99] = 458})[id]
        if spell then return spellNames[spell], spell, nil, false, true, nil, true, false, nil, false, id ~= 99 end
    end,
}
C_Item = {
    GetItemCount = function(id) return id == 1000 and 2 or 0 end,
    GetItemSpell = function(id) return id == 1000 and "食物" end,
}
C_EquipmentSet = {
    GetEquipmentSetIDs = function() return {0, 2} end,
    GetEquipmentSetInfo = function(id) return "装备" .. id, nil, id, equippedSet == id, 16, 0, 16, id == 2 and 1 or 0 end,
    EquipmentSetContainsLockedItems = function() return gearLocked end,
    UseEquipmentSet = function(id) equippedSet = id; return true end,
}
C_SpecializationInfo = {
    GetSpecialization = function() return activeSpec end,
    GetSpecializationInfo = function(index) return index == 1 and 63 or 64, "专精" .. index end,
    SetSpecialization = function(index) activeSpec = index; return true end,
}
-- Retail keeps the count global while the query and switch are namespaced.
function GetNumSpecializations() return 2 end
C_ClassTalents = {
    GetActiveConfigID = function() return currentLoadout or 10 end,
    GetLastSelectedSavedConfigID = function() return currentLoadout end,
    GetConfigIDsBySpecID = function(spec) return spec == 64 and {22} or {11} end,
    LoadConfig = function(id) currentLoadout = id; return 3 end,
}
C_Traits = {GetConfigInfo = function(id) return {name = "天赋" .. id} end}

function GetMacroIndexByName(name) return macroIDs[name] or 0 end
function GetMacroBody(id) return macros[id] and macros[id] .. readbackSuffix end
function CreateMacro(name, _, body, character)
    if createError then error(createError) end
    assert(#body <= 255)
    local id = #macros + 1
    macros[id], macroIDs[name] = body, id
    createdScopes[id] = character == true
    return id
end
function print(...) calls[#calls + 1] = {...} end
local function command(value) WowAgent.CombatCommand(value) end
local function macro(name) return macros[macroIDs[name]] end

dofile("addon/WowAgent/Combat.lua")
command("macro assist")
assert(macro("WASAssist") == "#showtooltip 一键辅助\n/cast [harm,nodead,nochanneling] 一键辅助")
command("macro assist")
assert(#macros == 1)
command("arm 4 assist")
assert(math.floor(rows[96] / 2) % 2 == 1)
local oldCanAttack, oldGUID = UnitCanAttack, UnitGUID
UnitCanAttack = function() return true end
UnitGUID = function() return "Creature-0-0-0-0-244669-01" end
function GetPlayerFacing() return math.pi / 2 end
local previousBinding = GetBindingAction
function GetBindingAction(key)
    if key == "LEFT" then return "TURNLEFT" end
    if key == "RIGHT" then return "TURNRIGHT" end
    return previousBinding(key)
end
SPELL_FAILED_UNIT_NOT_INFRONT = "目标需要在你面前"
frames[1].scripts.OnUpdate(nil, .2)
assert(rows[80] == 244669 and math.floor(rows[48] / 524288) == 0)
assert(rows[0] % 512 == 129 and math.floor(rows[0] / 524288) == 1)
frames[2].scripts.OnEvent(nil, "UI_ERROR_MESSAGE", 0, "距离太远")
frames[1].scripts.OnUpdate(nil, .2)
assert(math.floor(rows[0] / 65536) % 8 == 0)
frames[2].scripts.OnEvent(nil, "UI_ERROR_MESSAGE", 0, SPELL_FAILED_UNIT_NOT_INFRONT)
frames[1].scripts.OnUpdate(nil, .2)
assert(math.floor(rows[0] / 65536) % 8 == 1)
frames[2].scripts.OnEvent(nil, "PLAYER_TARGET_CHANGED")
frames[1].scripts.OnUpdate(nil, .2)
assert(math.floor(rows[0] / 65536) % 8 == 0)
GetBindingAction = previousBinding
UnitThreatSituation = function(unit, target)
    assert(target == "target")
    if unit == "pet" then return 0 end
end
frames[1].scripts.OnUpdate(nil, .2)
assert(rows[80] == 244669 and math.floor(rows[48] / 524288) == 1)
UnitCanAttack, UnitGUID, UnitThreatSituation = oldCanAttack, oldGUID, nil
assist = false
frames[2].scripts.OnEvent(nil, "SPELLS_CHANGED")
frames[1].scripts.OnUpdate(nil, .2)
assert(math.floor(rows[96] / 2) % 2 == 0)
assist = true
frames[2].scripts.OnEvent(nil, "SPELLS_CHANGED")
frames[1].scripts.OnUpdate(nil, .2)
assert(math.floor(rows[96] / 2) % 2 == 1)
macros[1] = "/say original"
command("macro assist")
command("arm 4 assist")
assert(macros[1] == "/say original")
assert(macro("WASAssist1") == "#showtooltip 一键辅助\n/cast [harm,nodead,nochanneling] 一键辅助")
command("macro assist")
assert(#macros == 2)
actionKind, actionID = "spell", 121183
command("arm 4 assist")
assert(math.floor(rows[96] / 2) % 2 == 1)
assist = false
command("macro assist")
assert(#macros == 2)

command("mounts")
command("macro mount 6")
command("macro fly 20")
command("macro mount 99")
assert(macro("WAM6") == "#showtooltip 棕马\n/cast [nocombat,nomounted] 棕马")
assert(macro("WAF20"):find("[nocombat,nomounted,advflyable]", 1, true))
assert(not macro("WAM99"))

command("macro buff 1459")
command("macro buff 999")
command("macro supply 1000")
command("macro supply 1001")
assert(macro("WAB1459") == "#showtooltip 奥术智慧\n/cast [@player,nocombat] 奥术智慧")
assert(macro("WAI1000") == "#showtooltip item:1000\n/use [nocombat,nomounted] item:1000")
assert(not macro("WAB999") and not macro("WAI1001"))

command("prep")
command("gear 2")
assert(equippedSet == nil)
gearLocked = true
command("gear 0")
assert(equippedSet == nil)
gearLocked = false
locked = true
command("gear 0")
command("spec 64")
command("macro 133")
assert(equippedSet == nil and activeSpec == 1 and not macro("WAS133"))
locked = false
command("gear 0")
command("spec 64")
command("loadout 11")
assert(equippedSet == 0 and activeSpec == 2 and currentLoadout == nil)
command("loadout 22")
assert(currentLoadout == 22)
-- Older clients can expose all specialization functions as globals.
GetSpecialization, GetSpecializationInfo, SetSpecialization =
    C_SpecializationInfo.GetSpecialization, C_SpecializationInfo.GetSpecializationInfo, C_SpecializationInfo.SetSpecialization
local specAPI = C_SpecializationInfo
C_SpecializationInfo = nil
command("spec 63")
assert(activeSpec == 1)
command("spec 999")
assert(activeSpec == 1)
C_SpecializationInfo = specAPI
local switch = specAPI.SetSpecialization
specAPI.SetSpecialization = function() return false end
command("spec 64")
assert(activeSpec == 1 and calls[#calls][1]:find("游戏未接受", 1, true))
specAPI.SetSpecialization = switch
command("spec 64")
assert(activeSpec == 2)
C_ClassTalents.SwitchToLoadoutByName = function(name) currentLoadout = name end
command("loadout 22")
assert(currentLoadout == "天赋22")
C_ClassTalents.GetConfigIDsBySpecID = function() return {22, 23} end
C_Traits.GetConfigInfo = function() return {name = "重名天赋"} end
command("loadout 22")
assert(currentLoadout == "天赋22")

-- Prefer a matching action, otherwise use only an explicitly empty bound slot.
local bar, cursorKind, cursorID, placements = {[5] = {"spell", 133}}, nil, nil, 0
local rejected, vehicle = false, false
ActionButton5, ActionButton6 = {action = 5}, {action = 6}
function GetBindingAction(key)
    if key == "5" or key == "6" then return "ACTIONBUTTON" .. key end
end
function GetActionInfo(slot)
    if bar[slot] then return unpack(bar[slot]) end
end
function GetCursorInfo() return cursorKind, cursorID end
function PickupMacro(index) cursorKind, cursorID = "macro", index end
function ClearCursor() cursorKind, cursorID = nil, nil end
C_ActionBar = {
    HasAction = function(slot) return bar[slot] ~= nil end,
    HasVehicleActionBar = function() return vehicle end,
    PutActionInSlot = function(slot)
        if rejected then return end
        assert(bar[slot] == nil)
        bar[slot] = {cursorKind, cursorID}
        placements = placements + 1
        ClearCursor()
    end,
}
macros[1] = "#showtooltip 一键辅助\n/cast [harm,nodead,nochanneling] 一键辅助"
assist = true
currentLoadout = 22
command("setup assist 5")
assert(placements == 0 and bar[5][2] == 133)
cursorKind, cursorID = "item", 1000
command("setup assist 6")
assert(placements == 0 and cursorKind == "item")
ClearCursor()
vehicle = true
command("setup assist 6")
assert(placements == 0)
vehicle = false
rejected = true
command("setup assist 6")
assert(placements == 0 and cursorKind == nil)
rejected = false
command("setup assist")
assert(placements == 1 and bar[6][1] == "macro" and bar[6][2] == 1)
assert(math.floor(rows[96] / 2) % 2 == 1)
command("setup assist")
assert(placements == 1)
-- A conflicting legacy macro must not block automatic setup on an empty slot.
macros[1] = "/say original"
bar[6] = nil
command("setup assist 6")
assert(placements == 2 and bar[6][2] == macroIDs.WASAssist1)
assert(macros[1] == "/say original" and math.floor(rows[96] / 2) % 2 == 1)
command("setup assist 6")
assert(placements == 2)
macros[1] = "#showtooltip 一键辅助\n/cast [harm,nodead,nochanneling] 一键辅助"
bar[6] = nil
command("setup mount 6 6")
assert(placements == 3 and bar[6][2] == macroIDs.WAM6)

-- The switch can finish later. The macro must go on the confirmed loadout's bar.
local scheduled
C_Timer = {After = function(_, callback) scheduled = callback end}
C_ClassTalents.GetConfigIDsBySpecID = function() return {22} end
C_Traits.GetConfigInfo = function(id) return {name = "天赋" .. id} end
C_ClassTalents.SwitchToLoadoutByName = function(name) assert(name == "天赋22") end
currentLoadout = 10
bar[6] = nil
command("setup loadout 22 6")
assert(scheduled and placements == 3)
scheduled()
assert(placements == 3)
currentLoadout = 22
scheduled()
assert(placements == 3)
scheduled()
assert(placements == 4 and bar[6][2] == macroIDs.WASAssist)
bar[6] = nil
currentLoadout = 10
command("setup loadout 22 6")
command("off")
currentLoadout = 22
scheduled()
assert(placements == 4)
-- A prepared quest macro uses the fixed quest ID and the requested empty key.
WowAgent.QuestAction = function() end
bar[6] = nil
assert(WowAgent.LayoutSupport.SetupAction("quest", 90882, 6))
assert(macro("WAQ90882") == "/run WowAgent.QuestAction(90882)")
assert(bar[6][2] == macroIDs.WAQ90882)

-- Client line-ending normalization must preserve idempotent creation and arming.
readbackSuffix = "\r\n"
bar[6] = nil
assert(WowAgent.LayoutSupport.SetupAction("quest", 90883, 6))
local count = #macros
assert(WowAgent.LayoutSupport.SetupAction("quest", 90883, 6))
assert(#macros == count and bar[6][2] == macroIDs.WAQ90883)
bar[6] = nil
assert(WowAgent.LayoutSupport.SetupAction("assist", nil, 6))
assert(#macros == count and math.floor(rows[96] / 2) % 2 == 1)
readbackSuffix = ""

-- Use the client's capacity constants, and fall back to an available account slot.
Constants = {MacroConsts = {MAX_ACCOUNT_MACROS = 120, MAX_CHARACTER_MACROS = 18}}
function GetNumMacros() return 0, 18 end
bar[6] = nil
assert(WowAgent.LayoutSupport.SetupAction("quest", 90884, 6))
assert(createdScopes[macroIDs.WAQ90884] == false)
count = #macros
function GetNumMacros() return 120, 18 end
bar[6] = nil
assert(not WowAgent.LayoutSupport.SetupAction("quest", 90885, 6))
assert(#macros == count and not bar[6] and calls[#calls][1]:find("宏位已满", 1, true))

-- Reuse a compatible numbered macro even if the base name is free and macros are full.
macroIDs.WAQ908832 = macroIDs.WAQ90883
macroIDs.WAQ90883 = nil
assert(WowAgent.LayoutSupport.SetupAction("quest", 90883, 6))
assert(#macros == count and bar[6][2] == macroIDs.WAQ908832)

-- A client rejection reports its actual cause without attempting placement.
function GetNumMacros() return 0, 0 end
bar[6] = nil
createError = "test client blocked CreateMacro"
assert(not WowAgent.LayoutSupport.SetupAction("quest", 90886, 6))
assert(#macros == count and not bar[6] and calls[#calls][1]:find(createError, 1, true))
createError = nil

-- Retail resolves cast/use macro actions to spell/item IDs, not macro indices.
function GetNumMacros() return #macros, 0 end
function GetMacroInfo(index)
    for name, id in pairs(macroIDs) do
        if id == index then return name, 134400, macros[id] end
    end
end
C_ActionBar.GetActionText = function(slot)
    local entry = bar[slot]
    if entry and entry[1] == "macro" then return GetMacroInfo(entry[2]) end
    return ""
end
C_ActionBar.GetActionInfo = function(slot)
    local entry = bar[slot]
    if not entry then return end
    if entry[1] == "macro" then
        if macros[entry[2]]:find("/use ", 1, true) then return "macro", slot - 1, "item" end
        return "macro", 121183, "spell"
    end
    return unpack(entry)
end
bar[6] = nil
assert(WowAgent.LayoutSupport.SetupAction("assist", nil, 6))
local kind, index = WowAgent.LayoutSupport.ReadAction(6)
assert(kind == "macro" and index == macroIDs.WASAssist)
assert(calls[#calls][1]:find("战斗绑定已核对", 1, true))
assert(math.floor(rows[96] / 2) % 2 == 1)
count = #macros
local placed = placements
assert(WowAgent.LayoutSupport.SetupAction("assist", nil, 6))
assert(#macros == count and placements == placed)
bar[6] = nil
assert(WowAgent.LayoutSupport.SetupAction("supply", 1000, 6))
kind, index = WowAgent.LayoutSupport.ReadAction(6)
assert(kind == "macro" and index == macroIDs.WAI1000)

-- Different bodies under the same name are ambiguous across account/character tabs.
local originalMacroInfo = GetMacroInfo
local duplicateBody = "/say different"
function GetNumMacros() return #macros, 1 end
function GetMacroInfo(id)
    if id == 121 then return "WASAssist", 134400, duplicateBody end
    return originalMacroInfo(id)
end
bar[6] = {"macro", macroIDs.WASAssist}
kind, index = WowAgent.LayoutSupport.ReadAction(6)
assert(kind == "macro" and index == nil)
command("arm 6 assist")
assert(calls[#calls][1]:find("无法核对", 1, true))
duplicateBody = macros[macroIDs.WASAssist] .. "\n"
kind, index = WowAgent.LayoutSupport.ReadAction(6)
assert(kind == "macro" and index == macroIDs.WASAssist)

-- A new macro can temporarily read back as empty. Retry the same macro,
-- leaving any blank macro that existed before setup alone.
function GetNumMacros() return #macros, 0 end
CreateMacro("WATTest", 134400, "", nil)
local originalCreate = CreateMacro
function CreateMacro(name, icon, _, character)
    return originalCreate(name, icon, "", character)
end
function EditMacro(id, _, _, body)
    assert(id == macroIDs.WATTest1 and macros[id] == "")
    macros[id] = body
    return id
end
local expected = "/dismount [mounted,noflying]"
local failed, reason = WowAgent.LayoutSupport.WriteMacro("WATTest", expected)
assert(not failed and reason == "macro_pending")
count = #macros
assert(WowAgent.LayoutSupport.WriteMacro("WATTest", expected) == macroIDs.WATTest1)
assert(#macros == count and macro("WATTest") == "" and macro("WATTest1") == expected)

-- The new macro may first be absent from the name index and later have
-- a nil body in both read APIs. Keep its identity across the retry.
local indexed = false
local originalLookup, originalRead = GetMacroIndexByName, GetMacroBody
function GetMacroIndexByName(name)
    if name == "WATLand" and not indexed then return 0 end
    return originalLookup(name)
end
function GetMacroBody(id)
    if id == macroIDs.WATLand and macros[id] == "" then return nil end
    return originalRead(id)
end
function GetMacroInfo(id)
    if id == macroIDs.WATLand and macros[id] == "" then
        if indexed then return "WATLand", 134400, nil end
        return nil
    end
    return originalMacroInfo(id)
end
function EditMacro(id, _, _, body)
    assert(id == macroIDs.WATLand)
    macros[id] = body
    return id
end
failed, reason = WowAgent.LayoutSupport.WriteMacro("WATLand", expected)
assert(not failed and reason == "macro_pending")
count = #macros
failed, reason = WowAgent.LayoutSupport.WriteMacro("WATLand", expected)
assert(not failed and reason == "macro_pending" and #macros == count)
indexed = true
assert(WowAgent.LayoutSupport.WriteMacro("WATLand", expected) == macroIDs.WATLand)
assert(#macros == count and macro("WATLand") == expected)
