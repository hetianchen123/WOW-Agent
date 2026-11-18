-- Query the native quest blob without moving the mouse or opening the map.
local probe, cache
local secret = issecretvalue or function() return false end
local function Public(value) if not secret(value) then return value end end

function WowAgent.QuestAreaMask(id, map, x, y, width, height)
    if not x or not y or width <= 0 or height <= 0 then return end
    local now = GetTime()
    if cache and cache.id == id and cache.map == map and cache.x == x and cache.y == y
        and now - cache.at < 5 then return cache.mask end
    local ok, mask = pcall(function()
        if not probe then
            probe = CreateFrame("QuestPOI", nil, UIParent)
            probe:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
            probe:SetAlpha(0)
            probe:EnableMouse(false)
        end
        probe:SetSize(1000, 1000 * height / width)
        probe:SetMapID(map)
        probe:DrawNone()
        probe:DrawBlob(id, true)
        local bits, count, index = {0, 0, 0, 0, 0}, 0, 0
        -- Eighty points cover a 240-yard square, keeping the true blob boundary.
        for row = -4, 4 do
            for col = -4, 4 do
                if col ~= 0 or row ~= 0 then
                    local px, py = x + col * 30 / width, y + row * 30 / height
                    if px >= 0 and px <= 1 and py >= 0 and py <= 1 then
                        local found = Public(probe:UpdateMouseOverTooltip(px, py))
                        if found == id then
                            local word = math.floor(index / 16) + 1
                            bits[word] = bits[word] + 2^(index % 16)
                            count = count + 1
                        end
                    end
                    index = index + 1
                end
            end
        end
        return count > 0 and bits or nil
    end)
    cache = {id = id, map = map, x = x, y = y, at = now, mask = ok and mask or nil}
    return cache.mask
end
