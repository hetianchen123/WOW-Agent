"""Read-only real-screen Nav3D/legacy-channel capture and shadow validation."""
from dataclasses import asdict
import json
from pathlib import Path
import time
from ..core.config import load_config, Nav3DConfig
from ..perception.capture import ScreenCapture, frontmost_game_bounds, game_window_pixels
from ..perception.nav3d_channel import nav3d_region, decode_nav3d
from ..perception.travel_channel import travel_region, decode_travel
from .runtime import Nav3DCoordinator
from .planner import NavigationError


def probe_live(output, *, seconds=5, mesh=None, require_focus=True):
    if not 0<seconds<=30:raise NavigationError('probe_duration_out_of_bounds')
    output=Path(output)
    if output.exists():raise NavigationError('probe_output_exists')
    output.mkdir(parents=True)
    cfg=load_config();capture=ScreenCapture(cfg.capture);x,y=cfg.channel.x,cfg.channel.y
    forever=cfg.game_version=="1.60.1"
    nr,tr=nav3d_region(x,y),travel_region(x,y,supply_bars=forever)
    raised=nav3d_region(x,y,raised=forever)
    left,top=min(nr[0],tr[0]),min(raised[1],tr[1]);right=max(nr[0]+nr[2],tr[0]+tr[2])
    def crop(image,r):return image[r[1]-top:r[1]-top+r[3],r[0]-left:r[0]-left+r[2]]
    events=[];started=time.monotonic();rt=None;records=[];unique=set()
    if mesh:rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d-shadow',mesh=str(mesh)),now=started,log_event=lambda event,data:events.append(dict(event=event,data=data)))
    try:
        while time.monotonic()-started<seconds:
            if require_focus:
                frontmost_game_bounds()
                image=capture.grab(left,top,right-left,y-top)
            else:
                full=game_window_pixels(require_focus=False)
                from ..perception.calibration import find_rows
                candidates=[]
                for cx,cy in find_rows(full):
                    at=travel_region(cx,cy,supply_bars=forever)
                    if at[1]<0:continue
                    tn=decode_travel(full[at[1]:at[1]+at[3],at[0]:at[0]+at[2]])
                    if tn is None:continue
                    an=nav3d_region(cx,cy,raised=(tn.addon_build or 0)>=303)
                    pn=decode_nav3d(full[an[1]:an[1]+an[3],an[0]:an[0]+an[2]])
                    if pn is None:
                        an=nav3d_region(cx,cy)
                        pn=decode_nav3d(full[an[1]:an[1]+an[3],an[0]:an[0]+an[2]])
                    if pn:candidates.append((cx,cy,an,at))
                if len(candidates)!=1:raise NavigationError('live_window_channel_missing_or_ambiguous')
                x,y,nr,tr=candidates[0]
                raised=nav3d_region(x,y,raised=forever)
                left,top=min(nr[0],tr[0]),min(raised[1],tr[1]);right=max(nr[0]+nr[2],tr[0]+tr[2])
                image=full[top:y,left:right]
            nav=decode_travel(crop(image,tr))
            pose=decode_nav3d(crop(image,nr)) or decode_nav3d(crop(image,raised));now=time.monotonic()
            coherent=bool(pose and nav and pose.sequence==nav.sequence and pose.quest_id==nav.quest_id and pose.ui_map_id==nav.map_id)
            record=dict(elapsed=round(now-started,3),coherent=coherent,pose=asdict(pose) if pose else None,
                legacy=dict(quest_id=nav.quest_id,sequence=nav.sequence,map_id=nav.map_id,destination=nav.destination,position=nav.position,addon_build=nav.addon_build) if nav else None)
            if coherent:
                unique.add(pose.sequence)
                if rt:
                    cmd=rt.step(now,pose,nav);record['shadow_outcome']=cmd.outcome
            records.append(record)
            if len(records)==1:
                from PIL import Image
                Image.fromarray(image).save(output/'telemetry.png')
            time.sleep(.1)
    except Exception as exc:
        (output/'result.json').write_text(json.dumps(dict(status='blocked',reason=str(exc),
            movement_sent=False,reached_in_game=False),ensure_ascii=False,indent=2))
        raise
    finally:
        capture.close()
        (output/'samples.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        (output/'events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
    result=dict(capture='live_screen' if require_focus else 'live_game_window',require_focus=require_focus,samples=len(records),coherent=sum(r['coherent'] for r in records),distinct_sequences=len(unique),
        shadow_outcome=next((r['shadow_outcome'] for r in reversed(records) if r.get('shadow_outcome')),None),
        movement_sent=False,reached_in_game=False)
    (output/'result.json').write_text(json.dumps(result,indent=2));return result
