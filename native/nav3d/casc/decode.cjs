// Headless adapter to unmodified, pinned MIT wow.export binary readers.
// No renderer, textures, animations, network, or execution of client assets.
const fs = require('fs'), path = require('path'), Module = require('module');
const base = path.resolve(process.argv[2], 'src/js');
const modules = new Map();
let request;
const unavailable = () => { throw Error('headless_operation_unavailable'); };
const core = {view:{config:{},casc:{
  getBuildName:()=>request.build,
  getVirtualFileByName:async()=>new BufferWrapper(fs.readFileSync(request.path)),
  cache:{getFile:async()=>new BufferWrapper(fs.readFileSync(request.dbd))}
}}};
const stubs = {'core':core,'log':{write:()=>{}},'generics':{downloadFile:unavailable},
 'constants':{GAME:{MAP_SIZE:64,MAP_SIZE_SQ:4096},CACHE:{DIR_DBD:'dbd'},MAGIC:{MD20:0x3032444d}},
 'casc/listfile':{getByID:()=>undefined,getByFilename:()=>undefined},
 'casc/export-helper':{replaceExtension:x=>x.replace(/\.[^.]+$/,'')}};
function load(file) {
 file=path.resolve(file);
 const rel=path.relative(base,file).replace(/\.js$/,'');
 if (Object.hasOwn(stubs,rel)) return stubs[rel];
 if (modules.has(file)) return modules.get(file).exports;
 if (!file.startsWith(base+path.sep)) throw Error('reader_outside_pinned_source');
 const mod=new Module(file); modules.set(file,mod);mod.filename=file;
 mod.require=name=>name==='webp-wasm'?{decode:unavailable}:name.startsWith('.')?load(path.resolve(path.dirname(file),name+'.js')):require(name);
 mod._compile(fs.readFileSync(file,'utf8'),file);return mod.exports;
}
const BufferWrapper=load(path.join(base,'buffer.js'));
function clean(obj) {
 return JSON.parse(JSON.stringify(obj,(key,value)=>['data','handlers','groups'].includes(key)?undefined:typeof value==='bigint'?value.toString():value));
}
async function decode(req) {
 request=req; const data=new BufferWrapper(fs.readFileSync(req.path));
 if(req.kind==='map') {
  const Reader=load(path.join(base,'db/WDCReader.js'));const r=new Reader('Map.db2');await r.parse();return r.getRow(req.world_id);
 }
 const types={wdt:'WDT',adt:'ADT',obj:'ADT',wmo:'WMO',m2:'M2'};
 const Reader=load(path.join(base,`3D/loaders/${types[req.kind]}Loader.js`));const r=new Reader(data);
 if(req.kind==='wdt')r.load();
 else if(req.kind==='adt')r.loadRoot();
 else if(req.kind==='obj')r.loadObj();
 else if(req.kind==='wmo') {
  await r.load();
  const raw=fs.readFileSync(req.path);
  function chunks(start,end) {
   for(let at=start;at+8<=end;) {
    const tag=raw.readUInt32LE(at),size=raw.readUInt32LE(at+4),data=at+8,next=data+size;
    if(next>end)throw Error('wmo_chunk_bounds');
    if(tag===0x4d4f4750)chunks(data+68,next); // MOGP
    if(tag===0x4d4f5658) { if(size%4)throw Error('wmo_movx_size'); r.indices=Array.from({length:size/4},(_,i)=>raw.readUInt32LE(data+i*4)); }
    if(tag===0x4d505932) { if(size%4)throw Error('wmo_mpy2_size'); r.materialInfo=Array.from({length:size/4},(_,i)=>({flags:raw.readUInt16LE(data+i*4),materialID:raw.readUInt16LE(data+i*4+2)})); }
    at=next;
   }
  }
  chunks(0,raw.length);
 }
 else if(req.kind==='m2') {
  // Collision header in MD20 v264+, or the MD21 wrapper. Skip all render data.
  const raw=fs.readFileSync(req.path);let ofs=0;
  if(raw.toString('ascii',0,4)!=='MD20') {
   while(ofs+8<=raw.length && raw.toString('ascii',ofs,ofs+4)!=='MD21')ofs+=8+raw.readUInt32LE(ofs+4);
   if(ofs+8>raw.length)throw Error('m2_md21_missing');ofs+=8;
  }
  if(raw.toString('ascii',ofs,ofs+4)!=='MD20'||raw.readUInt32LE(ofs+4)<264)throw Error('m2_version_unsupported');
  data.seek(ofs+160);r.parseChunk_MD21_collision(ofs);
  return {vertices:r.collisionPositions,indices:r.collisionIndices,bounds:r.collisionBox};
 }
 return clean(r);
}
const rl=require('readline').createInterface({input:process.stdin,crlfDelay:Infinity});
(async()=>{for await(const line of rl){try{process.stdout.write(JSON.stringify({ok:true,data:await decode(JSON.parse(line))})+'\n');}catch(e){process.stdout.write(JSON.stringify({ok:false,error:e.stack})+'\n');}}})();
