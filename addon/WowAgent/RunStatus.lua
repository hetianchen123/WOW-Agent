local status = {}
WowAgent.RunStatus = status

local labels = {
    "脚本运行中 · 核对任务",
    "正在寻路",
    "正在找怪",
    "正在战斗",
    "正在拾取尸体",
    "正在找 NPC",
    "正在处理任务对话",
    "正在等待游戏反馈",
    "正在战斗 · 脱战后拾取",
    "正在核对楼层与出口",
    "正在等待 StartLux 决策",
    "正在核对位置与朝向",
    "正在等待任务阶段回执",
    "正在核对镜头角度",
    "正在撤离 · 等待脱战",
}
local taskStages = {"接取任务", "执行目标", "准备交付", "已交付"}
local serviceStages = {
    "前往训练师", "训练资金不足", "找训练师对话", "学习技能", "核对成长回执",
    "分配天赋", "更新战斗宏", "已学技能", "已分配天赋", "已更新战斗宏",
    "训练核对完成", "成长步骤未确认", "核对训练选项", "等待训练窗口",
    "更换装备", "核对装备回执", "已更换装备", "换装未确认",
    "前往修理商", "找修理商对话", "修理装备", "核对修理回执",
    "已修理", "修理资金不足", "修理未确认", "关闭修理窗口", "修理资金不足",
    "出售闲置物品", "核对售卖回执", "已收到售卖收入",
}
local secret = issecretvalue or function() return false end

local panel = CreateFrame("Frame", nil, UIParent)
panel:SetSize(390, 50)
panel:SetPoint("TOP", UIParent, "TOP", 0, -50)
panel:SetFrameStrata("HIGH")
local background = panel:CreateTexture(nil, "BACKGROUND")
background:SetAllPoints()
background:SetColorTexture(.035, .04, .05, .84)
local message = panel:CreateFontString(nil, "OVERLAY", "GameFontNormal")
message:SetPoint("TOP", 0, -28)
local taskText = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
taskText:SetPoint("TOP", 0, -8)
taskText:SetWidth(372)
taskText:SetWordWrap(false)
local updatedAt, prefixAt, prefixCount, received, receiptEpoch, lastQuest, questTitle
receiptEpoch = 0

local function receive(state)
    received, receiptEpoch = state, (receiptEpoch + 1) % 64
    updatedAt = GetTime()
    message:SetText("WowAgent · " .. labels[state])
end

panel:SetScript("OnUpdate", function()
    if prefixAt and GetTime() - prefixAt > .9 then
        prefixAt, prefixCount = nil, nil
    end
    if updatedAt and GetTime() - updatedAt >= 4 then
        updatedAt = nil
        message:SetText("WowAgent · 主控未连接")
    end
end)
panel:Hide()

for code = 1, 6 do
    local symbol = code
    local button = CreateFrame("Button", "WowAgentRunStatus" .. code, UIParent)
    button:RegisterForClicks("LeftButtonUp")
    button:SetScript("OnClick", function()
        if not panel:IsShown() then return end
        local now = GetTime()
        if symbol == 6 then
            prefixCount = prefixAt and now - prefixAt <= .9 and prefixCount + 1 or 1
            prefixAt = now
            return
        end
        local state = symbol
        if prefixAt and now - prefixAt <= .9 then
            if prefixCount == 1 then
                state = symbol + 5
            elseif prefixCount == 2 then
                state = symbol + 10
            else
                state = nil
            end
        end
        prefixAt, prefixCount = nil, nil
        if state then receive(state) end
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
    for code = 1, 6 do
        if not bindings.ClickAvailable("run_status_" .. code, false) then
            message:SetText("WowAgent · 状态快捷键冲突")
            panel:Show()
            return
        end
    end
    for code = 1, 6 do bindings.BindClick(owner, "run_status_" .. code, false) end
    updatedAt, prefixAt, prefixCount, received = nil, nil, nil, nil
    taskText:SetText("")
    message:SetText("WowAgent · 等待主控连接")
    panel:Show()
end

function status.Close()
    updatedAt, prefixAt, prefixCount, received = nil, nil, nil, nil
    taskText:SetText("")
    panel:Hide()
end

function status.AwaitBindings()
    updatedAt, prefixAt, prefixCount, received = nil, nil, nil, nil
    taskText:SetText("")
    message:SetText("WowAgent · 等待动作栏可配置")
    panel:Show()
end

function status.Publish(sequence, prepared, quest, phase, progress, service)
    local ready = prepared and panel:IsShown() and not (ChatEdit_GetActiveWindow and ChatEdit_GetActiveWindow())
    if ready then
        for code = 1, 6 do
            if not WowAgent.TaskBindings.ClickReady("run_status_" .. code) then ready = false; break end
        end
    end
    if panel:IsShown() and quest and quest > 0 then
        if quest ~= lastQuest or not questTitle then
            local title = C_QuestLog and C_QuestLog.GetTitleForQuestID and C_QuestLog.GetTitleForQuestID(quest)
            questTitle = not secret(title) and type(title) == "string" and title ~= "" and title or nil
            lastQuest = quest
        end
        local required = progress and progress % 1024 or 0
        local count = required > 0 and required < 1000 and string.format("  %d/%d", math.floor(progress / 1024), required) or ""
        local serviceLabel = serviceStages[service]
        local context = serviceLabel and "  ·  " .. serviceLabel or ""
        taskText:SetText(string.format("#%d %s  ·  %s%s%s", quest, questTitle or "任务",
            taskStages[phase] or "核对任务", count, context))
    end
    -- Receipt 1 is unused by the original labels and fits in the same four bits.
    local code = received == 15 and 1 or (received and received + 1 or 0)
    status.write(7, sequence * 8192 + 6144 + receiptEpoch * 32 + code * 2
        + (ready and 1 or 0))
end
