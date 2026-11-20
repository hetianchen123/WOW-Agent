local frames, bound, text, now, taskLine = {}, {}, nil, 0, nil
local conflict
function GetTime() return now end
function CreateFrame(_, name)
    local frame = {scripts = {}, shown = true}
    function frame:SetSize() end
    function frame:SetPoint() end
    function frame:SetFrameStrata() end
    function frame:RegisterForClicks() end
    function frame:RegisterEvent() end
    function frame:CreateTexture() return {SetAllPoints = function() end, SetColorTexture = function() end} end
    function frame:CreateFontString(_, _, font)
        return {SetPoint = function() end, SetWidth = function() end, SetWordWrap = function() end,
            SetText = function(_, value)
                if font == "GameFontHighlight" then taskLine = value else text = value end
            end}
    end
    function frame:SetScript(event, callback) self.scripts[event] = callback end
    function frame:Show() self.shown = true end
    function frame:Hide() self.shown = false end
    function frame:IsShown() return self.shown end
    frames[name or 'panel'] = frame
    return frame
end
local payload
C_QuestLog = {GetTitleForQuestID = function() return "法戈第矿洞" end}
WowAgent = {
    NewPixelRow = function(_, offset)
        assert(offset == -464)
        return function(kind, value) assert(kind == 7); payload = value end
    end,
    TaskBindings = {
        ClickAvailable = function(role) return role ~= conflict end,
        BindClick = function(_, role) bound[role] = true end,
        ClickReady = function(role) return bound[role] end,
    },
}
dofile("addon/WowAgent/RunStatus.lua")
local status = WowAgent.RunStatus
local normal, chat, prepared = false, nil, 0
WowAgent.LayoutSupport = {NormalBar = function() return normal end}
WowAgent.ControllerPreparationAvailable = function() return true end
WowAgent.PrepareAutomaticQuestCommand = function() prepared = prepared + 1 end
ChatEdit_GetActiveWindow = function() return chat end
local prepare = frames.WowAgentControllerPrepare
assert(not status.PrepareKeyReady())
prepare.scripts.OnEvent()
prepare.scripts.OnClick()
assert(prepared == 0)
normal = true
assert(status.PrepareKeyReady())
chat = {}
prepare.scripts.OnClick()
assert(prepared == 0)
chat = nil
prepare.scripts.OnClick()
prepare.scripts.OnClick()
assert(prepared == 1)
bound.controller_prepare = nil
status.Register({})
conflict = "run_status_3"
status.Prepare({})
assert(next(bound) == nil)
status.Publish(5, true)
assert(payload == 5 * 8192 + 6144)
conflict = nil
status.Prepare({})
status.Publish(5, true, 62, 2, 1)
assert(payload == 5 * 8192 + 6145)
assert(taskLine:find("#62 法戈第矿洞", 1, true) and taskLine:find("0/1", 1, true))
status.Publish(5, true, 62, 2, 1, 1)
assert(taskLine:find("前往训练师", 1, true))
frames.WowAgentRunStatus3.scripts.OnClick()
assert(text:find("正在找怪", 1, true))
status.Publish(5, true)
assert(payload == 5 * 8192 + 6145 + 32 + 4 * 2)
now = 4.1
frames.panel.scripts.OnUpdate()
assert(text:find("未连接", 1, true))
assert(not frames.WowAgentRunStatus0 and not frames.WowAgentRunStatus7)
frames.WowAgentRunStatus6.scripts.OnClick()
now = 4.3
frames.WowAgentRunStatus4.scripts.OnClick()
assert(text:find("脱战后拾取", 1, true))
frames.WowAgentRunStatus6.scripts.OnClick()
now = 4.5
frames.WowAgentRunStatus5.scripts.OnClick()
assert(text:find("楼层与出口", 1, true))
frames.WowAgentRunStatus6.scripts.OnClick()
now = 4.7
frames.WowAgentRunStatus6.scripts.OnClick()
now = 4.9
frames.WowAgentRunStatus4.scripts.OnClick()
assert(text:find("镜头角度", 1, true))
frames.WowAgentRunStatus6.scripts.OnClick()
now = 9
frames.panel.scripts.OnUpdate()
assert(text:find("未连接", 1, true))
status.Close()
status.Publish(6, true)
assert(payload == 6 * 8192 + 6144 + 4 * 32)
status.Prepare({})
frames.WowAgentRunStatus6.scripts.OnClick()
frames.WowAgentRunStatus6.scripts.OnClick()
frames.WowAgentRunStatus5.scripts.OnClick()
assert(text:find("正在撤离", 1, true))
status.Publish(6, true)
assert(payload == 6 * 8192 + 6144 + 5 * 32 + 2 + 1)
