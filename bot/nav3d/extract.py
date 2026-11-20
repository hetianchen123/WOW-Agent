"""Bounded client collision extraction using pinned wow.export loaders.

Output is static geometry, not proof of live phasing or dynamic-object state.
World coordinates are east/north/up: (-UnitPosition.y, UnitPosition.x, z).
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import subprocess

import numpy as np

from .casc import LocalCASC, CASC_COMMIT, EXPORT_COMMIT
from .planner import Frame, NavigationError

ROOT = Path(__file__).resolve().parents[2]
TILE = 1600/3
CENTER = TILE*32
ALL_TERRAIN_CELLS = (1 << 64) - 1


class ClientDecoder:
    def __init__(self, source, work):
        source = Path(source).resolve()
        revision = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
        if revision != EXPORT_COMMIT or subprocess.check_output(['git','-C',str(source),'diff','HEAD','--name-only']):
            raise NavigationError('wow_export_revision_mismatch')
        self.work = Path(work); self.work.mkdir(parents=True,exist_ok=True)
        self.process = subprocess.Popen(['node',str(ROOT/'native/nav3d/casc/decode.cjs'),str(source)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        self.memo = {}

    def decode(self, kind, data, **extra):
        digest = hashlib.sha256(data).hexdigest(); key=(kind,digest,json.dumps(extra,sort_keys=True))
        if key not in self.memo:
            path=self.work/digest
            if not path.exists(): path.write_bytes(data)
            req=dict(kind=kind,path=str(path.resolve()),**extra)
            self.process.stdin.write(json.dumps(req)+'\n');self.process.stdin.flush()
            line=self.process.stdout.readline()
            if not line: raise NavigationError('client_decoder_exited')
            result=json.loads(line)
            if not result['ok']: raise NavigationError('client_decode_failed:'+result['error'])
            self.memo[key]=result['data']
        return self.memo[key]

    def close(self):
        self.process.stdin.close()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()


def placement_matrix(entry, *, wmo=False):
    """ADT Euler XYZ, reproduced from wow.export's Blender placement convention."""
    px,py,pz=entry['position'];rx,ry,rz=np.radians(entry['rotation'])
    def rot(axis,a):
        c,s=math.cos(a),math.sin(a)
        if axis==0:return np.array([[1,0,0],[0,c,-s],[0,s,c]])
        if axis==1:return np.array([[c,0,s],[0,1,0],[-s,0,c]])
        return np.array([[c,-s,0],[s,c,0],[0,0,1]])
    scale=entry['scale']/1024
    if wmo and not entry['flags']&4:scale=1
    if not 0<scale<=100:raise NavigationError('invalid_placement_scale')
    m=np.eye(4);m[:3,:3]=rot(2,ry+1.5*math.pi)@rot(1,rx)@rot(0,rz)*scale
    m[:3,3]=[px-CENTER,CENTER-pz,py]
    return m


def quaternion_matrix(q, position, scale):
    x,y,z,w=map(float,q);norm=sum(v*v for v in q)
    if abs(norm-1)>.01 or not 0<scale<=100:raise NavigationError('invalid_doodad_transform')
    m=np.eye(4);m[:3,:3]=np.array([
        [1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
        [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
        [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])*scale
    m[:3,3]=position;return m


def raw_vertices(values):
    # wow.export's loader maps source XYZ to (X,Z,-Y) for rendering; undo it.
    v=np.asarray(values,float).reshape(-1,3)
    return v[:,[0,2,1]]*np.array([1,-1,1])


def liquid_cells(instances):
    """Return the union of wet MH2O cells. Incomplete records block the chunk."""
    wet = 0
    for layer in instances:
        if not isinstance(layer, dict):
            return ALL_TERRAIN_CELLS
        x0, y0, width, height, offset = (layer.get(key) for key in
            ("xOffset", "yOffset", "width", "height", "offsetExistsBitmap"))
        if (any(type(v) is not int for v in (x0, y0, width, height, offset)) or offset < 0
                or not (0 <= x0 < x0 + width <= 8 and 0 <= y0 < y0 + height <= 8)):
            return ALL_TERRAIN_CELLS
        coverage = (1 << (width * height)) - 1
        if offset:
            bitmap = layer.get("bitmap")
            nbytes = (width * height + 7) // 8
            if (not isinstance(bitmap, list) or len(bitmap) < nbytes
                    or any(type(v) is not int or not 0 <= v <= 255 for v in bitmap[:nbytes])):
                return ALL_TERRAIN_CELLS
            coverage &= int.from_bytes(bytes(bitmap[:nbytes]), "little")
        row_mask = (1 << width) - 1
        for y in range(height):
            row = (coverage >> (y * width)) & row_mask
            wet |= row << ((y + y0) * 8 + x0)
    return wet


def terrain(chunk, wet=0):
    unit=TILE/128;v=[];t=[];cx,cy,cz=chunk['position']
    for row in range(17):
        for col in range(8 if row%2 else 9):
            v.append([-cy+col*unit+(unit/2 if row%2 else 0),cx-row*unit/2,cz+chunk['vertices'][len(v)]])
    for y in range(8):
        for x in range(8):
            hole=((chunk['holesHighRes'][y]>>x)&1 if chunk['flags']&0x10000
                  else (chunk['holesLowRes']>>(x//2+4*(y//2)))&1)
            if hole or wet & (1 << (y * 8 + x)):continue
            i=y*17+9+x
            # ENU up-facing winding.
            t.extend([[i,i-9,i+8],[i,i-8,i-9],[i,i+9,i-8],[i,i+8,i+9]])
    return np.array(v),np.array(t,dtype=int).reshape(-1,3)


def wmo_liquid_cells(liquid, placement):
    """Yield the world-space surface of each active MLIQ cell."""
    if not isinstance(liquid, dict):
        raise NavigationError('wmo_liquid_shape_invalid')
    width, height = (liquid.get(key) for key in ('tileX', 'tileY'))
    flags, vertices = liquid.get('tiles'), liquid.get('vertices')
    if (type(width) is not int or type(height) is not int or not 1 <= width <= 256 or not 1 <= height <= 256
            or liquid.get('vertX') != width + 1 or liquid.get('vertY') != height + 1
            or not isinstance(flags, list) or len(flags) != width * height
            or any(type(flag) is not int or not 0 <= flag <= 255 for flag in flags)
            or not isinstance(vertices, list) or len(vertices) != (width + 1) * (height + 1)):
        raise NavigationError('wmo_liquid_shape_invalid')
    corner = np.asarray(liquid.get('corner'), dtype=float)
    if corner.shape != (3,) or not np.isfinite(corner).all():
        raise NavigationError('wmo_liquid_corner_invalid')
    offsets = np.array(((0, 0), (1, 0), (1, 1), (0, 1)))
    for index, flag in enumerate(flags):
        if flag & 0x0F == 0x0F:
            continue
        y, x = divmod(index, width)
        grid = offsets + (x, y)
        corners = [vertices[col + row * (width + 1)] for col, row in grid]
        heights = [vertex.get('height') if isinstance(vertex, dict) else None for vertex in corners]
        if not all(type(z) in (float, int) and math.isfinite(z) for z in heights):
            raise NavigationError('wmo_liquid_height_invalid')
        points = np.c_[corner[:2] + grid * (TILE / 128), heights, np.ones(4)]
        yield (points @ placement.T)[:, :3]


def split_polygon(polygon, signed):
    """Return polygon pieces on the positive and negative sides of a plane."""
    inside, outside = [], []
    for index, vertex in enumerate(polygon):
        previous, current = signed[index - 1], signed[index]
        if previous * current < 0:
            start = polygon[index - 1]
            crossing = start + (vertex - start) * (previous / (previous - current))
            inside.append(crossing)
            outside.append(crossing)
        if current >= 0:
            inside.append(vertex)
        if current <= 0:
            outside.append(vertex)
    return np.asarray(inside), np.asarray(outside)


def subtract_submerged(polygon, planes):
    """Keep convex fragments outside the vertical volume under one liquid face."""
    dry = []
    for normal, anchor in planes:
        signed = (polygon - anchor) @ normal
        lower = signed.min()
        if lower >= 0:
            continue
        if signed.max() <= 0:
            return [*dry, polygon]
        polygon, outside = split_polygon(polygon, signed)
        if len(outside) >= 3:
            dry.append(outside)
    return dry


class SceneBuilder:
    def __init__(self, casc, decoder, roi, names=None, *, tiled=False):
        self.casc,self.decoder,self.roi=casc,decoder,np.asarray(roi,float)
        max_span = 800 if tiled else 320
        if self.roi.shape!=(2,2) or not np.isfinite(self.roi).all() or (self.roi[1]<=self.roi[0]).any() or (self.roi[1]-self.roi[0]>max_span).any():
            raise NavigationError('invalid_local_roi')
        self.names=names or {};self.vertices=[];self.triangles=[];self.instances=[];self.seen=set()
        self.excluded_liquid_chunks=[];self.excluded_liquid_cells=[];self.excluded_wmo_groups=[]
        self.wmo_liquids = []
        self.wmo_liquid_cells = []

    def read(self,kind,fdid):return self.decoder.decode(kind,self.casc.read(fdid))

    def file_id(self,entry,obj,wmo=False):
        field='mwidEntry' if wmo else 'mmidEntry';flag=8 if wmo else 0x40
        if entry['flags']&flag:return entry[field]
        prefix='wmo' if wmo else 'm2'
        name=obj.get(prefix+'Names',{}).get(str(obj[prefix+'Offsets'][entry[field]]),'').lower().replace('.mdx','.m2')
        if name not in self.names:raise NavigationError('placement_name_unresolved:'+name)
        return self.names[name]

    def add(self,v,t,m,record):
        if len(t)==0:return
        v=(np.c_[v,np.ones(len(v))]@m.T)[:,:3];tri=v[np.asarray(t,int)]
        keep=(tri[:,:,:2].max(axis=1)>=self.roi[0]).all(axis=1)&(tri[:,:,:2].min(axis=1)<=self.roi[1]).all(axis=1)
        count=0
        for triangle in tri[keep]:
            poly=list(triangle)
            for axis in (0,1):
                for bound,sign in ((self.roi[0,axis],1),(self.roi[1,axis],-1)):
                    result=[]
                    for a,b in zip(poly,poly[1:]+poly[:1]):
                        ina=(a[axis]-bound)*sign>=0;inb=(b[axis]-bound)*sign>=0
                        if ina:result.append(a)
                        if ina!=inb:result.append(a+(b-a)*(bound-a[axis])/(b[axis]-a[axis]))
                    poly=result
            for i in range(1,len(poly)-1):
                a,b,c=poly[0],poly[i],poly[i+1]
                if np.linalg.norm(np.cross(b-a,c-a))<1e-9:continue
                if len(self.vertices)+3>2000000:raise NavigationError('scene_geometry_limit')
                n=len(self.vertices);self.vertices.extend([a,b,c]);self.triangles.append([n,n+1,n+2]);count+=1
        if count:self.instances.append(dict(record,triangles=count,transform=m.tolist()))

    def m2(self,fdid,m,record):
        data=self.read('m2',fdid)
        self.add(raw_vertices(data['vertices']),np.array(data['indices'],int).reshape(-1,3),m,dict(record,file_id=fdid,kind='m2_collision'))

    def wmo(self,fdid,m,entry):
        root=self.read('wmo',fdid)
        if not root.get('groupIDs') or len(root['groupIDs'])<root['groupCount']:
            raise NavigationError('wmo_group_file_ids_missing')
        for gid in root['groupIDs'][:root['groupCount']]:
            group=self.read('wmo',gid)
            # MOGP antiportals are visibility occluders, not physical walls.
            name=root.get('groupNames',{}).get(str(group.get('nameOfs')),'')
            if group.get('flags',0)&0x4000080 or name.lower()=='antiportal':
                self.excluded_wmo_groups.append(dict(parent=fdid,file_id=gid,
                    flags=group.get('flags',0),name=name,reason='non_physical_group'))
                continue
            if group.get('liquid'):
                cells = [cell for cell in wmo_liquid_cells(group['liquid'], m)
                         if (cell[:, :2].max(axis=0) >= self.roi[0]).all()
                         and (cell[:, :2].min(axis=0) <= self.roi[1]).all()]
                if cells:
                    self.wmo_liquids.extend(cells)
                    self.wmo_liquid_cells.append(dict(parent=fdid, file_id=gid, uid=entry['uniqueId'],
                                                      wet_cells=len(cells)))
            indices=np.array(group.get('indices',[]),int).reshape(-1,3);materials=group.get('materialInfo',[])
            if len(materials)!=len(indices):raise NavigationError('wmo_collision_flags_missing')
            flags=np.array([r['flags'] for r in materials],dtype=np.uint32)
            mask=(flags&8!=0)|((flags&0x20!=0)&(flags&4==0))
            self.add(raw_vertices(group.get('vertices',[])),indices[mask],m,dict(kind='wmo_collision',file_id=gid,parent=fdid,uid=entry['uniqueId']))
        # The global/default set plus the placement-selected set are both active.
        for index in sorted({0,entry['doodadSet']}):
            sets=root.get('doodadSets',[])
            if not sets and index==0:continue
            if index>=len(sets):raise NavigationError('wmo_doodad_set_missing')
            ds=sets[index]
            for i in range(ds['firstInstanceIndex'],ds['firstInstanceIndex']+ds['doodadCount']):
                d=root['doodads'][i]
                if root.get('fileDataIDs'):fid=root['fileDataIDs'][d['offset']]
                else:fid=self.names.get(root.get('doodadNames',{}).get(str(d['offset']),'').lower())
                if not fid:raise NavigationError('wmo_doodad_file_missing')
                self.m2(fid,m@quaternion_matrix(d['rotation'],d['position'],d['scale']),dict(parent=fdid,doodad=i))

    def remove_submerged_geometry(self):
        if not self.wmo_liquids or not self.triangles:
            return 0
        triangles = np.asarray(self.vertices).reshape(-1, 3, 3)
        lo, hi = triangles.min(axis=1), triangles.max(axis=1)
        changed = {}
        for cell in self.wmo_liquids:
            # Subdivide along the MLIQ height interpolation diagonal.
            for face in (cell[[0, 1, 2]], cell[[0, 2, 3]]):
                normal = np.cross(face[1] - face[0], face[2] - face[0])
                if normal[2] <= 1e-8:
                    raise NavigationError('wmo_liquid_orientation_invalid')
                face_lo, face_hi = face.min(axis=0), face.max(axis=0)
                intersecting = np.flatnonzero((lo[:, 2] <= face_hi[2])
                    & np.all(lo[:, :2] < face_hi[:2], axis=1) & np.all(hi[:, :2] > face_lo[:2], axis=1))
                edges = np.roll(face, -1, axis=0) - face
                inward = np.c_[-edges[:, 1], edges[:, 0], np.zeros(3)]
                planes = (*zip(inward, face), (-normal, face[0]))
                for index in intersecting:
                    dry = []
                    for polygon in changed.get(index, (triangles[index],)):
                        dry.extend(subtract_submerged(polygon, planes))
                    changed[index] = dry
        if not changed:
            return 0
        self.vertices, self.triangles = [], []
        first = 0
        for instance in self.instances:
            stop = first + instance['triangles']
            emitted = len(self.triangles)
            for index in range(first, stop):
                for polygon in changed.get(index, (triangles[index],)):
                    for second, third in zip(polygon[1:], polygon[2:]):
                        anchor = polygon[0]
                        if index in changed and np.linalg.norm(np.cross(second - anchor, third - anchor)) < 1e-9:
                            continue
                        if len(self.vertices) + 3 > 2000000:
                            raise NavigationError('scene_geometry_limit')
                        start = len(self.vertices)
                        self.vertices.extend((anchor, second, third))
                        self.triangles.append((start, start + 1, start + 2))
            instance['triangles'] = len(self.triangles) - emitted
            first = stop
        return len(changed)

    def extract(self,world_id,dbd,output,phase_id,*,wdt_file=None):
        if wdt_file is None:
            maprow=self.decoder.decode('map',self.casc.read(1349477),dbd=str(Path(dbd).resolve()),
                build=self.casc.build['Version'],world_id=world_id)
        else:
            maprow=dict(ID=world_id,WdtFileDataID=wdt_file,source='explicit_wdt_file')
        if not maprow or maprow.get('ID')!=world_id:raise NavigationError('world_map_db2_missing')
        wdt=self.read('wdt',maprow['WdtFileDataID'])
        if not wdt.get('entries') or wdt.get('worldModelPlacement'):raise NavigationError('global_wmo_map_unsupported')
        # WDT loader indexes entries as x*64+y (its disk loop is transposed).
        xmin,xmax=[math.floor((e+CENTER)/TILE) for e in self.roi[:,0]]
        ymin,ymax=[math.floor((CENTER-n)/TILE) for n in self.roi[::-1,1]]
        tiles=[]
        # Neighbor tiles may reference models whose bounds overlap this ROI.
        for x in range(max(0,xmin-1),min(64,xmax+2)):
            for y in range(max(0,ymin-1),min(64,ymax+2)):
                entry=wdt['entries'][x*64+y]
                if not entry['rootADT']:
                    if xmin<=x<=xmax and ymin<=y<=ymax:raise NavigationError('roi_terrain_tile_missing')
                    continue
                tiles.append(dict(x=x,y=y,**entry))
                if xmin<=x<=xmax and ymin<=y<=ymax:
                    adt=self.read('adt',entry['rootADT'])
                    liquids=adt.get('liquidChunks',[])
                    for i,ch in enumerate(adt['chunks']):
                        wet=liquid_cells(liquids[i]['instances']) if liquids else 0
                        if wet==ALL_TERRAIN_CELLS:
                            self.excluded_liquid_chunks.append([x,y,i]);continue
                        if wet:
                            self.excluded_liquid_cells.append(dict(x=x,y=y,chunk=i,mask=f'{wet:016x}'))
                        v,t=terrain(ch,wet);self.add(v,t,np.eye(4),dict(kind='terrain',file_id=entry['rootADT'],chunk=i))
                if not entry['obj0ADT']:raise NavigationError('tile_objects_missing')
                obj=self.read('obj',entry['obj0ADT'])
                for iswmo,models in ((False,obj.get('models',[])),(True,obj.get('worldModels',[]))):
                    for model in models:
                        key=(iswmo,model['uniqueId'])
                        if key in self.seen:continue
                        self.seen.add(key);m=placement_matrix(model,wmo=iswmo)
                        if iswmo:
                            bounds=np.array([model['lowerBounds'],model['upperBounds']]);b=np.c_[bounds[:,0]-CENTER,CENTER-bounds[:,2]]
                            if (b.max(axis=0)<self.roi[0]).any() or (b.min(axis=0)>self.roi[1]).any():continue
                        # M2 has no placement bounds: must load collision before culling.
                        fid=self.file_id(model,obj,iswmo)
                        if iswmo:self.wmo(fid,m,model)
                        else:self.m2(fid,m,dict(uid=model['uniqueId']))
        cut_candidates = self.remove_submerged_geometry()
        if not self.triangles:raise NavigationError('empty_roi_collision')
        output=Path(output);output.mkdir(parents=True,exist_ok=False)
        with (output/'collision.obj').open('w') as f:
            for v in self.vertices:f.write('v '+' '.join(f'{c:.8f}' for c in v)+'\n')
            for t in self.triangles:f.write('f '+' '.join(str(i+1) for i in t)+'\n')
        frame=Frame(self.casc.build['Version'],world_id,phase_id,'east_north_up')
        manifest=dict(schema=1,geometry_kind='collision',units='yards',up_axis='Z',frame=asdict(frame),
            evidence='local_client_collision_unverified_live_alignment',instances=[dict(path='collision.obj',transform=np.eye(4).tolist())])
        (output/'scene.json').write_text(json.dumps(manifest,indent=2))
        evidence=dict(schema=1,map_record=maprow,build=self.casc.build,roi=self.roi.tolist(),tiles=tiles,
            dependencies=dict(casclib=CASC_COMMIT,wow_export=EXPORT_COMMIT,casclib_patch_sha256=hashlib.sha256((ROOT/'native/nav3d/casc/readonly.patch').read_bytes()).hexdigest(),map_dbd_sha256=hashlib.sha256(Path(dbd).read_bytes()).hexdigest()),
            resources=list(self.casc.records.values()),instances=self.instances,excluded_liquid_chunks=self.excluded_liquid_chunks,
            excluded_liquid_cells=self.excluded_liquid_cells,
            wmo_liquid_cells=self.wmo_liquid_cells, wmo_liquid_candidate_triangles=cut_candidates,
            excluded_wmo_groups=self.excluded_wmo_groups,
            alignment_verified=False,phase_verified=False,dynamic_objects_included=False,reached_in_game=False)
        (output/'extraction.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        return output/'scene.json'
