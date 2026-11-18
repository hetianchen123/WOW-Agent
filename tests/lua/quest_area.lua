WowAgent, UIParent = {}, {}
local now, draws, fail = 0, 0, false
function GetTime() return now end
function CreateFrame(kind)
    assert(kind == "QuestPOI")
    return {
        SetSize = function() end, SetPoint = function() end, SetAlpha = function() end, EnableMouse = function() end,
        SetMapID = function(_, map) assert(map == 2451) end,
        DrawNone = function() end,
        DrawBlob = function(_, id) assert(id == 90886); draws = draws + 1 end,
        UpdateMouseOverTooltip = function(_, x, y)
            if fail then error("unavailable") end
            -- An asymmetric quest area: never include points to the west.
            if x > .5 and (x-.5)^2 + (y-.5)^2 < .1^2 then return 90886 end
        end,
    }
end
dofile("addon/WowAgent/QuestArea.lua")
local mask = WowAgent.QuestAreaMask(90886, 2451, .5, .5, 1000, 1000)
assert(mask and #mask == 5)
local index, count = 0, 0
for y = -4, 4 do for x = -4, 4 do if x ~= 0 or y ~= 0 then
    local bit = math.floor(mask[math.floor(index/16)+1] / 2^(index%16)) % 2
    assert((bit == 1) == (x > 0 and (x*.03)^2 + (y*.03)^2 < .1^2))
    count = count + bit
    index = index + 1
end end end
assert(count > 0)
WowAgent.QuestAreaMask(90886, 2451, .5, .5, 1000, 1000)
assert(draws == 1)
fail, now = true, 6
assert(WowAgent.QuestAreaMask(90886, 2451, .5, .5, 1000, 1000) == nil)
