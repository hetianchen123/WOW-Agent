// WowAgent's C ABI around unmodified Recast/Detour 1.6.0. Zlib dependency
// notices are preserved in vendor/recastnavigation/License.txt.
#include "Recast.h"
#include "DetourAlloc.h"
#include "DetourCommon.h"
#include "DetourNavMesh.h"
#include "DetourNavMeshBuilder.h"
#include "DetourNavMeshQuery.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <utility>
#include <vector>

namespace {
void need(bool ok, const char* reason) { if (!ok) throw std::runtime_error(reason); }
bool complete(dtStatus s) {
    return dtStatusSucceed(s) && !(s & (DT_PARTIAL_RESULT | DT_BUFFER_TOO_SMALL | DT_OUT_OF_NODES));
}
void error(char* out, int cap, const char* text) { if (cap > 0) std::snprintf(out, cap, "%s", text); }
using Nav = std::unique_ptr<dtNavMesh, decltype(&dtFreeNavMesh)>;
using Query = std::unique_ptr<dtNavMeshQuery, decltype(&dtFreeNavMeshQuery)>;
using Tile = std::unique_ptr<unsigned char, decltype(&dtFree)>;
constexpr std::uint32_t tileSetMagic = 0x5741544d;
constexpr int maxTileCount = 1024;
struct TileSetHeader {
    std::uint32_t magic, version;
    float origin[3], tileWidth;
    int count, maxPolys;
};

dtMeshHeader inspectTile(const unsigned char* bytes, int size) {
    need(size >= static_cast<int>(sizeof(dtMeshHeader)), "invalid_navmesh_tile");
    dtMeshHeader mesh;
    std::memcpy(&mesh, bytes, sizeof(mesh));
    need(mesh.magic == DT_NAVMESH_MAGIC && mesh.version == DT_NAVMESH_VERSION &&
         mesh.polyCount > 0 && mesh.vertCount > 0 && mesh.maxLinkCount > 0 &&
         mesh.detailMeshCount == mesh.polyCount && mesh.detailVertCount >= 0 &&
         mesh.detailTriCount >= 0 && mesh.bvNodeCount >= 0 && mesh.offMeshConCount == 0,
         "invalid_navmesh_tile");
    auto align4 = [](std::size_t length) { return (length + 3) & ~std::size_t(3); };
    const std::size_t expected = align4(sizeof(mesh)) + align4(sizeof(float) * 3 * mesh.vertCount) +
        align4(sizeof(dtPoly) * mesh.polyCount) + align4(sizeof(dtLink) * mesh.maxLinkCount) +
        align4(sizeof(dtPolyDetail) * mesh.detailMeshCount) + align4(sizeof(float) * 3 * mesh.detailVertCount) +
        align4(4u * std::size_t(mesh.detailTriCount)) + align4(sizeof(dtBVNode) * mesh.bvNodeCount);
    need(expected == static_cast<std::size_t>(size), "invalid_navmesh_tile_layout");
    return mesh;
}

Tile copyTile(const unsigned char* bytes, int size) {
    Tile copy(static_cast<unsigned char*>(dtAlloc(size, DT_ALLOC_PERM)), dtFree);
    need(bool(copy), "out_of_memory");
    std::memcpy(copy.get(), bytes, size);
    return copy;
}

Nav load(const unsigned char* bytes, int size) {
    need(bytes && size >= static_cast<int>(sizeof(std::uint32_t)), "invalid_navmesh");
    Nav nav(dtAllocNavMesh(), dtFreeNavMesh);
    need(bool(nav), "out_of_memory");
    std::uint32_t magic;
    std::memcpy(&magic, bytes, sizeof(magic));
    if (magic != tileSetMagic) {
        inspectTile(bytes, size);
        auto copy = copyTile(bytes, size);
        need(complete(nav->init(copy.get(), size, DT_TILE_FREE_DATA)), "invalid_navmesh");
        copy.release();
        return nav;
    }
    need(size >= static_cast<int>(sizeof(TileSetHeader)), "invalid_tileset_header");
    TileSetHeader header;
    std::memcpy(&header, bytes, sizeof(header));
    need(header.version == 1 && header.count > 0 && header.count <= maxTileCount &&
         header.maxPolys > 0 && header.maxPolys < 65535 &&
         std::isfinite(header.tileWidth) && header.tileWidth > 0 &&
         std::isfinite(header.origin[0]) && std::isfinite(header.origin[1]) && std::isfinite(header.origin[2]),
         "invalid_tileset_header");
    dtNavMeshParams params{};
    dtVcopy(params.orig, header.origin);
    params.tileWidth = params.tileHeight = header.tileWidth;
    params.maxTiles = header.count;
    params.maxPolys = header.maxPolys;
    need(complete(nav->init(&params)), "tileset_init_failed");
    int cursor = sizeof(header);
    for (int i = 0; i < header.count; ++i) {
        need(size - cursor >= static_cast<int>(sizeof(int)), "truncated_tileset");
        int length;
        std::memcpy(&length, bytes + cursor, sizeof(length));
        cursor += sizeof(length);
        need(length >= static_cast<int>(sizeof(dtMeshHeader)) && length <= size - cursor, "invalid_tileset_tile_size");
        const auto mesh = inspectTile(bytes + cursor, length);
        need(mesh.polyCount <= header.maxPolys, "invalid_tileset_tile_polygons");
        auto copy = copyTile(bytes + cursor, length);
        need(complete(nav->addTile(copy.get(), length, DT_TILE_FREE_DATA, 0, nullptr)), "tileset_tile_link_failed");
        copy.release();
        cursor += length;
    }
    need(cursor == size, "trailing_tileset_data");
    return nav;
}

Query makeQuery(const dtNavMesh* nav) {
    Query query(dtAllocNavMeshQuery(), dtFreeNavMeshQuery);
    need(bool(query) && complete(query->init(nav, 65535)), "query_init_failed");
    return query;
}

struct PreparedMesh {
    explicit PreparedMesh(Nav loaded) : nav(std::move(loaded)), query(makeQuery(nav.get())) {}
    Nav nav;
    Query query;
    std::mutex lock;
};

// cfg: cell size, cell height, body height, radius, climb, slope degrees.
rcConfig configuration(const float* vertices, int nv, int nt, const float* cfg) {
    need(nv >= 3 && nt > 0 && nv <= 2000000 && nt <= 2000000, "geometry_size_limit");
    rcConfig c{};
    c.cs = cfg[0]; c.ch = cfg[1]; c.walkableSlopeAngle = cfg[5];
    c.walkableHeight = static_cast<int>(std::ceil(cfg[2] / c.ch));
    c.walkableRadius = static_cast<int>(std::ceil(cfg[3] / c.cs));
    c.walkableClimb = static_cast<int>(std::floor(cfg[4] / c.ch));
    c.maxEdgeLen = static_cast<int>(12 / c.cs); c.maxSimplificationError = 1.0f;
    c.minRegionArea = 0; c.mergeRegionArea = 16; c.maxVertsPerPoly = 6;
    c.detailSampleDist = c.cs * 6; c.detailSampleMaxError = c.ch;
    rcCalcBounds(vertices, nv, c.bmin, c.bmax);
    c.bmin[1] -= c.ch * 2; c.bmax[1] += cfg[2] + c.ch * 2;
    need((c.bmax[0]-c.bmin[0])/c.cs < 30000 &&
         (c.bmax[2]-c.bmin[2])/c.cs < 30000 &&
         (c.bmax[1]-c.bmin[1])/c.ch < RC_SPAN_MAX_HEIGHT, "region_extent_limit");
    return c;
}

int buildRegion(const float* vertices, int nv, const int* triangles, int nt, const float* cfg,
                const rcConfig& c, int tx, int tz, unsigned char** output, int* size) {
    rcContext ctx(false);
    std::unique_ptr<rcHeightfield, decltype(&rcFreeHeightField)> solid(rcAllocHeightfield(), rcFreeHeightField);
    need(bool(solid) && rcCreateHeightfield(&ctx, *solid, c.width, c.height, c.bmin, c.bmax, c.cs, c.ch), "heightfield_failed");
    std::vector<unsigned char> areas(nt, RC_NULL_AREA);
    rcMarkWalkableTriangles(&ctx, c.walkableSlopeAngle, vertices, nv, triangles, nt, areas.data());
    need(rcRasterizeTriangles(&ctx, vertices, nv, triangles, areas.data(), nt, *solid, c.walkableClimb), "rasterize_failed");
    rcFilterLowHangingWalkableObstacles(&ctx, c.walkableClimb, *solid);
    rcFilterLedgeSpans(&ctx, c.walkableHeight, c.walkableClimb, *solid);
    rcFilterWalkableLowHeightSpans(&ctx, c.walkableHeight, *solid);
    std::unique_ptr<rcCompactHeightfield, decltype(&rcFreeCompactHeightfield)> compact(rcAllocCompactHeightfield(), rcFreeCompactHeightfield);
    need(bool(compact) && rcBuildCompactHeightfield(&ctx, c.walkableHeight, c.walkableClimb, *solid, *compact), "compact_failed");
    solid.reset();
    need(rcErodeWalkableArea(&ctx, c.walkableRadius, *compact), "erosion_failed");
    // Monotone partitioning avoids overlapping regions in stacked interiors.
    need(rcBuildRegionsMonotone(&ctx, *compact, c.borderSize, c.minRegionArea, c.mergeRegionArea), "regions_failed");
    std::unique_ptr<rcContourSet, decltype(&rcFreeContourSet)> contours(rcAllocContourSet(), rcFreeContourSet);
    need(bool(contours) && rcBuildContours(&ctx, *compact, c.maxSimplificationError, c.maxEdgeLen, *contours), "contours_failed");
    if (!contours->nconts && c.borderSize) return 0;
    std::unique_ptr<rcPolyMesh, decltype(&rcFreePolyMesh)> mesh(rcAllocPolyMesh(), rcFreePolyMesh);
    need(bool(mesh) && rcBuildPolyMesh(&ctx, *contours, c.maxVertsPerPoly, *mesh), "polymesh_failed");
    if (!mesh->npolys && c.borderSize) return 0;
    need(mesh->npolys > 0 && mesh->nverts < 65535, "no_walkable_surface_or_region_too_large");
    std::unique_ptr<rcPolyMeshDetail, decltype(&rcFreePolyMeshDetail)> detail(rcAllocPolyMeshDetail(), rcFreePolyMeshDetail);
    need(bool(detail) && rcBuildPolyMeshDetail(&ctx, *mesh, *compact, c.detailSampleDist, c.detailSampleMaxError, *detail), "detail_failed");
    for (int i=0; i<mesh->npolys; ++i) { mesh->flags[i]=1; mesh->areas[i]=0; }
    dtNavMeshCreateParams p{};
    p.verts=mesh->verts; p.vertCount=mesh->nverts;
    p.polys=mesh->polys; p.polyAreas=mesh->areas; p.polyFlags=mesh->flags;
    p.polyCount=mesh->npolys; p.nvp=mesh->nvp;
    p.detailMeshes=detail->meshes; p.detailVerts=detail->verts;
    p.detailVertsCount=detail->nverts; p.detailTris=detail->tris; p.detailTriCount=detail->ntris;
    p.walkableHeight=cfg[2]; p.walkableRadius=cfg[3]; p.walkableClimb=cfg[4];
    p.tileX=tx; p.tileY=tz;
    rcVcopy(p.bmin, mesh->bmin); rcVcopy(p.bmax, mesh->bmax);
    p.cs=c.cs; p.ch=c.ch; p.buildBvTree=true;
    need(dtCreateNavMeshData(&p, output, size), "detour_build_failed");
    return mesh->npolys;
}
}

extern "C" {
void wa_free(void* p) { dtFree(p); }

int wa_build(const float* vertices, int nv, const int* triangles, int nt,
             const float* cfg, unsigned char** output, int* size, char* err, int cap) {
    *output = nullptr; *size = 0;
    try {
        auto c = configuration(vertices, nv, nt, cfg);
        rcCalcGridSize(c.bmin, c.bmax, c.cs, &c.width, &c.height);
        need(c.width > 0 && c.height > 0 &&
             static_cast<long long>(c.width)*c.height <= 4000000, "region_cell_limit");
        return buildRegion(vertices, nv, triangles, nt, cfg, c, 0, 0, output, size);
    } catch (const std::exception& e) { error(err, cap, e.what()); return -1; }
}

int wa_build_tiles(const float* vertices, int nv, const int* triangles, int nt,
                   const float* cfg, int cells, unsigned char** output, int* size, char* err, int cap) {
    *output = nullptr; *size = 0;
    try {
        need(cells >= 32 && cells <= 512, "tile_cell_limit");
        auto c = configuration(vertices, nv, nt, cfg);
        c.tileSize = cells;
        c.borderSize = c.walkableRadius + 3;
        c.width = c.height = cells + 2 * c.borderSize;
        const float width = cells * c.cs;
        const float padding = c.borderSize * c.cs;
        TileSetHeader header{tileSetMagic, 1, {}, width, 0, 0};
        dtVcopy(header.origin, c.bmin);
        // A common grid origin and voxel height make adjacent polygons connect.
        const int nx = std::max(1, static_cast<int>(std::ceil((c.bmax[0] - c.bmin[0]) / width)));
        const int nz = std::max(1, static_cast<int>(std::ceil((c.bmax[2] - c.bmin[2]) / width)));
        need(nx * nz <= maxTileCount, "region_tile_limit");
        std::vector<std::vector<int>> buckets(nx * nz);
        auto bin = [width](float value, float origin, int count) {
            return std::clamp(static_cast<int>(std::floor((value - origin) / width)), 0, count - 1);
        };
        std::size_t references = 0;
        for (int i = 0; i < nt; ++i) {
            const int* triangle = triangles + 3 * i;
            const float* a = vertices + 3 * triangle[0];
            const float* b = vertices + 3 * triangle[1];
            const float* d = vertices + 3 * triangle[2];
            const int minX = bin(std::min({a[0], b[0], d[0]}) - padding, header.origin[0], nx);
            const int maxX = bin(std::max({a[0], b[0], d[0]}) + padding, header.origin[0], nx);
            const int minZ = bin(std::min({a[2], b[2], d[2]}) - padding, header.origin[2], nz);
            const int maxZ = bin(std::max({a[2], b[2], d[2]}) + padding, header.origin[2], nz);
            references += (maxX - minX + 1) * (maxZ - minZ + 1);
            need(references <= 16000000, "tile_triangle_reference_limit");
            for (int z = minZ; z <= maxZ; ++z)
                for (int x = minX; x <= maxX; ++x) {
                    auto& bucket = buckets[z * nx + x];
                    bucket.insert(bucket.end(), triangle, triangle + 3);
                }
        }
        std::vector<unsigned char> archive(sizeof(header));
        int totalPolys = 0;
        for (int z = 0; z < nz; ++z) {
            for (int x = 0; x < nx; ++x) {
                const auto& bucket = buckets[z * nx + x];
                if (bucket.empty()) continue;
                c.bmin[0] = header.origin[0] + x * width - padding;
                c.bmin[2] = header.origin[2] + z * width - padding;
                c.bmax[0] = header.origin[0] + (x + 1) * width + padding;
                c.bmax[2] = header.origin[2] + (z + 1) * width + padding;
                unsigned char* bytes = nullptr;
                int length = 0;
                const int count = buildRegion(vertices, nv, bucket.data(), static_cast<int>(bucket.size() / 3),
                                              cfg, c, x, z, &bytes, &length);
                Tile tile(bytes, dtFree);
                if (!count) continue;
                need(archive.size() + sizeof(length) + length < 128 * 1024 * 1024, "tileset_size_limit");
                const auto* lengthBytes = reinterpret_cast<const unsigned char*>(&length);
                archive.insert(archive.end(), lengthBytes, lengthBytes + sizeof(length));
                archive.insert(archive.end(), bytes, bytes + length);
                ++header.count;
                header.maxPolys = std::max(header.maxPolys, count);
                totalPolys += count;
            }
        }
        need(totalPolys > 0, "no_walkable_surface_or_region_too_large");
        std::memcpy(archive.data(), &header, sizeof(header));
        load(archive.data(), static_cast<int>(archive.size()));
        auto* result = static_cast<unsigned char*>(dtAlloc(archive.size(), DT_ALLOC_PERM));
        need(result != nullptr, "out_of_memory");
        std::memcpy(result, archive.data(), archive.size());
        *output = result;
        *size = static_cast<int>(archive.size());
        return totalPolys;
    } catch (const std::exception& e) { error(err, cap, e.what()); return -1; }
}

static dtQueryFilter groundFilter() {
    dtQueryFilter filter;
    filter.setIncludeFlags(1);
    filter.setExcludeFlags(2);
    return filter;
}

constexpr int pathLimit = 8192;

static int writePath(dtNavMeshQuery* q, const float* a, const float* b,
                     const dtPolyRef* corridor, int corridorSize, float spacing,
                     float* points, int maxPoints) {
    std::vector<dtPolyRef> refs(pathLimit);
    std::vector<float> straight(pathLimit * 3);
    std::vector<unsigned char> flags(pathLimit);
    int straightN = 0;
    need(complete(q->findStraightPath(a, b, corridor, corridorSize, straight.data(), flags.data(),
         refs.data(), &straightN, pathLimit, DT_STRAIGHTPATH_ALL_CROSSINGS)), "path_buffer_exhausted");
    need(straightN > 0 && (flags[straightN - 1] & DT_STRAIGHTPATH_END), "no_complete_path");
    int count=0;
    auto emit=[&](const float* p) {
        need(count<maxPoints, "path_buffer_exhausted");
        std::memcpy(points+count++*3,p,3*sizeof(float));
    };
    emit(a);
    for (int i=0;i<straightN-1;++i) {
        const float* from=&straight[i*3]; const float* to=&straight[(i+1)*3];
        int steps=std::max(1,static_cast<int>(std::ceil(dtVdist(from,to)/spacing)));
        need(steps<=maxPoints-count, "path_buffer_exhausted");
        for (int j=1;j<=steps;++j) {
            float p[3]; dtVlerp(p,from,to,static_cast<float>(j)/steps);
            // ALL_CROSSINGS retains every polygon transition; sample the
            // detail surface of that segment, never a nearby stacked floor.
            float h=0;
            if (!complete(q->getPolyHeight(refs[i],p,&h))) {
                // Portal-edge rounding may put a point a few ulps outside
                // its polygon. Only clamp within 1 mm on that SAME polygon.
                float nearest[3];
                need(complete(q->closestPointOnPoly(refs[i],p,nearest,nullptr)) &&
                     std::hypot(p[0]-nearest[0],p[2]-nearest[2])<=.001f,
                     "surface_height_unavailable");
                h=nearest[1];
            }
            p[1]=h; emit(p);
        }
    }
    return count;
}

static int queryPath(dtNavMeshQuery* q, const float* start, const float* end,
                     float horizontalSnap, float verticalSnap, float spacing,
                     float* points, int maxPoints, std::uint64_t* refs = nullptr,
                     int* refCount = nullptr) {
    const auto filter = groundFilter();
    float ext[3]={horizontalSnap,verticalSnap,horizontalSnap}, a[3], b[3];
    dtPolyRef ar=0,br=0;
    need(complete(q->findNearestPoly(start,ext,&filter,&ar,a)) && ar, "start_off_mesh");
    need(complete(q->findNearestPoly(end,ext,&filter,&br,b)) && br, "goal_off_mesh");
    auto near=[&](const float* x,const float* y) {
        return std::hypot(x[0]-y[0],x[2]-y[2])<=horizontalSnap+1e-5f && std::abs(x[1]-y[1])<=verticalSnap+1e-5f;
    };
    need(near(start,a), "start_snap_exceeded"); need(near(end,b), "goal_snap_exceeded");
    std::vector<dtPolyRef> corridor(pathLimit);
    int n=0;
    auto status=q->findPath(ar,br,a,b,&filter,corridor.data(),&n,pathLimit);
    need(complete(status) && n>0 && corridor[n-1]==br, "no_complete_path");
    if (refs) {
        std::copy_n(corridor.begin(), n, refs);
        *refCount = n;
    }
    return writePath(q, a, b, corridor.data(), n, spacing, points, maxPoints);
}

void* wa_open_mesh(const unsigned char* bytes, int size, char* err, int cap) {
    try {
        return new PreparedMesh(load(bytes, size));
    } catch (const std::exception& e) { error(err, cap, e.what()); return nullptr; }
}

void wa_close_mesh(void* handle) { delete static_cast<PreparedMesh*>(handle); }

int wa_query_mesh(void* handle, const float* start, const float* end,
                  float horizontalSnap, float verticalSnap, float spacing,
                  float* points, int maxPoints, std::uint64_t* refs, int* refCount, char* err, int cap) {
    try {
        need(handle, "invalid_prepared_mesh");
        auto* mesh = static_cast<PreparedMesh*>(handle);
        std::lock_guard<std::mutex> guard(mesh->lock);
        return queryPath(mesh->query.get(), start, end, horizontalSnap, verticalSnap, spacing,
                         points, maxPoints, refs, refCount);
    } catch (const std::exception& e) { error(err, cap, e.what()); return -1; }
}

// Move the live end of a Detour corridor over a small, observed surface step.
// Repair only the prefix where the observed polygons meet the existing path.
int wa_advance_corridor(void* handle, const float* start, const float* position,
                        float tolerance, float* snapped, std::uint64_t* refs, int size,
                        char* err, int cap) {
    try {
        need(handle && size > 0 && size <= pathLimit, "invalid_live_corridor");
        auto* mesh = static_cast<PreparedMesh*>(handle);
        std::lock_guard<std::mutex> guard(mesh->lock);
        auto* q = mesh->query.get();
        const auto filter = groundFilter();
        need(dtVdist(start, position) <= 2.2f, "corridor_step_too_long");
        constexpr int maxVisited = 32;
        dtPolyRef visited[maxVisited];
        int count = 0;
        float reached[3];
        need(complete(q->moveAlongSurface(static_cast<dtPolyRef>(refs[0]), start, position,
             &filter, reached, visited, &count, maxVisited)) && count > 0, "corridor_surface_move_failed");
        need(complete(q->closestPointOnPoly(visited[count - 1], reached, snapped, nullptr)) &&
             std::hypot(snapped[0] - position[0], snapped[2] - position[2]) <= tolerance &&
             std::abs(snapped[1] - position[1]) <= .15f, "corridor_support_mismatch");
        const int localPrefix = std::min(size, maxVisited);
        for (int observed = count - 1; observed >= 0; --observed) {
            const auto* match = std::find(refs, refs + localPrefix, visited[observed]);
            if (match == refs + localPrefix) continue;
            const int remaining = size - static_cast<int>(match - refs);
            const int prefix = count - 1 - observed;
            need(prefix + remaining <= pathLimit, "corridor_buffer_exhausted");
            std::memmove(refs + prefix, match, remaining * sizeof(*refs));
            std::reverse_copy(visited + observed + 1, visited + count, refs);
            return prefix + remaining;
        }
        throw std::runtime_error("corridor_path_disconnected");
    } catch (const std::exception& e) { error(err, cap, e.what()); return -1; }
}

int wa_render_corridor(void* handle, const float* start, const float* goal, float spacing,
                       const std::uint64_t* refs, int size, float* points, int maxPoints,
                       char* err, int cap) {
    try {
        need(handle && size > 0 && size <= pathLimit, "invalid_live_corridor");
        auto* mesh = static_cast<PreparedMesh*>(handle);
        std::lock_guard<std::mutex> guard(mesh->lock);
        std::vector<dtPolyRef> path(refs, refs + size);
        return writePath(mesh->query.get(), start, goal, path.data(), size, spacing, points, maxPoints);
    } catch (const std::exception& e) { error(err, cap, e.what()); return -1; }
}

int wa_corridor_raycast(void* handle, std::uint64_t startRef, const float* start,
                        const float* end, float* fraction, char* err, int cap) {
    try {
        need(handle && startRef, "invalid_live_corridor");
        auto* mesh = static_cast<PreparedMesh*>(handle);
        std::lock_guard<std::mutex> guard(mesh->lock);
        const auto filter = groundFilter();
        dtPolyRef visited[32];
        dtRaycastHit hit{};
        hit.path = visited;
        hit.maxPath = 32;
        need(complete(mesh->query->raycast(static_cast<dtPolyRef>(startRef), start, end, &filter, 0, &hit))
             && std::isfinite(hit.t), "corridor_raycast_failed");
        *fraction = std::clamp(hit.t, 0.0f, 1.0f);
        return 0;
    } catch (const std::exception& e) { error(err, cap, e.what()); return -1; }
}

// Request-local boxes include the agent's horizontal radius. Use a disposable
// mesh because exclusion mutates polygon flags throughout the request.
int wa_query(const unsigned char* bytes, int size, const float* start, const float* end,
             float horizontalSnap, float verticalSnap, float spacing,
             const float* boxes, int nboxes, float* points, int maxPoints,
             char* err, int cap) {
    try {
        auto nav = load(bytes, size);
        const dtNavMesh* read = nav.get();
        for (int t = 0; nboxes > 0 && t < read->getMaxTiles(); ++t) {
            const auto* tile = read->getTile(t);
            if (!tile || !tile->header) continue;
            const auto base = nav->getPolyRefBase(tile);
            for (int i = 0; i < tile->header->polyCount; ++i) {
                const auto& poly = tile->polys[i];
                float lo[3] = {INFINITY, INFINITY, INFINITY}, hi[3] = {-INFINITY, -INFINITY, -INFINITY};
                for (int j = 0; j < poly.vertCount; ++j) {
                    const float* v = &tile->verts[poly.verts[j] * 3];
                    dtVmin(lo, v); dtVmax(hi, v);
                }
                hi[1] += tile->header->walkableHeight;
                for (int b = 0; b < nboxes; ++b) {
                    if (dtOverlapBounds(lo, hi, boxes + b * 6, boxes + b * 6 + 3)) {
                        nav->setPolyFlags(base | i, 2);
                        break;
                    }
                }
            }
        }
        auto query = makeQuery(nav.get());
        return queryPath(query.get(), start, end, horizontalSnap, verticalSnap, spacing, points, maxPoints);
    } catch (const std::exception& e) { error(err,cap,e.what()); return -1; }
}

// Triangle soup of the detail mesh for offline visualization (two-pass API).
int wa_debug(const unsigned char* bytes, int size, float* out, int maxTriangles,
             char* err, int cap) {
    try {
        auto nav=load(bytes,size); const dtNavMesh* read=nav.get(); int count=0;
        for (int t=0;t<read->getMaxTiles();++t) {
            const auto* tile=read->getTile(t); if (!tile || !tile->header) continue;
            for (int i=0;i<tile->header->detailMeshCount;++i) {
                const auto& poly=tile->polys[i]; const auto& detail=tile->detailMeshes[i];
                for (int j=0;j<detail.triCount;++j) {
                    if (out) {
                        need(count<maxTriangles,"debug_buffer_exhausted");
                        const auto* tri=&tile->detailTris[(detail.triBase+j)*4];
                        for (int k=0;k<3;++k) {
                            const float* v=tri[k]<poly.vertCount ? &tile->verts[poly.verts[tri[k]]*3]
                                : &tile->detailVerts[(detail.vertBase+tri[k]-poly.vertCount)*3];
                            std::memcpy(out+count*9+k*3,v,3*sizeof(float));
                        }
                    }
                    ++count;
                }
            }
        }
        return count;
    } catch (const std::exception& e) { error(err,cap,e.what()); return -1; }
}
}
