local status = {}
WowAgent.RunStatus = status

local labels = {
    [0] = "脚本已停止",
    "脚本运行中 · 核对任务",
    "正在寻路",
    "正在找怪",
    "正在战斗",
    "正在拾取尸体",
    "正在找 NPC",
    "正在处理任务对话",
    "正在等待游戏反馈",
    "正在战斗 · 脱战后拾取",
}

local panel = CreateFrame("Frame", nil, UIParent)
panel:SetSize(260, 28)
panel:SetPoint("TOP", UIParent, "TOP", 0, -50)
panel:SetFrameStrata("HIGH")
local background = panel:CreateTexture(nil, "BACKGROUND")
background:SetAllPoints()
background:SetColorTexture(.035, .04, .05, .84)
local message = panel:CreateFontString(nil, "OVERLAY", "GameFontNormal")
message:SetPoint("CENTER")
local updatedAt, prefixAt, received, receiptEpoch
receiptEpoch = 0
panel:SetScript("OnUpdate", function()
    if updatedAt and GetTime() - updatedAt >= 4 then
        updatedAt = nil
        message:SetText("WowAgent · 主控未连接")
    end
end)
panel:Hide()

for code = 0, 7 do
    local symbol = code
    local button = CreateFrame("Button", "WowAgentRunStatus" .. code, UIParent)
    button:RegisterForClicks("LeftButtonUp")
    button:SetScript("OnClick", function()
        if not panel:IsShown() then return end
        local now = GetTime()
        if symbol == 7 then prefixAt = now; return end
        local state = prefixAt and now - prefixAt <= .5 and symbol + 7 or symbol
        prefixAt = nil
        if not labels[state] then return end
        received, receiptEpoch = state, (receiptEpoch + 1) % 128
        updatedAt = state ~= 0 and GetTime() or nil
        message:SetText("WowAgent · " .. labels[state])
    end)
end

local prepare = CreateFrame("Button", "WowAgentControllerPrepare", UIParent)
prepare:RegisterForClicks("AnyDown", "AnyUp")
local lastPrepare

function status.PrepareKeyReady()
    local support = WowAgent.LayoutSupport
    return support and support.NormalBar and support.NormalBar() and WowAgent.PrepareAutomaticQuestCommand
        and WowAgent.ControllerPreparationAvailable and WowAgent.ControllerPreparationAvailable()
        and (not ChatEdit_GetActiveWindow or not ChatEdit_GetActiveWindow())
        and WowAgent.TaskBindings.ClickReady("controller_prepare")
end

prepare:SetScript("OnClick", function()
    local now = GetTime()
    if status.PrepareKeyReady() and (not lastPrepare or now - lastPrepare >= .25) then
        lastPrepare = now
        WowAgent.PrepareAutomaticQuestCommand()
    end
end)
prepare:RegisterEvent("PLAYER_ENTERING_WORLD")
prepare:RegisterEvent("PLAYER_REGEN_ENABLED")
prepare:SetScript("OnEvent", function()
    local bindings = WowAgent.TaskBindings
    if not bindings.ClickReady("controller_prepare") then
        bindings.BindClick(prepare, "controller_prepare", false)
    end
end)

function status.Register(frame)
    status.write = WowAgent.NewPixelRow(frame, -464)
end

function status.Prepare(owner)
    local bindings = WowAgent.TaskBindings
    for code = 0, 7 do
        if not bindings.ClickAvailable("run_status_" .. code, false) then
            message:SetText("WowAgent · 状态快捷键冲突")
            panel:Show()
            return
        end
    end
    for code = 0, 7 do bindings.BindClick(owner, "run_status_" .. code, false) end
    updatedAt, prefixAt, received = nil, nil, nil
    message:SetText("WowAgent · 等待主控连接")
    panel:Show()
end

function status.Close()
    updatedAt, prefixAt, received = nil, nil, nil
    panel:Hide()
end

function status.AwaitBindings()
    updatedAt, prefixAt, received = nil, nil, nil
    message:SetText("WowAgent · 等待动作栏可配置")
    panel:Show()
end

function status.Publish(sequence, prepared)
    local ready = prepared and panel:IsShown() and not (ChatEdit_GetActiveWindow and ChatEdit_GetActiveWindow())
    if ready then
        for code = 0, 7 do
            if not WowAgent.TaskBindings.ClickReady("run_status_" .. code) then ready = false; break end
        end
    end
    status.write(7, sequence * 8192 + 4096 + receiptEpoch * 32 + (received and (received + 1) * 2 or 0)
        + (ready and 1 or 0))
end
