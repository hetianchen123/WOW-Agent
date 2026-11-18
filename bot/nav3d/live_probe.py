"""Read-only real-screen Nav3D/legacy-channel capture and shadow validation."""
from dataclasses import asdict
import json
from pathlib import Path
import time
from ..core.config import load_config, Nav3DConfig
from ..perception.capture import ScreenCapture, frontmost_game_bounds
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
    nr,tr=nav3d_region(x,y),travel_region(x,y)
    left,top=min(nr[0],tr[0]),min(nr[1],tr[1]);right=max(nr[0]+nr[2],tr[0]+tr[2])
    def crop(image,r):return image[r[1]-top:r[1]-top+r[3],r[0]-left:r[0]-left+r[2]]
    events=[];started=time.monotonic();rt=None;records=[];unique=set()
    if mesh:rt=Nav3DCoordinator(Nav3DConfig(mode='nav3d-shadow',mesh=str(mesh)),now=started,log_event=lambda event,data:events.append(dict(event=event,data=data)))
    try:
        while time.monotonic()-started<seconds:
            if require_focus:
                frontmost_game_bounds()
                image=capture.grab(left,top,right-left,y-top)
            else:
                # Window-only read is deliberately confined to this no-input probe.
                full=game_window_pixels()
                from ..perception.calibration import find_rows
                candidates=[]
                for cx,cy in find_rows(full):
                    an,at=nav3d_region(cx,cy),travel_region(cx,cy)
                    if min(an[1],at[1])<0:continue
                    pn=decode_nav3d(full[an[1]:an[1]+an[3],an[0]:an[0]+an[2]])
                    tn=decode_travel(full[at[1]:at[1]+at[3],at[0]:at[0]+at[2]])
                    if pn and tn:candidates.append((cx,cy,an,at))
                if len(candidates)!=1:raise NavigationError('live_window_channel_missing_or_ambiguous')
                x,y,nr,tr=candidates[0]
                left,top=min(nr[0],tr[0]),min(nr[1],tr[1]);right=max(nr[0]+nr[2],tr[0]+tr[2])
                image=full[top:y,left:right]
            nav=decode_travel(crop(image,tr));pose=decode_nav3d(crop(image,nr));now=time.monotonic()
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


def game_window_pixels():
    """Read a single WoW window, including while covered; cannot produce input."""
    import Quartz as cg
    from AppKit import NSWorkspace
    import numpy as np
    pids={a.processIdentifier() for a in NSWorkspace.sharedWorkspace().runningApplications()
          if a.bundleIdentifier()=="com.blizzard.worldofwarcraft"}
    windows=[w for w in cg.CGWindowListCopyWindowInfo(cg.kCGWindowListOptionOnScreenOnly,cg.kCGNullWindowID) or []
             if w.get('kCGWindowOwnerPID') in pids and w.get('kCGWindowLayer')==0
             and w.get('kCGWindowBounds',{}).get('Width',0)>=800]
    if len(windows)!=1:raise NavigationError('game_window_missing_or_ambiguous')
    image=cg.CGWindowListCreateImage(cg.CGRectNull,cg.kCGWindowListOptionIncludingWindow,windows[0]['kCGWindowNumber'],
        cg.kCGWindowImageBoundsIgnoreFraming|cg.kCGWindowImageShouldBeOpaque|cg.kCGWindowImageNominalResolution)
    if image is None:raise NavigationError('game_window_capture_failed')
    width,height=cg.CGImageGetWidth(image),cg.CGImageGetHeight(image)
    bitmap=cg.CGBitmapContextCreate(None,width,height,8,width*4,cg.CGColorSpaceCreateWithName(cg.kCGColorSpaceSRGB),cg.kCGImageAlphaNoneSkipLast)
    if bitmap is None:raise NavigationError('game_window_bitmap_failed')
    cg.CGContextDrawImage(bitmap,cg.CGRectMake(0,0,width,height),image)
    data=cg.CGBitmapContextGetData(bitmap).as_buffer(width*height*4)
    return np.frombuffer(data,np.uint8).reshape(height,width,4)[:,:,:3].copy()
