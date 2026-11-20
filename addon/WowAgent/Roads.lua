-- User-authored travel routes. Coordinates are map-local annotations, never
-- proof of terrain clearance. This module issues no movement or bindings.
local Roads = {}
WowAgent.Roads = Roads
Roads.MAX_POINTS, Roads.MAX_ROADS = 512, 100
-- Keep the existing ground key so 0.2.70 riding routes need no data rewrite.
Roads.MODES = {
    walk = {label = "步行", marker = "步", color = {.35, 1, .45}},
    ground = {label = "骑马", marker = "骑", color = {1, .72, .12}},
    fly = {label = "飞行", marker = "飞", color = {.3, .75, 1}},
}

local function Copy(value)
    if type(value) ~= "table" then return value end
    local result = {}
    for key, item in pairs(value) do result[key] = Copy(item) end
    return result
end

local function Number(value, lo, hi)
    return not (issecretvalue and issecretvalue(value)) and type(value) == "number"
        and value == value and value >= lo and value <= hi
end

local function Text(value)
    if type(value) ~= "string" then return "" end
    return value:gsub("|", ""):gsub("%c", " "):match("^%s*(.-)%s*$")
end

function Roads.ProfileKey()
    local realm = GetRealmName and GetRealmName()
    if type(realm) == "string" and realm ~= "" then
        return tostring(WOW_PROJECT_ID or 0) .. ":" .. realm, realm
    end
end

function Roads.ProfileToken()
    local key = Roads.ProfileKey()
    if not key then return end
    local token = 0
    for i = 1, #key do token = (token * 131 + key:byte(i)) % 16777216 end
    return token
end

function Roads.Store()
    WowAgentDB = WowAgentDB or {}
    WowAgentDB.roads = WowAgentDB.roads or {schema = 1, profiles = {}}
    local root = WowAgentDB.roads
    if root.schema ~= 1 then return nil, "路线数据版本不支持，原数据已保留。" end
    -- Different realms/clients can use the same map IDs for different terrain.
    local key, realm = Roads.ProfileKey()
    realm = realm or "unknown"
    key = key or tostring(WOW_PROJECT_ID or 0) .. ":" .. realm
    root.profiles[key] = root.profiles[key] or {nextID = 1, routes = {}, realm = realm,
        project = WOW_PROJECT_ID or 0}
    return root.profiles[key]
end

function Roads.Scanned(mapID, artID)
    if not WowAgent.Forever or not artID then return {} end
    local _, build = GetBuildInfo()
    local profile = WowAgent.RoadScanData and WowAgent.RoadScanData[Roads.ProfileToken()]
    local maps = profile and profile[tonumber(build)]
    local arts = maps and maps[mapID]
    return arts and arts[artID] or {}
end

function Roads.Draft()
    local store, err = Roads.Store()
    if not store then return nil, err end
    store.draft = store.draft or {name = "", from = "", to = "", mode = "ground",
        bidirectional = true, points = {}, evidence = "manual_unverified"}
    return store.draft
end

function Roads.AddPoint(mapID, x, y, source, transition, artID)
    local draft, err = Roads.Draft()
    if not draft then return nil, err end
    if not Number(mapID, 1, 2147483647) or mapID % 1 ~= 0
        or not Number(x, 0, 1) or not Number(y, 0, 1) then
        return nil, "地图或坐标不可读，未添加路点。"
    end
    if #draft.points >= Roads.MAX_POINTS then return nil, "每条路线最多 512 个路点。" end
    local previous = draft.points[#draft.points]
    if previous and previous.mapID ~= mapID and not transition then
        return nil, "已切换地图：先启用跨图连接，再在相邻地图标入口。"
    end
    if previous and previous.mapID == mapID and previous.x == x and previous.y == y then
        return nil, "与上一个路点重复。"
    end
    local point = {mapID = mapID, x = x, y = y,
        source = source == "player_sample" and source or "map_click"}
    if Number(artID, 1, 2147483647) then point.artID = artID end
    if previous and previous.mapID ~= mapID then point.transition = "boundary" end
    draft.points[#draft.points + 1] = point
    draft.evidence = "manual_unverified"
    return point
end

function Roads.Undo()
    local draft = Roads.Draft()
    if draft then return table.remove(draft.points) end
end

function Roads.PlayerPoint(transition)
    if not (C_Map and C_Map.GetBestMapForUnit and C_Map.GetPlayerMapPosition) then
        return nil, "当前客户端不支持玩家地图坐标。"
    end
    local map = C_Map.GetBestMapForUnit("player")
    if not Number(map, 1, 2147483647) then return nil, "当前地图不可读。" end
    local pos = C_Map.GetPlayerMapPosition(map, "player")
    if not pos then return nil, "当前位置不可读。" end
    local x, y = pos:GetXY()
    return Roads.AddPoint(map, x, y, "player_sample", transition,
        C_Map.GetMapArtID and C_Map.GetMapArtID(map))
end

function Roads.Validate(route)
    if Text(route.name) == "" or Text(route.from) == "" or Text(route.to) == "" then
        return nil, "请填写路线名称、起点区域和终点区域。"
    end
    if #route.name > 180 or #route.from > 180 or #route.to > 180 then
        return nil, "名称或区域名称过长。"
    end
    if not Roads.MODES[route.mode] then return nil, "旅行方式无效。" end
    if type(route.bidirectional) ~= "boolean" then return nil, "方向属性无效。" end
    if #route.points < 2 or #route.points > Roads.MAX_POINTS then
        return nil, "路线需要 2–512 个路点。"
    end
    for index, point in ipairs(route.points) do
        if not Number(point.mapID, 1, 2147483647) or point.mapID % 1 ~= 0
            or not Number(point.x, 0, 1) or not Number(point.y, 0, 1) then
            return nil, "路线含无效地图坐标。"
        end
        local previous = route.points[index - 1]
        if previous and previous.mapID ~= point.mapID and point.transition ~= "boundary" then
            return nil, "跨地图路点缺少明确连接口。"
        end
    end
    return true
end

function Roads.Save()
    local store, err = Roads.Store()
    if not store then return nil, err end
    local draft = Roads.Draft()
    local valid; valid, err = Roads.Validate(draft)
    if not valid then return nil, err end
    if not draft.id and #store.routes >= Roads.MAX_ROADS then return nil, "路线库已满（100 条）。" end
    local route = Copy(draft)
    route.name, route.from, route.to = Text(route.name), Text(route.from), Text(route.to)
    route.evidence, route.updatedAt = "manual_unverified", time and time() or 0
    route.clientVersion = GetBuildInfo and GetBuildInfo() or "unknown"
    local index
    for i, item in ipairs(store.routes) do if item.id == route.id then index = i; break end end
    if not index then
        route.id, store.nextID = store.nextID, store.nextID + 1
        index = #store.routes + 1
    end
    store.routes[index], store.draft = route, Copy(route)
    return route
end

-- Switching/new preserves the current draft in a single recoverable slot.
function Roads.Load(id)
    local store, err = Roads.Store()
    if not store then return nil, err end
    for _, route in ipairs(store.routes) do
        if route.id == id then
            store.previousDraft, store.draft = store.draft, Copy(route)
            return store.draft
        end
    end
    return nil, "路线不存在。"
end

function Roads.New()
    local store = Roads.Store()
    if not store then return end
    store.previousDraft, store.draft = store.draft, nil
    return Roads.Draft()
end

function Roads.RestoreDraft()
    local store = Roads.Store()
    if not store or not store.previousDraft then return nil, "没有可恢复的草稿。" end
    store.draft, store.previousDraft = store.previousDraft, store.draft
    return store.draft
end

function Roads.Delete()
    local store = Roads.Store()
    if not store or not store.draft or not store.draft.id then return nil, "请先载入已保存的路线。" end
    for i, route in ipairs(store.routes) do
        if route.id == store.draft.id then
            store.deleted = table.remove(store.routes, i)
            -- Keep the working copy: Save restores it with a new identity.
            store.draft.id = nil
            return true
        end
    end
    return nil, "路线不存在。"
end

-- A renderer must never draw a straight line between different local maps.
function Roads.VisibleSegments(route, mapID, artID)
    local points, segments = {}, {}
    local function Visible(p)
        return p and p.mapID == mapID and (not p.artID or not artID or p.artID == artID)
    end
    for index, point in ipairs(route.points) do
        if Visible(point) then
            points[#points + 1] = {index = index, point = point}
            if Visible(route.points[index - 1]) then
                segments[#segments + 1] = {route.points[index - 1], point}
            end
        end
    end
    return points, segments
end
