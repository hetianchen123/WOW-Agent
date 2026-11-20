local frames, bound, text, now = {}, {}, nil, 0
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
    function frame:CreateFontString()
        return {SetPoint = function() end, SetText = function(_, value) text = value end}
    end
    function frame:SetScript(event, callback) self.scripts[event] = callback end
    function frame:Show() self.shown = true end
    function frame:Hide() self.shown = false end
    function frame:IsShown() return self.shown end
    frames[name or 'panel'] = frame
    return frame
end
local payload
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
assert(payload == 5 * 8192 + 4096)
conflict = nil
status.Prepare({})
status.Publish(5, true)
assert(payload == 5 * 8192 + 4097)
frames.WowAgentRunStatus3.scripts.OnClick()
assert(text:find("正在找怪", 1, true))
status.Publish(5, true)
assert(payload == 5 * 8192 + 4097 + 32 + 4 * 2)
now = 4.1
frames.panel.scripts.OnUpdate()
assert(text:find("未连接", 1, true))
frames.WowAgentRunStatus7.scripts.OnClick()
frames.WowAgentRunStatus2.scripts.OnClick()
assert(text:find("脱战后拾取", 1, true))
status.Close()
status.Publish(6, true)
assert(payload == 6 * 8192 + 4096 + 2 * 32)
