-- Temporary visual cleanup. Keep frames shown and action bindings intact.
-- Only explicitly selected HUD roots are touched; never hide UIParent.
local ui = {}
WowAgent.VisualUI = ui
local saved, active, pendingRestore = {}, false, false
local defaults = {bars = true, tracker = false, chat = false, portraits = false}
local roots = {
    bars = {"MainActionBar", "MainMenuBarArtFrame", "MultiBarBottomLeft", "MultiBarBottomRight",
        "MultiBarRight", "MultiBarLeft", "MultiBar5", "MultiBar6", "MultiBar7", "PetActionBar",
        "PetActionBarFrame", "StanceBar", "StanceBarFrame", "StatusTrackingBarManager",
        "MainStatusTrackingBarContainer", "SecondaryStatusTrackingBarContainer"},
    tracker = {"ObjectiveTrackerFrame", "WatchFrame"},
    chat = {"GeneralDockManager", "ChatFrameMenuButton", "ChatFrameChannelButton", "QuickJoinToastButton"},
    portraits = {"PlayerFrame", "TargetFrame", "FocusFrame", "PetFrame"},
}
for i = 1, 12 do
    for _, prefix in ipairs({"BT4Bar", "DominosActionBar", "ElvUI_Bar"}) do
        roots.bars[#roots.bars + 1] = prefix .. i
    end
    -- Individual native buttons also cover older MainMenuBar arrangements.
    roots.bars[#roots.bars + 1] = "ActionButton" .. i
end
for i = 1, 10 do
    for _, suffix in ipairs({"", "Tab", "ButtonFrame"}) do
        roots.chat[#roots.chat + 1] = "ChatFrame" .. i .. suffix
    end
end

local function Report(text)
    (WowAgent.Report or print)("WowAgent：" .. text)
end

local function InCombat()
    return InCombatLockdown and InCombatLockdown()
end

local function Read(frame, method)
    if not frame[method] then return end
    local ok, value = pcall(frame[method], frame)
    if ok and not (issecretvalue and issecretvalue(value)) then return value end
end

local function Write(frame, method, value)
    if value == nil or not frame[method] then return true end
    return pcall(frame[method], frame, value)
end

function ui.GetOptions()
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.visualUI = WowAgentDB.visualUI or {}
    local result = {}
    for key, default in pairs(defaults) do
        local value = WowAgentDB.visualUI[key]
        if type(value) == "boolean" then result[key] = value else result[key] = default end
    end
    return result
end

function ui.SetOption(key, value)
    if defaults[key] == nil then return false end
    ui.GetOptions()
    WowAgentDB.visualUI[key] = value == true
    return true
end

local function RestoreFrame(frame, state)
    local ok = Write(frame, "SetAlpha", state.alpha)
    for _, pair in ipairs({{"clicks", "SetMouseClickEnabled"}, {"motion", "EnableMouseMotion"},
                           {"mouse", "EnableMouse"}}) do
        if not Write(frame, pair[2], state[pair[1]]) then ok = false end
    end
    return ok
end

function ui.Restore(quiet)
    if InCombat() then
        pendingRestore = true
        if not quiet then Report("恢复界面已排队，脱战后自动执行。") end
        return false
    end
    pendingRestore, active = false, false
    local failures = 0
    for frame, state in pairs(saved) do
        if RestoreFrame(frame, state) then saved[frame] = nil else failures = failures + 1 end
    end
    if not quiet then
        Report(failures == 0 and "界面已恢复。" or "部分界面未能恢复，请再次恢复或 /reload。")
    end
    return failures == 0
end

local function Conceal(frame, seen)
    if not frame or seen[frame] or Read(frame, "IsForbidden") then return true end
    seen[frame] = true
    local state = saved[frame]
    if not state then
        local alpha = Read(frame, "GetAlpha")
        if type(alpha) ~= "number" or alpha ~= alpha or alpha < 0 or alpha > 1 then return true end
        state = {alpha = alpha}
        if frame.SetMouseClickEnabled and frame.EnableMouseMotion then
            state.clicks = Read(frame, "IsMouseClickEnabled")
            state.motion = Read(frame, "IsMouseMotionEnabled")
        elseif frame.EnableMouse then
            state.mouse = Read(frame, "IsMouseEnabled")
        end
        saved[frame] = state
    end
    if not Write(frame, "SetAlpha", 0) then return false end
    if state.clicks ~= nil and not Write(frame, "SetMouseClickEnabled", false) then return false end
    if state.motion ~= nil and not Write(frame, "EnableMouseMotion", false) then return false end
    if state.mouse ~= nil and not Write(frame, "EnableMouse", false) then return false end
    if frame.GetChildren then
        local children = {pcall(frame.GetChildren, frame)}
        if not children[1] then return false end
        for i = 2, #children do
            if not Conceal(children[i], seen) then return false end
        end
    end
    return true
end

local currentOptions
local function Refresh()
    if not active or InCombat() then return end
    local seen = {}
    for group, names in pairs(roots) do
        if currentOptions[group] then
            for _, name in ipairs(names) do
                if not Conceal(_G[name], seen) then
                    ui.Restore(true)
                    Report("客户端拒绝修改界面，已尝试恢复；请检查后重试。")
                    return false
                end
            end
        end
    end
    return true
end

function ui.Apply(options)
    if InCombat() then Report("请脱战后隐藏界面。"); return false end
    if not ui.Restore(true) then Report("请先恢复上一轮界面状态。"); return false end
    currentOptions = {}
    for key in pairs(defaults) do currentOptions[key] = (options or ui.GetOptions())[key] == true end
    active = true
    if not Refresh() then return false end
    if next(saved) == nil then
        active = false
        Report("没有找到所选界面。第三方动作栏需使用已支持的框架名称。")
        return false
    end
    Report("所选界面已临时隐藏，快捷键保留。点击面板「恢复 UI」或输入 /wagent ui restore。")
    return true
end

function ui.Status()
    local count = 0
    for _ in pairs(saved) do count = count + 1 end
    return {active = active, pendingRestore = pendingRestore, modifiedFrames = count}
end

local function Hidden(frame, seen)
    if not frame or seen[frame] then return true end
    seen[frame] = true
    if Read(frame, "IsForbidden") then return false end
    if Read(frame, "IsShown") == false then return true end
    local state = saved[frame]
    if not state or Read(frame, "GetAlpha") ~= 0 then return false end
    if state.clicks ~= nil and Read(frame, "IsMouseClickEnabled") ~= false then return false end
    if state.motion ~= nil and Read(frame, "IsMouseMotionEnabled") ~= false then return false end
    if state.mouse ~= nil and Read(frame, "IsMouseEnabled") ~= false then return false end
    if frame.GetChildren then
        local children = {pcall(frame.GetChildren, frame)}
        if not children[1] then return false end
        for i = 2, #children do if not Hidden(children[i], seen) then return false end end
    end
    return true
end

function ui.HiddenMask()
    -- Read actual supported frames every telemetry tick, not the saved options
    -- or the last successful Apply. A new child / native alpha reset revokes it.
    if not active or pendingRestore then return 0 end
    local mask = 0
    for group, bit in pairs({bars=1, tracker=2, chat=4, portraits=8}) do
        if currentOptions[group] then
            local found, hidden, seen = false, true, {}
            for _, name in ipairs(roots[group]) do
                local frame = _G[name]
                if frame then
                    found = true
                    if not Hidden(frame, seen) then hidden = false end
                end
            end
            if found and hidden then mask = mask + bit end
        end
    end
    return mask
end

function ui.Command(command)
    if command == "hide" then return ui.Apply() end
    if command == "restore" or command == "show" then return ui.Restore() end
    local status = ui.Status()
    Report(status.pendingRestore and "等待脱战恢复。" or status.active and "视觉界面模式已开启。" or "视觉界面模式未开启。")
    Report("/wagent ui hide 隐藏所选 UI；/wagent ui restore 恢复。选项在「视觉界面」页。")
end

local events = CreateFrame("Frame")
events:RegisterEvent("PLAYER_REGEN_ENABLED")
events:RegisterEvent("PLAYER_ENTERING_WORLD")
events:RegisterEvent("PLAYER_LOGOUT")
events:SetScript("OnEvent", function(_, event)
    if event == "PLAYER_REGEN_ENABLED" then
        if pendingRestore then ui.Restore(true) else Refresh() end
    else ui.Restore(true) end
end)
local elapsed = 0
events:SetScript("OnUpdate", function(_, dt)
    if not active or InCombat() then return end
    elapsed = elapsed + dt
    if elapsed < 1 then return end
    elapsed = 0
    Refresh() -- Cover late-created buttons without hooking secure bar methods.
end)
