-- Explicit quest encounter identity. Never guess an NPC ID from its quest giver.
WowAgent.QuestEncounter = {}
local function Public(value)
    if issecretvalue and issecretvalue(value) then return nil end
    return value
end

function WowAgent.QuestEncounter.Name(id, phase)
    local retail = WowAgent.RetailQuestData
    if phase ~= 2 or not retail or retail.map ~= C_Map.GetBestMapForUnit("player") then return end
    local profile = retail.quests[id]
    local locale = GetLocale and GetLocale()
    local name = profile and profile.respawnNames and profile.respawnNames[locale]
    if type(name) == "string" and name ~= "" and not name:find("[%z\1-\31\127%[%];]") then return name end
end

function WowAgent.QuestEncounter.State(id, phase, button)
    local name = WowAgent.QuestEncounter.Name(id, phase)
    if not name then return 0, 0 end
    local flags = 2048 -- supported profile, distinct from verified selector
    local binding = Public(GetBindingAction("K", true))
    if type(binding) == "string" and binding:lower() == "click wowagenttasktarget:leftbutton"
        and button and Public(button:GetAttribute("type")) == "macro"
        and Public(button:GetAttribute("macrotext")) == "/targetexact " .. name then
        flags = flags + 128
    end
    local guid = UnitGUID and Public(UnitGUID("target"))
    local creature = type(guid) == "string" and tonumber(guid:match("^Creature%-%d+%-%d+%-%d+%-%d+%-(%d+)%-"))
    if not creature or not UnitName or Public(UnitName("target")) ~= name then return flags, 0 end
    flags = flags + 256
    local attackable = UnitCanAttack and Public(UnitCanAttack("player", "target")) == true
    local alive = UnitIsDead and Public(UnitIsDead("target")) == false
    if attackable then flags = flags + 512 end
    if alive then flags = flags + 1024 end
    return flags, attackable and alive and creature or 0
end
