import re
from pathlib import Path

import pytest

from bot.perception.travel_channel import CAMERA_KEYS, DESCENT_KEYS, RECOVERY_DIALOG_KEYS, SKY_KEYS


@pytest.mark.parametrize("forever, f4_action", [(False, "camera_orbit"), (True, "forever_progress")])
def test_binding_report_is_read_only_and_uses_helper_keys(forever, f4_action):
    lua = pytest.importorskip("lupa.lua51").LuaRuntime()
    report = []
    lua.globals().print = report.append
    lua.execute("""
        function GetBindingAction(key, effective)
            if key == "F6" and not effective then return "SITORSTAND" end
            return ""
        end
        function SetOverrideBinding() error("read-only report") end
        function SetOverrideBindingClick() error("read-only report") end
        WowAgent = {Forever = (...) and {} or nil}
        dofile("addon/WowAgent/TaskBindings.lua")
        WowAgent.TaskBindings.Report()
    """, forever)
    helper = Path("macos/KeyboardMouse.swift").read_text()
    physical_keys = set(re.findall(r'"([a-z0-9]+)":\s*\d+', helper))
    declared = [row.split()[1].lower() for row in report]
    assert len(declared) == len(set(declared))
    wire_keys = set(DESCENT_KEYS.values()) | set(SKY_KEYS[1:]) | set(CAMERA_KEYS[1:]) | set(RECOVERY_DIALOG_KEYS[1:])
    status_keys = {f"f{key}" for key in range(13, 21)}
    assert "[105, 107, 113, 106, 64, 79, 80, 90]" in helper
    assert all(flag in helper for flag in (".maskControl", ".maskAlternate", ".maskShift"))
    assert (set(declared) | wire_keys) <= physical_keys | status_keys | {'ctrl-alt-shift-f5'}
    assert [row.split()[2] for row in report if row.startswith("WowAgent F4 ")] == [f4_action]
    assert any(row.startswith("WowAgent F6 ") and "原 SITORSTAND" in row and "检查冲突" in row for row in report)


def test_click_binding_checks_base_layer_and_rechecks_empty_slot():
    lua = pytest.importorskip("lupa.lua51").LuaRuntime()
    lua.execute("""
        local base, override, filled = "SITORSTAND", "", false
        function GetBindingAction(key, effective)
            return key == "F6" and (effective and override or base) or ""
        end
        function InCombatLockdown() return false end
        function SetOverrideBindingClick(_, _, _, name, button)
            override = "CLICK " .. name .. ":" .. button
        end
        function HasAction(slot) assert(slot == 47); return filled end
        MultiBarBottomRightButton11 = {action = 47}
        dofile("addon/WowAgent/TaskBindings.lua")
        local bindings = WowAgent.TaskBindings
        assert(not bindings.BindClick({}, "camera_probe", true) and override == "")
        base = "MULTIACTIONBAR2BUTTON11"
        override = base
        assert(not bindings.BindClick({}, "camera_probe"))
        filled = true
        assert(not bindings.BindClick({}, "camera_probe", true))
        filled = false
        assert(bindings.BindClick({}, "camera_probe", true) and bindings.ClickReady("camera_probe"))
        filled = true
        assert(not bindings.ClickReady("camera_probe"))
    """)


def test_duplicate_turn_can_be_borrowed_only_while_its_arrow_is_preserved():
    lua = pytest.importorskip("lupa.lua51").LuaRuntime()
    lua.execute("""
        local native = {Q = "TURNLEFT", E = "TURNRIGHT", LEFT = "TURNLEFT", RIGHT = "TURNRIGHT"}
        local overrides = {}
        function GetBindingAction(key, effective)
            return (effective and overrides[key]) or native[key] or ""
        end
        dofile("addon/WowAgent/TaskBindings.lua")
        local bindings = WowAgent.TaskBindings
        assert(bindings.NativeAvailable("Q", "STRAFELEFT"))
        assert(bindings.NativeAvailable("E", "STRAFERIGHT"))
        overrides.LEFT = "MOVEFORWARD"
        assert(not bindings.NativeAvailable("Q", "STRAFELEFT"))
        native.Q = "SITORSTAND"
        assert(not bindings.NativeAvailable("Q", "STRAFELEFT"))
    """)


def test_camera_assigns_distinct_unoccupied_keys_and_restores_on_change_or_cleanup():
    lua = pytest.importorskip("lupa.lua51").LuaRuntime()
    lua.execute("""
        local native = {F6 = "SITORSTAND", F8 = "ACTIONBUTTON5", U = "TARGETNEARESTENEMY"}
        local override = {}
        local combat = false
        local written, tag
        function CreateFrame() return {} end
        function InCombatLockdown() return combat end
        function GetBindingAction(key, effective)
            return effective and override[key] and override[key].action or native[key] or ""
        end
        function SetOverrideBindingClick(owner, _, key, name, button)
            assert(not combat and not override[key])
            override[key] = {owner = owner, action = "CLICK " .. name .. ":" .. button}
        end
        function ClearOverrideBindings(owner)
            assert(not combat)
            for key, row in pairs(override) do if row.owner == owner then override[key] = nil end end
        end
        WowAgent = {NewPixelRow = function(_, location)
            assert(location == -432)
            return function(kind, value) tag, written = kind, value end
        end}
        local owner = {}
        dofile("addon/WowAgent/TaskBindings.lua")
        local b = WowAgent.TaskBindings
        b.RegisterCameraWire({})
        assert(b.BindClick(owner, "camera_restore", true, true))
        assert(b.BindClick(owner, "camera_probe", true, true))
        assert(b.ClickKey("camera_restore") == "Y" and b.ClickKey("camera_probe") == "H")
        assert(b.Reserved("Y") and b.Reserved("H") and GetBindingAction("F6", true) == "SITORSTAND")
        b.PublishCameraKeys(9, 47, 2, 1429, false, false)
        assert(tag == 4 and math.floor(written / 8192) == 9 and written % 256 == 84)
        local generation = math.floor(written / 256) % 32

        combat = true
        native.Y = "ACTIONBUTTON3"
        b.PublishCameraKeys(10, 47, 2, 1429, false, false)
        assert(not b.ClickReady("camera_restore") and override.Y)
        assert(written % 256 == 80 and math.floor(written / 256) % 32 ~= generation)
        combat = false
        b.PublishCameraKeys(11, 47, 2, 1429, false, false)
        assert(not override.Y and GetBindingAction("Y", true) == "ACTIONBUTTON3")
        assert(b.ClickKey("camera_probe") == "H")

        b.Forget(owner)
        assert(not b.ClickReady("camera_probe") and not b.Reserved("H"))
        assert(GetBindingAction("F8", true) == "ACTIONBUTTON5")
    """)


def test_recovery_key_only_invokes_the_current_permitted_native_popup():
    lua = pytest.importorskip("lupa.lua51").LuaRuntime()
    lua.execute("""
        local frames, native, override = {}, {U = "TARGETNEARESTENEMY", Y = "MULTIACTIONBAR1BUTTON5"}, {}
        local ghost, visible, enabled, forbidden, clicks = true, true, true, false, 0
        local combat, constrained = false, false
        WowAgent = {}
        Enum = {ForbiddenAspect = {ScriptedInput = 1}}
        UIParent = {GetWidth = function() return 1600 end, GetHeight = function() return 900 end,
            GetTop = function() return 900 end, GetLeft = function() return 0 end}
        local button = {IsVisible = function() return visible end, IsEnabled = function() return enabled end,
            HasAccessConstraints = function() return constrained end,
            HasAnyForbiddenAspects = function(_, aspect) return aspect == 1 and forbidden end,
            GetCenter = function() return 800, 600 end, GetEffectiveScale = function() return 1 end,
            Click = function(_, mouse) assert(mouse == "LeftButton"); clicks = clicks + 1 end}
        UIParent.GetEffectiveScale = button.GetEffectiveScale
        local dialog = {IsVisible = button.IsVisible, GetButton1 = function() return button end}
        function StaticPopup_FindVisible(which) if which == "RECOVER_CORPSE" then return dialog end end
        function UnitIsDead() return not ghost end
        function UnitIsGhost() return ghost end
        function InCombatLockdown() return combat end
        function CreateFrame(_, name)
            local frame = {RegisterForClicks = function() end, SetScript = function(self, key, call) self[key] = call end}
            if name then frames[name] = frame end
            return frame
        end
        function GetBindingAction(key, effective)
            return effective and override[key] and override[key].action or native[key] or ""
        end
        function SetOverrideBindingClick(owner, _, key, name, click)
            assert(GetBindingAction(key, true) == "")
            override[key] = {owner = owner, action = "CLICK " .. name .. ":" .. click}
        end
        function ClearOverrideBindings(owner)
            for key, row in pairs(override) do if row.owner == owner then override[key] = nil end end
        end
        dofile("addon/WowAgent/TaskBindings.lua")
        dofile("addon/WowAgent/Recovery.lua")
        local owner = {}
        local plan = WowAgent.PrepareRecovery(owner)
        local proxy = frames.WowAgentPlayerRecovery
        local function read()
            local state = WowAgent.RecoveryState(plan, 0, function() return true end)
            return math.floor(state / 32) % 8
        end
        assert(plan.dialogKey and read() == 3) -- H, with U and Y kept intact.
        assert(WowAgent.RecoveryDialogStatus() == "not_prepared")
        assert(WowAgent.RecoveryDialogStatus({}) == "binding_unavailable")
        local status, key = WowAgent.RecoveryDialogStatus(plan)
        assert(status == "ready" and key == "H")
        proxy:OnClick("LeftButton")
        assert(clicks == 1)
        forbidden = true
        proxy:OnClick("LeftButton")
        assert(read() == 0 and clicks == 1)
        assert(WowAgent.RecoveryDialogStatus(plan) == "scripted_input_restricted")
        forbidden, enabled = false, false
        assert(read() == 0)
        assert(WowAgent.RecoveryDialogStatus(plan) == "button_unavailable")
        enabled = true
        combat = true
        assert(read() == 0 and WowAgent.RecoveryDialogStatus(plan) == "combat_lockdown")
        combat, constrained = false, true
        assert(read() == 0 and WowAgent.RecoveryDialogStatus(plan) == "access_constrained")
        constrained = false
        assert(read() == 3)
        override.H = nil
        assert(read() == 0 and WowAgent.RecoveryDialogStatus(plan) == "binding_unverified")
        ghost = false
        proxy:OnClick("LeftButton")
        assert(clicks == 1)
        assert(WowAgent.RecoveryDialogStatus(plan) == "no_death_dialog")
        WowAgent.TaskBindings.Forget(owner)
        assert(GetBindingAction("H", true) == "" and GetBindingAction("Y", true) == native.Y)

        ghost = true
        for key in ("HGBNM"):gmatch(".") do native[key] = "USER_BINDING" end
        plan = WowAgent.PrepareRecovery(owner)
        assert(plan.dialogKey and WowAgent.TaskBindings.RecoveryCode() == 8)
        assert(WowAgent.PrepareRecovery(owner).dialogKey) -- Retain a verified alternate.
        local flags = WowAgent.RecoveryState(plan, 0, function() return true end)
        assert(read() == 0 and math.floor(flags / 256) % 2 == 1)
        proxy:OnClick("LeftButton")
        assert(clicks == 2)
        WowAgent.TaskBindings.Forget(owner)
        assert(GetBindingAction("F7", true) == "")

        native.F7 = "USER_BINDING"
        assert(not WowAgent.PrepareRecovery(owner).dialogKey)
        assert(GetBindingAction("F7", true) == "USER_BINDING")
        native.F7 = nil
        assert(WowAgent.TaskBindings.BindClick(owner, "pet_mend", false))
        assert(not WowAgent.PrepareRecovery(owner).dialogKey)
        assert(WowAgent.TaskBindings.ClickKey("pet_mend") == "F7")
        WowAgent.TaskBindings.Forget(owner)
    """)
