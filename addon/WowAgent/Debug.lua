-- WowAgent.Debug: slash commands and the human-readable HUD.

local f = CreateFrame("Frame", "WowAgentDebugFrame", UIParent)
f:SetSize(260, 70)
f:SetFrameStrata("TOOLTIP")
f:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 0, 4)
f.bg = f:CreateTexture(nil, "BACKGROUND")
f.bg:SetAllPoints()
f.bg:SetColorTexture(0, 0, 0, 0.5)
f.text = f:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
f.text:SetAllPoints()
f.text:SetJustifyH("LEFT")
f.text:SetJustifyV("TOP")

local learnMode = false

local function Update(self)
    if not WowAgent.debugVisible then self:Hide() return end
    local _, pos = WowAgent.BuildPos()
    local _, hdg = WowAgent.BuildHdg()
    local _, st = WowAgent.BuildStatus()
    local _, bagsum = WowAgent.BuildBagSum()
    local x = pos and math.floor((pos / 1024) / 1023 * 10000) / 10000 or -1
    local y = pos and math.floor((pos % 1024) / 1023 * 10000) / 10000 or -1
    local h = hdg and math.floor(hdg / 1048575 * 360 + 0.5) or -1
    local free = math.floor(bagsum / 4096)
    local total = bagsum % 4096
    self.text:SetText(string.format(
        "pos %.4f,%.4f  hdg %d°\nflags %d  free %d  items %d\ngold %d  zone %d  evq %d",
        x, y, h, st, free, total,
        math.floor(GetMoney() / 10000),
        C_Map.GetBestMapForUnit("player") or 0,
        #WowAgent.events
    ))
end

f:SetScript("OnUpdate", function(self, dt)
    self.acc = (self.acc or 0) + dt
    if self.acc < 0.25 then return end
    self.acc = 0
    Update(self)
end)

-- learn mode: print spellIDs of everything the player casts
local sink = CreateFrame("Frame", "WowAgentLearnFrame")
sink:RegisterEvent("UNIT_SPELLCAST_START")
sink:RegisterEvent("UNIT_SPELLCAST_SUCCEEDED")
sink:SetScript("OnEvent", function(_, event, unit, castGUID, spellId)
    if learnMode and unit == "player" and not (issecretvalue and issecretvalue(spellId)) then
        local name = C_Spell.GetSpellName(spellId)
        print(string.format("|cff00ccffWowAgent learn:|r %s %s (id=%d)",
            event == "UNIT_SPELLCAST_START" and "START" or "DONE", name or "?", spellId))
    end
end)

SLASH_WOWAGENT1 = "/wagent"
SlashCmdList["WOWAGENT"] = function(msg)
    msg = (msg or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")
    if msg == "" and WowAgent_TogglePanel then
        WowAgent_TogglePanel()
    elseif msg == "combat" or msg:match("^combat%s") then
        if WowAgent.CombatCommand then
            WowAgent.CombatCommand(msg:sub(7):gsub("^%s+", ""))
        else
            print("WowAgent: restart the client to load the combat module")
        end
    elseif msg == "quest" or msg:match("^quest%s") then
        if WowAgent.QuestCommand then
            WowAgent.QuestCommand(msg:sub(6):gsub("^%s+", ""))
        else
            print("WowAgent: quest telemetry is unavailable on this client")
        end
    elseif msg == "debug" then
        WowAgent.debugVisible = not WowAgent.debugVisible
        print("WowAgent HUD: " .. (WowAgent.debugVisible and "on" or "off"))
    elseif msg == "learn on" or msg == "learn" then
        learnMode = true
        print("|cff00ff00WowAgent learn mode ON|r — cast/gather once, copy the spellIDs into WowAgent.TRACKED_SPELLS")
    elseif msg == "learn off" then
        learnMode = false
        print("WowAgent learn mode off")
    elseif msg == "unlock" then
        WowAgent.SetMovable(true)
        print("WowAgent: drag the channel frame, position saves on release")
    elseif msg == "lock" then
        WowAgent.SetMovable(false)
        print("WowAgent: channel locked")
    elseif msg == "pos" then
        local _, p = WowAgent.BuildPos()
        print("WowAgent raw pos value: " .. tostring(p))
    else
        print("WowAgent: /wagent debug | learn on/off | unlock | lock | pos | quest")
    end
end

print("|cff00ff00WowAgent 已加载。|r 点击游戏左上角 WowAgent，或输入 /wagent 打开面板。")
