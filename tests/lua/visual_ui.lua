-- Reversible display changes, with shown/action state independent of alpha.
local events, combat, messages = {}, false, {}
local function frame(alpha, clicks, motion, children)
    local f = {alpha = alpha or 1, clicks = clicks ~= false, motion = motion ~= false,
               shown = true, children = children or {}, scripts = {}, action = 42}
    function f:GetAlpha() return self.alpha end
    function f:IsShown() return self.shown end
    function f:SetAlpha(value) if self.fail then error("blocked") end; self.alpha = value end
    function f:IsMouseClickEnabled() return self.clicks end
    function f:SetMouseClickEnabled(value) self.clicks = value end
    function f:IsMouseMotionEnabled() return self.motion end
    function f:EnableMouseMotion(value) self.motion = value end
    function f:GetChildren() return unpack(self.children) end
    function f:RegisterEvent() end
    function f:SetScript(name, fn) self.scripts[name] = fn end
    function f:Hide() error("must not hide secure action frames") end
    function f:Show() error("must not change shown state") end
    function f:SetAttribute() error("must not change actions") end
    return f
end
function CreateFrame() local f = frame(); events[#events+1] = f; return f end
function InCombatLockdown() return combat end
function SetBinding() error("must not change bindings") end
WowAgent = {Report = function(msg) messages[#messages+1] = msg end}
WowAgentDB = {}
local child = frame(.6, true, false)
MainActionBar = frame(.8, false, false, {child})
MultiBarRight, ObjectiveTrackerFrame, ChatFrame1 = frame(.5), frame(.7), frame(.9)
WowAgentChannelFrame, Minimap, QuestFrame, StaticPopup1, WowAgentPanelLauncher = frame(), frame(), frame(), frame(), frame()
dofile("addon/WowAgent/VisualUI.lua")
local ui, driver = WowAgent.VisualUI, events[#events]
assert(ui.GetOptions().bars and not ui.GetOptions().chat)
assert(ui.HiddenMask() == 0)
ui.SetOption("bars", false); assert(not ui.GetOptions().bars)
ui.SetOption("bars", true); ui.SetOption("chat", true)
assert(ui.Apply())
assert(MainActionBar.alpha == 0 and child.alpha == 0 and MultiBarRight.alpha == 0)
assert(not child.clicks and not child.motion and MainActionBar.shown and child.action == 42)
assert(ChatFrame1.alpha == 0 and ObjectiveTrackerFrame.alpha == .7)
assert(ui.HiddenMask() == 5)
-- Options alone are not visibility receipts. Native resets revoke a group;
-- the periodic refresh can reclaim it only after applying the change again.
ui.SetOption("tracker", true)
assert(ui.HiddenMask() == 5 and ObjectiveTrackerFrame.alpha == .7)
ui.SetOption("tracker", false)
child.alpha = .4
assert(ui.HiddenMask() == 4)
driver.scripts.OnUpdate(driver, 1)
assert(ui.HiddenMask() == 5)
child.clicks = true
assert(ui.HiddenMask() == 4)
driver.scripts.OnUpdate(driver, 1)
assert(ui.HiddenMask() == 5)
for _, f in ipairs({WowAgentChannelFrame, Minimap, QuestFrame, StaticPopup1, WowAgentPanelLauncher}) do
    assert(f.alpha == 1 and f.clicks)
end
-- Reapply must retain the first original values, not snapshot hidden values.
assert(ui.Apply()); assert(ui.Restore())
assert(MainActionBar.alpha == .8 and child.alpha == .6 and child.clicks and not child.motion)
assert(not MainActionBar.clicks and not MainActionBar.motion)
assert(ChatFrame1.alpha == .9 and ui.Status().modifiedFrames == 0)
assert(ui.HiddenMask() == 0)
assert(ui.Restore())

-- Combat application has no effect; restore queues without touching secure frames.
combat = true
assert(not ui.Apply() and MainActionBar.alpha == .8)
combat = false
assert(ui.Apply())
combat = true
assert(not ui.Restore() and ui.Status().pendingRestore and child.alpha == 0)
assert(ui.HiddenMask() == 0)
driver.scripts.OnUpdate(driver, 2)
assert(child.alpha == 0)
combat = false
driver.scripts.OnEvent(driver, "PLAYER_REGEN_ENABLED")
assert(child.alpha == .6 and not ui.Status().pendingRestore and not ui.Status().active)

-- Late-created supported bar/button is restored to its own original state.
assert(ui.Apply())
BT4Bar1 = frame(.4, false, true)
local late = frame(.3, true, false)
MainActionBar.children[#MainActionBar.children+1] = late
assert(ui.HiddenMask() == 4)
driver.scripts.OnUpdate(driver, 1)
assert(BT4Bar1.alpha == 0 and late.alpha == 0)
assert(ui.HiddenMask() == 5)
driver.scripts.OnEvent(driver, "PLAYER_ENTERING_WORLD")
assert(BT4Bar1.alpha == .4 and not BT4Bar1.clicks and BT4Bar1.motion)
assert(late.alpha == .3 and late.clicks and not late.motion)
assert(not ui.Status().active)

-- A setter failure rolls back previous mutations; never report success.
MultiBarRight.fail = true
assert(not ui.Apply())
assert(MainActionBar.alpha == .8 and child.alpha == .6 and ChatFrame1.alpha == .9)
MultiBarRight.fail = false
assert(ui.Restore())

-- False options select nothing and must not be interpreted as default true.
assert(not ui.Apply({bars=false,chat=false,tracker=false,portraits=false}))
assert(not ui.Status().active)
ui.Command("hide"); assert(ui.Status().active)
ui.Command("restore"); assert(not ui.Status().active)
assert(ui.Apply({tracker=true}))
assert(ObjectiveTrackerFrame.alpha == 0 and MainActionBar.alpha == .8)
assert(ui.HiddenMask() == 2)
driver.scripts.OnEvent(driver, "PLAYER_LOGOUT")
assert(ObjectiveTrackerFrame.alpha == .7)
-- SavedVariables contain options only, never the hidden session or frame handles.
for key in pairs(WowAgentDB.visualUI) do assert(key == "bars" or key == "chat" or key == "tracker") end
