-- Optional UTF-8 quest context. The main navigation frame carries identity and progress.
local rows, frame = {}, CreateFrame("Frame", nil, UIParent)
frame:SetSize(142, 192)
frame:SetPoint("BOTTOMLEFT", WowAgentChannelFrame, "TOPLEFT", 0, 288)
frame:SetFrameStrata("TOOLTIP")
frame:Hide()
if WowAgent.RoadScan then WowAgent.RoadScan.RegisterStrip(frame) end
for index = 0, 11 do rows[index] = WowAgent.NewPixelRow(frame, index * 16) end

local previous, page, hash = nil, 0, 0
local descriptions = {}
local function Public(value)
    if not issecretvalue or not issecretvalue(value) then return value end
end

local function Field(value, maxBytes)
    value = Public(value)
    if type(value) ~= "string" then return "" end
    value = value:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("[%z\1-\31\127]", " ")
    local last = math.min(maxBytes, #value)
    while last > 0 do
        local nextByte = value:byte(last + 1)
        if not nextByte or nextByte < 128 or nextByte > 191 then break end
        last = last - 1
    end
    return value:sub(1, last)
end

function WowAgent.RememberQuestDescription(id)
    if not GetQuestText or not GetQuestID or Public(GetQuestID()) ~= id then return end
    local ok, text = pcall(GetQuestText)
    text = ok and Field(text, 120) or ""
    if text ~= "" then descriptions[id] = text end
end

local function QuestDescription(id)
    if descriptions[id] ~= nil then return descriptions[id] end
    if not GetQuestLogQuestText or not C_QuestLog.GetSelectedQuest then return "" end
    local selected = Public(C_QuestLog.GetSelectedQuest())
    if selected ~= id then
        if type(selected) ~= "number" or not C_QuestLog.SetSelectedQuest
            or (InCombatLockdown and InCombatLockdown())
            or (WorldMapFrame and WorldMapFrame:IsShown())
            or (QuestLogFrame and QuestLogFrame:IsShown()) then return "" end
        pcall(C_QuestLog.SetSelectedQuest, id)
    end
    local description = ""
    if Public(C_QuestLog.GetSelectedQuest()) == id then
        local ok, text = pcall(GetQuestLogQuestText)
        if ok then description = Field(text, 120) end
    end
    if selected ~= id then pcall(C_QuestLog.SetSelectedQuest, selected) end
    descriptions[id] = description
    return description
end

function WowAgent.HideQuestText()
    frame:Hide()
    previous = nil
end

function WowAgent.WriteQuestText(sequence, id, objective, ordinal, verifiedMode)
    if not id or id <= 0 or objective == nil or not ordinal or ordinal < 1 or ordinal > 31 then
        return WowAgent.HideQuestText()
    end
    local title = C_QuestLog.GetTitleForQuestID and C_QuestLog.GetTitleForQuestID(id)
    local payload = table.concat({Field(title, 60), Field(objective.type, 20), Field(objective.text, 176),
                                 QuestDescription(id)}, string.char(31))
    if payload ~= previous then
        previous, page, hash = payload, 0, 0
        for index = 1, #payload do hash = (hash * 31 + payload:byte(index)) % 1048576 end
    else
        page = (page + 1) % math.ceil(#payload / 16)
    end
    rows[0](0, 0xC71 + sequence * 4096)
    rows[1](1, id)
    rows[2](2, #payload + page * 512 + ordinal * 16384 + (verifiedMode and 524288 or 0))
    rows[3](3, hash)
    for index = 0, 7 do
        local a, b = payload:byte(page * 16 + 2 * index + 1, page * 16 + 2 * index + 2)
        rows[index + 4](index + 4, (a or 0) + (b or 0) * 256 + (sequence % 16) * 65536)
    end
    frame:Show()
end
