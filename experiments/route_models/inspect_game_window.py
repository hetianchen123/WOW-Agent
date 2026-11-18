"""Read the actual WoW window by bundle/PID, never by a localized app name."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def inspect(output):
    import Quartz as q
    from AppKit import NSWorkspace
    from ApplicationServices import AXIsProcessTrusted
    import cv2
    import numpy as np

    output.mkdir(parents=True, exist_ok=True)
    apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications()
            if a.bundleIdentifier() == 'com.blizzard.worldofwarcraft']
    pids = {int(a.processIdentifier()) for a in apps}
    windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or []
    candidates = [w for w in windows if w.get('kCGWindowOwnerPID') in pids
        and w.get('kCGWindowLayer') == 0 and w.get('kCGWindowBounds', {}).get('Width', 0) >= 800
        and w.get('kCGWindowBounds', {}).get('Height', 0) >= 500]
    info = dict(checked_at=datetime.now(timezone.utc).isoformat(), selection='bundle_id_then_owner_pid',
        screen_permission=bool(q.CGPreflightScreenCaptureAccess()),
        caller_accessibility=bool(AXIsProcessTrusted()), game_inputs_sent=False,
        running=bool(apps), active=any(a.isActive() for a in apps), game_window_count=len(candidates))
    if candidates:
        window = candidates[0]
        info.update(owner=str(window.get('kCGWindowOwnerName')), bounds=dict(window['kCGWindowBounds']))
        shot = q.CGWindowListCreateImage(q.CGRectNull, q.kCGWindowListOptionIncludingWindow,
            window['kCGWindowNumber'], q.kCGWindowImageBoundsIgnoreFraming
            | q.kCGWindowImageShouldBeOpaque | q.kCGWindowImageNominalResolution)
        if shot is not None:
            width, height = q.CGImageGetWidth(shot), q.CGImageGetHeight(shot)
            bitmap = q.CGBitmapContextCreate(None, width, height, 8, width*4,
                q.CGColorSpaceCreateWithName(q.kCGColorSpaceSRGB), q.kCGImageAlphaNoneSkipLast)
            q.CGContextDrawImage(bitmap, q.CGRectMake(0, 0, width, height), shot)
            buf = q.CGBitmapContextGetData(bitmap).as_buffer(width*height*4)
            rgb = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()
            path = output/'current-window.png'
            cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            info.update(screenshot=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (output/'window-status.json').write_text(json.dumps(info, indent=2))
    return info


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    print(json.dumps(inspect(parser.parse_args().output)))
