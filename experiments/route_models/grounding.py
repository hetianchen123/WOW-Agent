"""Same-scene grid grounding experiment, no game input or hidden manual path hints."""
from pathlib import Path
import sys,json,time
from dataclasses import replace
from threading import Event
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from bot.core.config import load_config
from bot.decision.route_planner import DeepSeekRouteClient

OUT=ROOT/'data/terrain/deepseek-validation-20261117'
PROMPT='''The two images show the SAME World of Warcraft ground scene: plain and a coordinate grid.
Columns A..P go left to right; rows 1..9 top to bottom. Cells are image regions,
NOT world-coordinate squares. Identify actual objects before choosing a walking path.
The player is the small red-armored upright humanoid near the central camera anchor,
not the lava quadruped pet. The goal is the large armored NPC underneath the leftmost
nearby yellow question mark. A question mark is above the NPC and is NOT its feet.
Identify the WOODEN barricade physically between player and goal, its full extent
including posts, and which END connects to visible grass that leads around it.
Route on ground around an END, not over pointed tips or across solid wooden posts.
Bushes, grass tufts, pets and characters are not static wooden obstacles. Do not
fabricate precision or a path through hidden terrain. No action/code/world distances.
Return concise JSON only: player_cell, goal_cell (cells containing their FEET),
wood_cells (cells actually containing wooden barricade),
route_cells (ordered adjacent cells from player via clear ground to NPC),
explanation (short actual visible bypass reasoning), uncertain_cells.
If feet or a connection are hidden, say unknown and explain instead of guessing.
The output is a visual candidate, not an executable or verified world route.'''

def run():
 OUT.mkdir(parents=True,exist_ok=True)
 font=ImageFont.truetype('/System/Library/Fonts/Monaco.ttf',12)
 for name,source,full in [('latest','data/screenshots/20261116_215414_camera_after_interact.png',True),('1917','data/terrain/20261116_191810_825d2ec2/right.png',False)]:
  image=Image.open(ROOT/source).convert('RGB')
  if full:image=image.crop((338,78,1350,664))
  bounds=(100,200,880,535)
  image=image.crop(bounds)
  image.save(OUT/(name+'-ground.png'))
  grid=image.copy();d=ImageDraw.Draw(grid)
  for c in range(17):d.line([(c*grid.width/16,0),(c*grid.width/16,grid.height)],fill=(120,170,180),width=1)
  for r in range(10):d.line([(0,r*grid.height/9),(grid.width,r*grid.height/9)],fill=(120,170,180),width=1)
  for r in range(9):
   for c in range(16):d.text((c*grid.width/16+2,r*grid.height/9+1),chr(65+c)+str(r+1),font=font,fill='white',stroke_width=1,stroke_fill='black')
  grid.save(OUT/(name+'-grid.png'))
  files=[OUT/(name+'-ground.png'),OUT/(name+'-grid.png')]
  response=DeepSeekRouteClient(replace(load_config().route_planner,request_timeout_s=20,max_tokens=1100)).plan(
   [p.read_bytes() for p in files],{'image_size':image.size,'same_exposure':True},Event(),instruction=PROMPT)
  record={'source':source,'scene_crop':bounds,'instruction':PROMPT,**response}
  (OUT/(name+'-grid-result.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2))
  print(name,json.dumps(response,ensure_ascii=False),flush=True)
if __name__=='__main__':run()
