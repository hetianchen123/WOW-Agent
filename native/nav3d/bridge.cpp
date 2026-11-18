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
#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <vector>

namespace {
void need(bool ok, const char* reason) { if (!ok) throw std::runtime_error(reason); }
bool complete(dtStatus s) {
    return dtStatusSucceed(s) && !(s & (DT_PARTIAL_RESULT | DT_BUFFER_TOO_SMALL | DT_OUT_OF_NODES));
}
void error(char* out, int cap, const char* text) { if (cap > 0) std::snprintf(out, cap, "%s", text); }
using Nav = std::unique_ptr<dtNavMesh, decltype(&dtFreeNavMesh)>;
Nav load(const unsigned char* bytes, int size) {
    need(size >= static_cast<int>(sizeof(dtMeshHeader)), "invalid_navmesh");
    Nav nav(dtAllocNavMesh(), dtFreeNavMesh);
    need(bool(nav), "out_of_memory");
    auto* copy = static_cast<unsigned char*>(dtAlloc(size, DT_ALLOC_PERM));
    need(copy != nullptr, "out_of_memory");
    std::memcpy(copy, bytes, size);
    dtStatus status = nav->init(copy, size, DT_TILE_FREE_DATA);
    if (dtStatusFailed(status)) { dtFree(copy); throw std::runtime_error("invalid_navmesh"); }
    return nav;
}
}

extern "C" {
void wa_free(void* p) { dtFree(p); }

// cfg: cell size, cell height, body height, radius, climb, slope degrees.
int wa_build(const float* vertices, int nv, const int* triangles, int nt,
             const float* cfg, unsigned char** output, int* size, char* err, int cap) {
    *output = nullptr; *size = 0;
    try {
        need(nv >= 3 && nt > 0 && nv <= 2000000 && nt <= 2000000, "geometry_size_limit");
        rcContext ctx(false);
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
        rcCalcGridSize(c.bmin, c.bmax, c.cs, &c.width, &c.height);
        need(c.width > 0 && c.height > 0 &&
             static_cast<long long>(c.width)*c.height <= 4000000, "region_cell_limit");
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
        need(rcBuildRegionsMonotone(&ctx, *compact, 0, c.minRegionArea, c.mergeRegionArea), "regions_failed");
        std::unique_ptr<rcContourSet, decltype(&rcFreeContourSet)> contours(rcAllocContourSet(), rcFreeContourSet);
        need(bool(contours) && rcBuildContours(&ctx, *compact, c.maxSimplificationError, c.maxEdgeLen, *contours), "contours_failed");
        std::unique_ptr<rcPolyMesh, decltype(&rcFreePolyMesh)> mesh(rcAllocPolyMesh(), rcFreePolyMesh);
        need(bool(mesh) && rcBuildPolyMesh(&ctx, *contours, c.maxVertsPerPoly, *mesh), "polymesh_failed");
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
        rcVcopy(p.bmin, mesh->bmin); rcVcopy(p.bmax, mesh->bmax);
        p.cs=c.cs; p.ch=c.ch; p.buildBvTree=true;
        need(dtCreateNavMeshData(&p, output, size), "detour_build_failed");
        return mesh->npolys;
    } catch (const std::exception& e) { error(err, cap, e.what()); return -1; }
}

// Request-local blocked boxes are conservative polygon exclusions. They never
// mutate the cached mesh. Boxes already include the agent's horizontal radius.
int wa_query(const unsigned char* bytes, int size, const float* start, const float* end,
             float horizontalSnap, float verticalSnap, float spacing,
             const float* boxes, int nboxes, float* points, int maxPoints,
             char* err, int cap) {
    try {
        auto nav=load(bytes, size);
        const dtNavMesh* read=nav.get();
        for (int t=0; t<read->getMaxTiles(); ++t) {
            const auto* tile=read->getTile(t);
            if (!tile || !tile->header) continue;
            const auto base=nav->getPolyRefBase(tile);
            for (int i=0; i<tile->header->polyCount; ++i) {
                const auto& poly=tile->polys[i];
                float lo[3]={INFINITY,INFINITY,INFINITY}, hi[3]={-INFINITY,-INFINITY,-INFINITY};
                for (int j=0;j<poly.vertCount;++j) {
                    const float* v=&tile->verts[poly.verts[j]*3];
                    dtVmin(lo,v); dtVmax(hi,v);
                }
                // Include the full standing body above a support surface.
                hi[1] += tile->header->walkableHeight;
                for (int b=0;b<nboxes;++b) if (dtOverlapBounds(lo,hi,boxes+b*6,boxes+b*6+3)) {
                    nav->setPolyFlags(base | i, 2); break;
                }
            }
        }
        std::unique_ptr<dtNavMeshQuery, decltype(&dtFreeNavMeshQuery)> q(dtAllocNavMeshQuery(), dtFreeNavMeshQuery);
        need(bool(q) && complete(q->init(nav.get(), 65535)), "query_init_failed");
        dtQueryFilter filter; filter.setIncludeFlags(1); filter.setExcludeFlags(2);
        float ext[3]={horizontalSnap,verticalSnap,horizontalSnap}, a[3], b[3];
        dtPolyRef ar=0,br=0;
        need(complete(q->findNearestPoly(start,ext,&filter,&ar,a)) && ar, "start_off_mesh");
        need(complete(q->findNearestPoly(end,ext,&filter,&br,b)) && br, "goal_off_mesh");
        auto near=[&](const float* x,const float* y) {
            return std::hypot(x[0]-y[0],x[2]-y[2])<=horizontalSnap+1e-5f && std::abs(x[1]-y[1])<=verticalSnap+1e-5f;
        };
        need(near(start,a), "start_snap_exceeded"); need(near(end,b), "goal_snap_exceeded");
        const int limit=8192;
        std::vector<dtPolyRef> corridor(limit), refs(limit);
        int n=0, straightN=0;
        auto status=q->findPath(ar,br,a,b,&filter,corridor.data(),&n,limit);
        need(complete(status) && n>0 && corridor[n-1]==br, "no_complete_path");
        std::vector<float> straight(limit*3);
        std::vector<unsigned char> flags(limit);
        need(complete(q->findStraightPath(a,b,corridor.data(),n,straight.data(),flags.data(),refs.data(),&straightN,limit,DT_STRAIGHTPATH_ALL_CROSSINGS)), "path_buffer_exhausted");
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
                    // its polygon. Only clamp within 1 mm on that SAME polygon;
                    // never search another floor to repair a missing height.
                    float nearest[3]; bool over=false;
                    need(complete(q->closestPointOnPoly(refs[i],p,nearest,&over)) &&
                         std::hypot(p[0]-nearest[0],p[2]-nearest[2])<=.001f,
                         "surface_height_unavailable");
                    h=nearest[1];
                }
                p[1]=h; emit(p);
            }
        }
        return count;
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
