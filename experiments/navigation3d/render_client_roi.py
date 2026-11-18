"""Export a static diagnostic of real client geometry (not a game screenshot)."""
from pathlib import Path
import json
import sys
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bot.nav3d import NavMesh, Query, NavigationError
from bot.nav3d.geometry import load_scene
from bot.nav3d.runtime import SurfaceTracker


def render(folder):
    folder=Path(folder);_,v,t,_=load_scene(folder/'scene.json');mesh=NavMesh.load(folder/'mesh')
    tracker=SurfaceTracker(mesh);start=tracker.update((2100.6,-888.1));goal=(2133.,-854.)
    checks=[];paths=[]
    for label,xy in [('task',goal),('short A',(2098,-890)),('short B',(2103,-890)),('short C',(2095,-880))]:
        for p in tracker.candidates(xy):
            try:
                route=mesh.plan(Query(mesh.frame,start,p));paths.append(route)
                checks.append(dict(label=label,goal=p,length=route.length,status='complete',reached_in_game=False))
            except NavigationError as e:checks.append(dict(label=label,goal=p,status='rejected',reason=str(e),reached_in_game=False))
    (folder/'diagnostic-paths.json').write_text(json.dumps(dict(start=start,checks=checks),indent=2))
    img=Image.new('RGB',(1450,950),'#101923');d=ImageDraw.Draw(img)
    font=lambda size:ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',size)
    d.text((30,20),'Live client 2927 | collision and ground navigation',font=font(30),fill='#edf4fc')
    d.text((30,62),'Build 12.1.5.70961 | real local assets; paths computed offline; no game movement sent',font=font(18),fill='#acc0d0')
    roi=json.loads((folder/'extraction.json').read_text())['roi'];xmin,ymin=roi[0];xmax,ymax=roi[1]
    def xy(p):return (35+(p[0]-xmin)*7.6,865-(p[1]-ymin)*7.6)
    for tri in v[t]:
        if tri[:,2].min()<78:d.polygon([xy(p) for p in tri],fill='#253442',outline='#394956')
    for tri in sorted(mesh.debug_triangles(),key=lambda t:t[:,2].mean()):
        h=tri[:,2].mean()
        if h<78:
            d.polygon([xy(p) for p in tri],fill='#3c9b9b' if h<65 else '#8a7760')
    for route in paths:
        d.line([xy(p) for p in route.points],fill='#ffd46b',width=4)
    for p,c in [(start,'#7affb7'),(goal,'#ff7979')]:
        x,y=xy(p);d.ellipse((x-6,y-6,x+6,y+6),fill=c)
    d.text((810,140),'Observed player XY',font=font(23),fill='#7affb7')
    d.text((810,178),'E 2100.60   N -888.10',font=font(20),fill='white')
    d.text((810,215),f'Unique mesh support Z {start[2]:.2f}',font=font(20),fill='white')
    d.text((810,250),'Public native Z = 0 (untrusted)',font=font(19),fill='#a8bbc8')
    d.text((810,320),'Task pin: three candidate floors',font=font(23),fill='#ff9292')
    at=360
    for row in checks:
        if row['label']=='task':
            d.text((810,at),f"Z {row['goal'][2]:.2f} : no complete path",font=font(19),fill='white');at+=34
    d.text((810,520),'Short routes are computable',font=font(23),fill='#ffd46b')
    at=560
    for row in checks:
        if row['label']!='task' and row['status']=='complete':
            d.text((810,at),f"{row['label']}: {row['length']:.2f} yd",font=font(20),fill='white');at+=34
    d.text((810,720),'Teal / tan: walkable surfaces below Z 78',font=font(17),fill='#a8bbc8')
    d.text((810,750),'Gray: collision footprint   Gold: short paths',font=font(17),fill='#a8bbc8')
    d.text((35,905),'North is up. Missing connectivity and task floor remain unresolved. This image does not certify live traversal.',font=font(18),fill='#a8bbc8')
    img.save(folder/'diagnostic.png')
    print(folder/'diagnostic.png')

if __name__=='__main__':render(sys.argv[1])
