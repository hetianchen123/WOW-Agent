"""Dependency-free, offline 3D inspection of source geometry, navmesh and path."""
import json
from pathlib import Path

from .backend import NavigationError


def write_preview(path, scenes):
    if any(len(s["triangles"])+len(s["navmesh"]) > 200000 for s in scenes):
        raise NavigationError("preview_region_too_large")
    data = json.dumps(scenes, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HTML.replace("__SCENES__", data), encoding="utf-8")


HTML = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WowAgent · 三维导航验证</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#111923;color:#edf4fc;font:14px system-ui,sans-serif}
header{padding:24px 28px 18px;border-bottom:1px solid #324050}h1{font-size:24px;margin:0 0 8px}
p{color:#aabcce;margin:6px 0}main{display:grid;grid-template-columns:280px 1fr;height:calc(100vh - 113px);min-height:480px}
aside{padding:24px;border-right:1px solid #324050;overflow:auto}label{display:block;margin:18px 0 8px}
select,button{border:1px solid #42556b;background:#1c2939;color:#edf4fc;padding:9px;border-radius:6px}
select{width:100%}button{cursor:pointer;margin:8px 4px 8px 0}canvas{width:100%;height:100%;touch-action:none;display:block}
.stat{border-top:1px solid #324050;margin-top:20px;padding-top:16px;line-height:1.8;word-break:break-word}
.ok{color:#74e8b3}.fail{color:#ffbd73}.key{display:inline-block;width:10px;height:10px;margin-right:8px}
@media(max-width:720px){main{grid-template-columns:210px 1fr}aside{padding:12px}header{padding:16px}}
</style>
<header><h1>WowAgent · 三维导航验证</h1><p>拖动旋转 · 滚轮缩放 · 所有结果为离线几何验证，未证明游戏内走通</p></header>
<main><aside>
<label for="scene">测试场景</label><select id="scene"></select>
<label><input type="checkbox" id="geometry" checked> <span class="key" style="background:#73849a"></span>碰撞模型</label>
<label><input type="checkbox" id="mesh" checked> <span class="key" style="background:#409ba6"></span>可行走表面</label>
<label><input type="checkbox" id="route" checked> <span class="key" style="background:#ffd46f"></span>三维路径</label>
<button id="iso">斜视</button><button id="top">俯视</button><button id="side">侧视</button>
<div class="stat" id="stats"></div>
<div class="stat"><p>导航网格保留高度；重叠的楼层只有通过坡道或楼梯才能连接。</p><p>灰色：源碰撞表面<br>青色：角色可行走区域<br>黄色：完整路径<br>绿色 / 红色：起点 / 终点</p></div>
</aside><canvas id="canvas" aria-label="可旋转的三维碰撞地图和导航路线"></canvas></main>
<script>
const scenes=__SCENES__;
const $=id=>document.getElementById(id), canvas=$('canvas'), ctx=canvas.getContext('2d');
const names={wall:'绕开墙体',door:'穿过门洞',ramp:'坡道连接上下层',stairs:'沿楼梯上楼',stacked:'无连接的重叠楼层',steep:'陡坡拒绝',ceiling:'低净空拒绝',narrow:'窄通道拒绝'};
let current=0,yaw=-.7,pitch=.85,zoom=1,drag=null;
scenes.forEach((s,i)=>{let o=document.createElement('option');o.value=i;o.textContent=names[s.name]||s.name;$('scene').append(o)});
function update(){const s=scenes[current],r=s.route||{},el=$('stats');el.replaceChildren();
 const line=(text,cls)=>{let p=document.createElement('p');p.textContent=text;if(cls)p.className=cls;el.append(p)};
 line(r.status==='complete'?'完整路径已生成':'未生成可行路径',r.status==='complete'?'ok':'fail');
 if(r.reason)line('原因：'+r.reason);if(r.length!=null)line('长度：'+r.length.toFixed(2)+' 码');
 line('路点：'+(r.points||[]).length);line('构建：'+s.frame.client_build);line('场景：'+s.frame.phase_id);draw();}
function draw(){let rect=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1,w=rect.width,h=rect.height;
 if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr)}
 ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle='#111923';ctx.fillRect(0,0,w,h);
 const s=scenes[current],v=s.vertices,lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];
 for(const p of v)for(let k=0;k<3;k++){lo[k]=Math.min(lo[k],p[k]);hi[k]=Math.max(hi[k],p[k])}
 const center=lo.map((x,k)=>(x+hi[k])/2),scale=Math.min(w,h)*.65/Math.max(...hi.map((x,k)=>x-lo[k]),1)*zoom;
 function project(p){let [x,y,z]=p.map((v,k)=>v-center[k]),u=x*Math.cos(yaw)-y*Math.sin(yaw),q=x*Math.sin(yaw)+y*Math.cos(yaw);
 return [w/2+u*scale,h/2+(q*Math.cos(pitch)-z*Math.sin(pitch))*scale,q*Math.sin(pitch)+z*Math.cos(pitch)];}
 let polygons=[];
 if($('geometry').checked)s.triangles.forEach(t=>polygons.push({p:t.map(i=>project(v[i])),fill:'rgba(111,132,156,.25)',stroke:'rgba(163,183,205,.22)'}));
 if($('mesh').checked)s.navmesh.forEach(t=>polygons.push({p:t.map(p=>project([p[0],p[1],p[2]+.025])),fill:'rgba(39,157,161,.44)',stroke:'rgba(76,198,194,.42)'}));
 polygons.sort((a,b)=>a.p.reduce((n,p)=>n+p[2],0)-b.p.reduce((n,p)=>n+p[2],0));
 for(const f of polygons){ctx.beginPath();f.p.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.closePath();ctx.fillStyle=f.fill;ctx.fill();ctx.strokeStyle=f.stroke;ctx.lineWidth=.7;ctx.stroke();}
 const route=s.route?.points||[];
 if($('route').checked&&route.length){ctx.beginPath();route.forEach((p,i)=>{p=project(p);i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])});ctx.strokeStyle='#ffd46f';ctx.lineWidth=3;ctx.stroke();
 [route[0],route.at(-1)].forEach((p,i)=>{p=project(p);ctx.beginPath();ctx.arc(p[0],p[1],5,0,Math.PI*2);ctx.fillStyle=i?'#ff7c80':'#74e8b3';ctx.fill()});}
 ctx.fillStyle='#aabcce';ctx.font='12px system-ui';ctx.fillText('XYZ · Z 向上 · 单位：码',18,h-18);}
$('scene').onchange=e=>{current=Number(e.target.value);zoom=1;update()};
['geometry','mesh','route'].forEach(id=>$(id).onchange=draw);
$('iso').onclick=()=>{yaw=-.7;pitch=.85;zoom=1;draw()};$('top').onclick=()=>{yaw=0;pitch=Math.PI/2;draw()};$('side').onclick=()=>{yaw=0;pitch=.05;draw()};
canvas.onpointerdown=e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId)};
canvas.onpointermove=e=>{if(drag){yaw+=(e.clientX-drag[0])*.008;pitch=Math.max(.04,Math.min(Math.PI/2,(pitch+(e.clientY-drag[1])*.008)));drag=[e.clientX,e.clientY];draw()}};
canvas.onpointerup=canvas.onpointercancel=()=>drag=null;
canvas.onwheel=e=>{e.preventDefault();zoom=Math.max(.3,Math.min(6,zoom*Math.exp(-e.deltaY*.001)));draw()};
new ResizeObserver(draw).observe(canvas);update();
</script></html>'''
