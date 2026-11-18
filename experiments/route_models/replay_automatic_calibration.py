"""Replay saved native calibration evidence; optionally install its local cache.

Reads only project screenshots/logs. Never captures the desktop or sends input.
"""
import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.perception.automatic_ground import AutomaticGround
from bot.perception.ground_projection import ProjectionFrame,ProjectionError
from bot.perception.calibration_cache import save_cache


def replay_run(run):
    rows=[json.loads(line) for line in (Path(run)/'events.jsonl').read_text().splitlines()]
    feedback=[r['data'] for r in rows if r['kind']=='quest_automatic_calibration_feedback']
    calibrator=AutomaticGround();states=[];created_at=None
    for row in rows:
        if row['kind']!='quest_segmented_observation':continue
        folder=ROOT/'data/terrain'/('segmented-'+row['data']['frame_id'])
        job=json.loads((folder/'request.json').read_text())
        if job.get('automatic') is not True:raise ProjectionError('replay_not_native_automatic')
        values=job['frame']
        for key in ('size','world_size','player_position','camera_signature'):values[key]=tuple(values[key])
        frame=ProjectionFrame(**values)
        rgb=cv2.cvtColor(cv2.imread(str(folder/'image.png')),cv2.COLOR_BGR2RGB)
        with np.load(folder/'masks.npz') as mask:
            observed=SimpleNamespace(ground=mask['ground'],semantic_confidence=mask['semantic_confidence'])
        projection,proof=calibrator.update(frame,rgb,observed,now=frame.captured_at+.1)
        states.append({'frame_id':frame.frame_id,**proof})
        if projection is not None:
            recorded=json.loads((folder/'plan.json').read_text())
            if not np.allclose(projection.matrix,recorded['projection_matrix'],atol=1e-8):
                raise ProjectionError('replay_changed_measured_geometry')
            positions={f.frame_id:f.player_position for f,_,_ in calibrator.measurements}
            for pair in [*calibrator.training,*calibrator.validation]:
                if not any(np.allclose(v['origin'],positions[pair.ids[0]],atol=1e-9,rtol=0)
                           and np.allclose(v['position'],positions[pair.ids[1]],atol=1e-9,rtol=0)
                           and v['before_sequence']!=v['after_sequence'] and v['moved']>=.15
                           for v in feedback):
                    raise ProjectionError('replay_native_receipt_missing')
            created_at=row['ts'];break
    if created_at is None:raise ProjectionError('replay_no_completed_calibration')
    return calibrator,created_at,states


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--install-cache',type=Path)
    args=parser.parse_args();began=time.monotonic()
    calibrator,created_at,states=replay_run(args.run)
    report=dict(run=args.run.name,mode='historical_native_replay',states=states,
                elapsed_s=time.monotonic()-began,game_inputs_sent=False)
    if args.install_cache:
        report['cache_sha256']=save_cache(args.install_cache,calibrator,created_at=created_at,
                                          provenance='recorded_live_run:'+args.run.name)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='states'}))


if __name__=='__main__':main()
