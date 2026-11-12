-- Observe death dialogs and prepare verified, temporary class actions.
local secret = issecretvalue or function() return false end
local function Public(value)
    if not secret(value) then return value end
end

local callPet = {883, 83242, 83243, 83244, 83245}
local classBuff = {MAGE = 1459, PRIEST = 21562, DRUID = 1126, WARRIOR = 6673}
local keys = {Follow = "F9", Summon = "F10", Revive = "F11", Buff = "F12"}
local buttons = {}

local function SpellName(id)
    if not id then return end
    local known = C_SpellBook and C_SpellBook.IsSpellKnown or IsPlayerSpell
    if known and Public(known(id)) == true and C_Spell and C_Spell.GetSpellName then
        return Public(C_Spell.GetSpellName(id))
    end
end

local function CharacterOptions()
    local key = UnitGUID and Public(UnitGUID("player"))
    if type(key) ~= "string" then return {} end
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.recovery = WowAgentDB.recovery or {}
    WowAgentDB.recovery[key] = WowAgentDB.recovery[key] or {}
    return WowAgentDB.recovery[key]
end

function WowAgent.ConfigureRecovery(args)
    local kind, id, aura = args:match("^recovery (%a+) (%d+)%s*(%d*)$")
    id, aura = tonumber(id), tonumber(aura)
    if kind ~= "pet" and kind ~= "buff" then
        print("WowAgent：/wagent quest recovery pet <召唤技能ID>，或 recovery buff <技能ID> [光环ID]。0 恢复职业默认。")
        return
    end
    if id ~= 0 and not SpellName(id) then
        return print("WowAgent：当前角色未学会该技能。")
    end
    local options = CharacterOptions()
    options[kind] = id ~= 0 and id or nil
    if kind == "buff" then options.aura = id ~= 0 and (aura or id) or nil end
    print("WowAgent：已保存复活准备设置，重新准备任务后生效。")
end

local function Bind(owner, plan, action, body)
    if not body then return end
    local name = "WowAgentRecovery" .. action
    local button = buttons[action] or CreateFrame("Button", name, UIParent, "SecureActionButtonTemplate")
    buttons[action] = button
    button:RegisterForClicks("AnyDown", "AnyUp")
    button:SetAttribute("type", "macro")
    button:SetAttribute("macrotext", body)
    SetOverrideBindingClick(owner, false, keys[action], name, "LeftButton")
    plan[action] = body
end

local function Cast(name, condition)
    if name then return "/cast " .. (condition or "") .. name end
end

function WowAgent.PrepareRecovery(owner)
    local options = CharacterOptions()
    local class = UnitClass and Public(select(2, UnitClass("player")))
    local summon = SpellName(options.pet)
    local revive
    if class == "HUNTER" then
        revive = SpellName(982)
        local book = C_SpellBook
        if not revive and book and book.IsSpellInSpellBook and C_Spell and C_Spell.GetSpellName
            and Public(book.IsSpellInSpellBook(982)) == true then
            revive = Public(C_Spell.GetSpellName(982))
        end
        -- Call Pet flyout spells may not be reported as ordinary spellbook entries.
        if not summon and GetCallPetSpellInfo and C_Spell and C_Spell.GetSpellName then
            for _, id in ipairs(callPet) do
                local ok, slot, pet = pcall(GetCallPetSpellInfo, id)
                slot, pet = Public(slot), Public(pet)
                if ok and slot and type(pet) == "string" and pet ~= "" then
                    summon = Public(C_Spell.GetSpellName(id))
                    if summon then break end
                end
            end
        end
    elseif class == "WARLOCK" and not summon then
        summon = SpellName(30146) or SpellName(688)
    end
    local buffID = options.buff or classBuff[class]
    local buff = SpellName(buffID)
    local plan = {aura = buff and (options.aura or buffID)}
    if summon or revive then
        Bind(owner, plan, "Follow", "/petfollow")
    end
    Bind(owner, plan, "Summon", Cast(summon))
    Bind(owner, plan, "Revive", Cast(revive))
    Bind(owner, plan, "Buff", Cast(buff, "[@player] "))
    return plan
end

local function Verified(plan, action)
    if not plan or not plan[action] then return false end
    local binding = Public(GetBindingAction(keys[action], true))
    return type(binding) == "string"
        and binding:lower() == ("CLICK WowAgentRecovery" .. action .. ":LeftButton"):lower()
        and Public(buttons[action]:GetAttribute("macrotext")) == plan[action]
end

local function Point(x, y)
    if type(x) ~= "number" or type(y) ~= "number" or x ~= x or y ~= y
        or x < 0 or x > 1 or y < 0 or y > 1 then return end
    return math.floor(x * 1023 + .5) * 1024 + math.floor(y * 1023 + .5)
end

local function Dialog(which)
    if not StaticPopup_FindVisible then return end
    local dialog = StaticPopup_FindVisible(which)
    if not dialog or not dialog:IsVisible() then return end
    local button = dialog.GetButton1 and dialog:GetButton1() or dialog.button1
    if not button or not button:IsVisible() or not button:IsEnabled() then return true end
    local ok, x, y = pcall(button.GetCenter, button)
    x, y = Public(x), Public(y)
    if not ok or not x or not y then return true end
    local width, height = UIParent:GetWidth(), UIParent:GetHeight()
    local scale = button:GetEffectiveScale() / UIParent:GetEffectiveScale()
    local point = Point((x * scale - UIParent:GetLeft()) / width, (UIParent:GetTop() - y * scale) / height)
    local hovered = button.IsMouseOver and Public(button:IsMouseOver()) == true
    if hovered and GetMouseFoci then
        local focus = Public(GetMouseFoci())
        hovered = type(focus) == "table" and Public(focus[1]) == button
    end
    return true, point, math.floor(height / width * 100000 + .5), hovered
end

function WowAgent.RecoveryState(plan, map)
    local dead = UnitIsDead and Public(UnitIsDead("player"))
    local ghost = UnitIsGhost and Public(UnitIsGhost("player"))
    local life = ghost == true and 3 or dead == true and 2
        or dead == false and ghost == false and 1 or 0
    if life >= 2 then
        local corpse
        local api = C_DeathInfo and C_DeathInfo.GetCorpseMapPosition
        if life == 3 and api and map > 0 then
            local ok, pos = pcall(api, map)
            pos = ok and Public(pos)
            if pos then
                local x, y = pos:GetXY()
                x, y = Public(x), Public(y)
                if x ~= 0 or y ~= 0 then corpse = Point(x, y) end
            end
        end
        local shown, point, aspect, hovered = Dialog(life == 3 and "RECOVER_CORPSE" or "DEATH")
        return life + (corpse and 512 or 0) + (shown and 1024 or 0) + (hovered and 2048 or 0)
            + (point and 4096 or 0), corpse or 0, point or 0, aspect or 0
    end
    if life ~= 1 or not plan then return life, 0, 0, 0 end
    local pet = 0
    if plan.Follow then
        local exists = UnitExists and Public(UnitExists("pet"))
        local petDead = UnitIsDead and Public(UnitIsDead("pet"))
        if exists == false then pet = 1
        elseif exists == true and petDead == false then pet = 2
        elseif exists == true and petDead == true then pet = 3 end
    end
    local buffState = 0
    if plan.aura then
        local api = C_UnitAuras and C_UnitAuras.GetPlayerAuraBySpellID
        local ok, aura = false, nil
        if api then ok, aura = pcall(api, plan.aura) end
        if not ok or secret(aura) then buffState = 256
        elseif not aura then buffState = Verified(plan, "Buff") and 128 or 256 end
    end
    return life + pet * 4 + (Verified(plan, "Follow") and 16 or 0)
        + (Verified(plan, "Summon") and 32 or 0) + (Verified(plan, "Revive") and 64 or 0)
        + buffState + (plan.Follow and 512 or 0), 0, 0, 0
end
