"""Render actual demo geometry/path data to a static diagnostic contact sheet.

Usage: .venv/bin/python experiments/navigation3d/render_preview.py data/nav3d/release-v1
This is an offline geometry render, not a screenshot of the HTML viewer/game.
"""
from pathlib import Path
import json
import math
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bot.nav3d import NavMesh
from bot.nav3d.geometry import load_scene


def render(folder):
    image=Image.new("RGB",(1600,1120),(17,25,35)); draw=ImageDraw.Draw(image)
    font_path="/System/Library/Fonts/Supplemental/Arial.ttf"
    def font(size):
        try: return ImageFont.truetype(font_path,size)
        except OSError: return ImageFont.load_default()
    draw.text((40,24),"WowAgent | 3D ground navigation v1",font=font(32),fill="#eef5fc")
    draw.text((40,70),"Real Recast/Detour on synthetic collision geometry. Not a live game test.",font=font(20),fill="#aabcce")
    for index,name in enumerate(("wall","door","ramp","stairs")):
        left=28+(index%2)*786; top=120+(index//2)*480
        draw.rounded_rectangle((left,top,left+758,top+450),radius=10,fill="#1b2736",outline="#38495d",width=2)
        _,v,t,_=load_scene(folder/name/"scene.json")
        nav=NavMesh.load(folder/name/"mesh").debug_triangles()
        route=json.loads((folder/name/"path.json").read_text())
        center=(v.min(axis=0)+v.max(axis=0))/2
        yaw,pitch=-.5,1.0
        def project(point):
            x,y,z=np.array(point)-center
            u=x*math.cos(yaw)-y*math.sin(yaw); q=x*math.sin(yaw)+y*math.cos(yaw)
            return (left+379+u*20,top+260+(q*math.cos(pitch)-z*math.sin(pitch))*20,
                    q*math.sin(pitch)+z*math.cos(pitch))
        faces=[]
        for tri in v[t]: faces.append(([project(p) for p in tri],False))
        for tri in nav: faces.append(([project(p+[0,0,.025]) for p in tri],True))
        faces.sort(key=lambda f:sum(p[2] for p in f[0]))
        overlay=Image.new("RGBA",image.size)
        painter=ImageDraw.Draw(overlay)
        for face,is_nav in faces:
            points=[(p[0],p[1]) for p in face]
            painter.polygon(points,fill=(43,148,154,190) if is_nav else (109,126,148,150),
                            outline=(86,194,198,210) if is_nav else (144,164,185,180))
        image=Image.alpha_composite(image.convert("RGBA"),overlay).convert("RGB");draw=ImageDraw.Draw(image)
        points=[project(p)[:2] for p in route["points"]]
        draw.line(points,fill="#ffd46f",width=4,joint="curve")
        for p,color in ((points[0],"#74e8b3"),(points[-1],"#ff7c80")):
            x,y=p;draw.ellipse((x-6,y-6,x+6,y+6),fill=color)
        titles={"wall":"01 / Around a wall","door":"02 / Through a doorway",
                "ramp":"03 / Ramp to upper floor","stairs":"04 / Stairs to upper floor"}
        draw.text((left+22,top+18),titles[name],font=font(25),fill="#eef5fc")
        draw.text((left+22,top+56),f"Complete path: {route['length']:.2f} yards",font=font(18),fill="#74e8b3")
    draw.text((40,1090),"Gray: collision geometry    Teal: walkable surface    Gold: planned 3D path",font=font(18),fill="#aabcce")
    output=folder/"overview.png";image.save(output);print(output)


if __name__=="__main__":
    render(Path(sys.argv[1]))
