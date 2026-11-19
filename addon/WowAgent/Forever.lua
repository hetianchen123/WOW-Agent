-- The Forever client uses its own quest, spell and talent APIs.
local version, build, _, interface = GetBuildInfo()
if type(version) ~= "string" or not version:match("^1%.60%.") then return end

WowAgent = WowAgent or {}
local forever = {}
WowAgent.Forever = forever

-- Candidate locations from QuestieDB Forever. The live client still has to
-- confirm the giver, dialogue quest ID and acceptance or reward outcome.
forever.questData = {
    map = 1429,
    npcs = {[823] = {48.2, 42.9}, [197] = {48.9, 41.6}},
    npcNames = {
        [823] = {enUS = "Deputy Willem", zhCN = "维里副队长"},
        [197] = {enUS = "Marshal McBride", zhCN = "治安官玛克布莱德"},
        [248415] = {enUS = "Tordrin Sternblade"},
        [251507] = {enUS = "Josephine Carson"},
    },
    quests = {
        [783] = {starter = true, level = 1, pickup = 823, turnIn = 197, arrival = true},
        [7] = {level = 1, after = {[783] = true}, pickup = 197, turnIn = 197,
            objectivePoint = {50, 36}, combatNPC = 6},
    },
}

function forever.questData.NPCPoint(_, id)
    return id and forever.questData.npcs[id]
end

function forever.OutputSpell(spells)
    if select(2, UnitClass("player")) ~= "HUNTER" then return nil end
    -- Use only IDs actually returned by this character's spellbook.
    for _, id in ipairs({3044, 1978, 75, 14260, 2973}) do
        if spells[id] then return id end
    end
end

function forever.OutputMacro(primary, spells)
    local function Name(id)
        local name = spells[id]
        if type(name) == "string" and name ~= "" and not name:find("[%z\1-\31\127;%[%]]") then
            return name
        end
    end
    local spell, auto = Name(primary), Name(75)
    if not spell then return end
    local body = "#showtooltip " .. spell
    if auto then body = body .. "\n/cast [harm,nodead] !" .. auto end
    if primary ~= 75 then body = body .. "\n/cast [harm,nodead] " .. spell end
    return #body <= 255 and body or nil
end

local function Read(api, ...)
    if type(api) ~= "function" then return nil end
    local ok, first, second, third = pcall(api, ...)
    if not ok or (issecretvalue and issecretvalue(first)) then return nil end
    return first, second, third
end

local trainers = {
    {id = 248415, map = 1429, x = .512, y = .409},
    {id = 251507, map = 1429, x = .411, y = .662},
}
local talents = {{104960, 19552, 5}, {104972, 24443, 1}}
local step = {travel = 1, gossip = 3, learn = 4, wait = 5, talent = 6, macro = 7,
    learned = 8, spent = 9, updated = 10, done = 11, error = 12, finish = 13, trainerWait = 14}
local trainerGUID

local function CreatureGUID(unit, id)
    local guid = Read(UnitGUID, unit)
    if type(guid) == "string" and tonumber(guid:match("^Creature%-%d+%-%d+%-%d+%-%d+%-(%d+)%-")) == id
        and Read(UnitIsDead, unit) == false and Read(UnitCanAttack, "player", unit) == false then
        return guid
    end
end

local function TrainerService(index)
    local name, availability = Read(GetTrainerServiceInfo, index)
    local cost, profession = Read(GetTrainerServiceCost, index)
    local info = C_TooltipInfo and Read(C_TooltipInfo.GetTrainerService, index)
    local id = info and info.id
    if type(id) ~= "number" or type(name) ~= "string" then return end
    return id, availability, cost, profession
end

local function TrainerPanel(trainer)
    return trainerGUID and trainerGUID == CreatureGUID("target", trainer.id)
        and ClassTrainerFrame and ClassTrainerFrame:IsShown()
        and type(Read(GetNumTrainerServices)) == "number"
end

local function TrainerOption(trainer)
    if not GossipFrame or not GossipFrame:IsShown()
        or CreatureGUID("npc", trainer.id) ~= CreatureGUID("target", trainer.id)
        or not CreatureGUID("target", trainer.id) or not C_GossipInfo then return end
    local found
    for _, option in ipairs(Read(C_GossipInfo.GetOptions) or {}) do
        -- The trainer uses Blizzard's trainer gossip icon. Match the option on
        -- the verified NPC and require a unique, current ID.
        if (option.icon == 132049 or option.icon == 136244)
            and type(option.gossipOptionID) == "number"
            and (not (Enum and Enum.GossipOptionStatus) or option.status == Enum.GossipOptionStatus.Available) then
            if found then return end
            found = option.gossipOptionID
        end
    end
    return found
end

local function TalentNode(config, id)
    return C_Traits and Read(C_Traits.GetNodeInfo, config, id)
end

local function TalentChoice()
    local spec = C_SpecializationInfo and Read(C_SpecializationInfo.GetActiveSpecGroup)
    local config = spec and Read(GetCombatConfigIDForSpecGroup, spec)
    if type(config) ~= "number" or config <= 0 or not C_Traits then return end
    local points = Read(UnitCharacterPoints, "player")
    if type(points) == "number" and points <= 0 then return end
    if Read(C_Traits.ConfigHasStagedChanges, config) ~= false then return end
    for _, candidate in ipairs(talents) do
        local id, spell, goal = unpack(candidate)
        local node = TalentNode(config, id)
        if not node or type(node.activeRank) ~= "number" then return end
        if node.activeRank < goal then
            if node.canPurchaseRank ~= true or not node.entryIDs or #node.entryIDs ~= 1 then return end
            local entry = Read(C_Traits.GetEntryInfo, config, node.entryIDs[1])
            local definition = entry and Read(C_Traits.GetDefinitionInfo, entry.definitionID)
            if definition and definition.spellID == spell then return config, id, node.activeRank end
            return
        end
    end
end

local function Report(plan, code, value)
    plan.foreverReceipt = {code = code, value = value or 0, untilTime = GetTime() + 1.4}
    plan.foreverPending = nil
    print("WowAgent 无限服成长", code, value or 0)
end

local function State(plan, map)
    if not plan.foreverEnabled or Read(GetBindingAction, "F4", true) ~= "CLICK WowAgentForeverProgress:LeftButton"
        or Read(UnitIsDeadOrGhost, "player") ~= false then return end
    local _, class = Read(UnitClass, "player")
    local level = Read(UnitLevel, "player")
    if class ~= "HUNTER" or type(level) ~= "number" then return end
    local receipt, pending = plan.foreverReceipt, plan.foreverPending
    if receipt then
        if GetTime() < receipt.untilTime then return receipt end
        plan.foreverReceipt = nil
    end
    if pending then
        local ok
        if pending.macro then
            local support = WowAgent.LayoutSupport
            local spells = support.LearnedSpells()
            if support.ActionMatches(4, pending.macro, forever.OutputMacro(pending.macro, spells)) then
                ok = support.SetupAction("forever", pending.macro, 4) == true
            end
        elseif pending.spell then
            local support = WowAgent.LayoutSupport
            ok = support.LearnedSpells()[pending.spell] ~= nil
            if not ok and TrainerPanel(pending.trainer) then
                for i = 1, Read(GetNumTrainerServices) do
                    local id, status = TrainerService(i)
                    if id == pending.spell and status == "used" then ok = true; break end
                end
            end
        else
            local node = TalentNode(pending.config, pending.node)
            ok = node and node.activeRank == pending.rank + 1
                and Read(C_Traits.ConfigHasStagedChanges, pending.config) == false
        end
        if ok then
            local receiptCode = pending.macro and step.updated or pending.spell and step.learned or step.spent
            Report(plan, receiptCode, pending.macro or pending.spell or pending.node)
            return plan.foreverReceipt
        end
        if GetTime() >= pending.deadline then
            plan.foreverFailed = level
            if pending.trainer and TrainerPanel(pending.trainer) then pcall(CloseTrainer) end
            Report(plan, step.error, pending.macro or pending.spell or pending.node)
            return plan.foreverReceipt
        end
        return {code = step.wait}
    end
    if InCombatLockdown() or Read(UnitAffectingCombat, "player") == true or plan.foreverFailed == level then return end
    local support = WowAgent.LayoutSupport
    local spells = support.LearnedSpells()
    local spell = forever.OutputSpell(spells)
    if spell and not support.ActionMatches(4, spell, forever.OutputMacro(spell, spells)) then
        return {code = step.macro, value = spell}
    end
    if level >= 10 then
        local config, node, rank = TalentChoice()
        if config then return {code = step.talent, config = config, node = node, rank = rank} end
    end
    if level < 2 or not plan.foreverTrain or (plan.foreverCheckedLevel or 0) >= level - level % 2
        or plan.foreverSkipLevel == level then return end
    local position = C_Map and Read(C_Map.GetPlayerMapPosition, map, "player")
    local x, y
    if position then x, y = Read(position.GetXY, position) end
    if not x or not y then return end
    local trainer = y < .53 and trainers[1] or trainers[2]
    if map ~= trainer.map then return end
    if TrainerPanel(trainer) then
        if Read(GetTrainerServiceTypeFilter, "available") == false then
            return {code = step.trainerWait, trainer = trainer}
        end
        local money = Read(GetMoney)
        local count = Read(GetNumTrainerServices)
        if count <= 0 or type(money) ~= "number" then return {code = step.trainerWait, trainer = trainer} end
        local unavailable = false
        for i = 1, count do
            local id, availability, cost, profession = TrainerService(i)
            if availability == "available" and not profession then
                if id and type(cost) == "number" and cost >= 0 and cost <= money and not spells[id] then
                    return {code = step.learn, value = id, index = i, trainer = trainer}
                elseif not id or not spells[id] then
                    unavailable = true
                end
            end
        end
        return {code = step.finish, trainer = trainer, unavailable = unavailable}
    elseif TrainerOption(trainer) then
        return {code = step.gossip, trainer = trainer}
    end
    return {code = step.travel, trainer = trainer}
end

function forever.Progression(plan, map)
    local dialogueClosed = not (QuestFrame and QuestFrame:IsShown() or GossipFrame and GossipFrame:IsShown())
    if not dialogueClosed and not plan.foreverTrain then return end
    local state = State(plan, map)
    if not dialogueClosed and state and state.code ~= step.gossip then
        return
    end
    return state
end

function forever.BindProgression(owner, plan)
    local button = WowAgentForeverProgress or CreateFrame("Button", "WowAgentForeverProgress", UIParent)
    button:RegisterForClicks("LeftButtonUp")
    button:SetScript("OnClick", function()
        if InCombatLockdown() then return end
        local state = State(plan, Read(C_Map.GetBestMapForUnit, "player"))
        if not state then return end
        if state.code == step.learn and TrainerPanel(state.trainer) then
            local ok, result = pcall(BuyTrainerService, state.index)
            if ok and result ~= false then
                plan.foreverPending = {spell = state.value, trainer = state.trainer, deadline = GetTime() + 4}
            else
                plan.foreverFailed = Read(UnitLevel, "player")
                pcall(CloseTrainer)
                Report(plan, step.error, state.value)
            end
        elseif state.code == step.finish and TrainerPanel(state.trainer) then
            local level = Read(UnitLevel, "player")
            plan.foreverCheckedLevel = level
            pcall(CloseTrainer)
            Report(plan, state.unavailable and step.error or step.done, level)
        elseif state.code == step.gossip then
            local option = TrainerOption(state.trainer)
            if option then pcall(C_GossipInfo.SelectOption, option) end
        elseif state.code == step.talent then
            local config, node = state.config, state.node
            if Read(C_Traits.PurchaseRank, config, node) ~= true then
                plan.foreverFailed = Read(UnitLevel, "player")
                Report(plan, step.error, node)
                return
            end
            if Read(C_Traits.ConfigHasStagedChanges, config) == true then Read(C_Traits.CommitConfig, config) end
            plan.foreverPending = {config = config, node = node, rank = state.rank, deadline = GetTime() + 4}
        elseif state.code == step.macro then
            local ok, reason = WowAgent.LayoutSupport.SetupAction("forever", state.value, 4)
            if not ok and reason == "macro_pending" then
                plan.foreverPending = {macro = state.value, deadline = GetTime() + 4}
            else
                if not ok then plan.foreverFailed = Read(UnitLevel, "player") end
                Report(plan, ok and step.updated or step.error, state.value)
            end
        elseif state.code == step.travel or state.code == step.trainerWait then
            plan.foreverSkipLevel = Read(UnitLevel, "player")
            if state.trainer and TrainerPanel(state.trainer) then pcall(CloseTrainer) end
            Report(plan, step.error, 0)
        end
    end)
    SetOverrideBindingClick(owner, false, "F4", "WowAgentForeverProgress", "LeftButton")
    plan.foreverEnabled = true
end

local events = CreateFrame("Frame")
events:RegisterEvent("TRAINER_SHOW")
events:RegisterEvent("TRAINER_CLOSED")
events:SetScript("OnEvent", function(_, event)
    trainerGUID = nil
    if event == "TRAINER_SHOW" then
        for _, trainer in ipairs(trainers) do
            local target, npc = CreatureGUID("target", trainer.id), CreatureGUID("npc", trainer.id)
            if target and (npc == nil or npc == target) then trainerGUID = target; return end
        end
    end
end)

local function Identity()
    local _, class, classID = Read(UnitClass, "player")
    local map = C_Map and Read(C_Map.GetBestMapForUnit, "player")
    local position = map and Read(C_Map.GetPlayerMapPosition, map, "player")
    local x, y
    if position then x, y = Read(position.GetXY, position) end
    return class, classID, map, x, y
end

function forever.Inspect()
    local class, classID, map, x, y = Identity()
    print("WA FOREVER", build, interface, Read(UnitLevel, "player"), class, classID,
        map, x and math.floor(x * 10000), y and math.floor(y * 10000))
    print("WA API", type(C_QuestInfoSystem), type(C_QuestLine and C_QuestLine.GetAvailableQuestLines),
        type(C_QuestLog and C_QuestLog.GetQuestObjectives), type(GetQuestLogTitle))
    print("WA TRAIN", type(GetNumTrainerServices), type(BuyTrainerService),
        "TALENT", type(GetNumTalentTabs), type(LearnTalent), "MACRO", type(CreateMacro))
    local worldPos = C_Map and C_Map.GetWorldPosFromMapPos
    if worldPos and map and x and y then
        local instance, world = Read(worldPos, map, CreateVector2D(x, y))
        local north, east
        if world then north, east = Read(world.GetXY, world) end
        print("WA WORLD", instance, north, east, IsOutdoors(), Read(GetPlayerFacing))
    end
    print("WA INPUT", Read(GetBindingAction, "F"), Read(UnitName, "softinteract"),
        Read(UnitName, "target"), type(C_Map and C_Map.GetMapWorldSize))
    local nearby = map and C_QuestLine and Read(C_QuestLine.GetAvailableQuestLines, map)
    if type(nearby) == "table" then
        print("WA OFFERS", #nearby)
        for i = 1, math.min(4, #nearby) do
            local q = nearby[i]
            print("WA OFFER", q.questID, q.startMapID, q.x, q.y, q.isQuestStart)
        end
    end
end

SLASH_WOWAGENTFOREVER1 = "/waforever"
SlashCmdList.WOWAGENTFOREVER = forever.Inspect
