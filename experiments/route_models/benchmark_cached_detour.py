"""Local-model benchmark on SAVED images; a replay clock preserves original poses.

No capture or input APIs. Outputs cannot be loaded as live execution receipts.
"""
import argparse
import io
import json
from pathlib import Path
import shutil
import sys
import time
from contextlib import redirect_stdout

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from experiments.route_models import segmentation_worker as worker


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    rows=[json.loads(line) for line in (args.run/'events.jsonl').read_text().splitlines()]
    jobs=[]
    for row in rows:
        if row['kind']!='quest_segmented_observation':continue
        folder=ROOT/'data/terrain'/('segmented-'+row['data']['frame_id'])
        plan=json.loads((folder/'plan.json').read_text())
        if plan['kind']!='route':continue
        target=args.output/row['data']['frame_id'];target.mkdir(exist_ok=True)
        shutil.copyfile(folder/'image.png',target/'image.png')
        job=json.loads((folder/'request.json').read_text())
        job.update(calibration_cache=str(args.cache.resolve()),session_id='offline-cache-replay')
        (target/'request.json').write_text(json.dumps(job,indent=2))
        jobs.append(dict(id=row['data']['frame_id'],images=[str(target/'image.png')],target=job))
    class ReplayClock:
        def monotonic(self):return self.stamp+time.perf_counter()-self.started
        def time(self):return time.time()
    clock=ReplayClock()
    def requests():
        for job in jobs:
            clock.stamp=job['target']['frame']['captured_at'];clock.started=time.perf_counter()
            yield json.dumps(job)
    worker.time=clock;worker.sys.stdin=requests();output=io.StringIO();began=time.perf_counter()
    with redirect_stdout(output):worker.main()
    replies=[json.loads(row) for row in output.getvalue().splitlines()]
    results=[]
    for reply in replies[1:]:
        if 'error' in reply:raise RuntimeError(reply)
        plan=reply['cases'][0]
        results.append({k:plan.get(k) for k in ('frame_id','kind','calibration_cache_status',
            'model_timings','registration','movement_budget_yards','execution_window')})
        assert plan['kind']=='route' and plan['calibration_cache_status']=='reused_with_current_image_validation'
    summary=dict(mode='historical_saved_images_with_local_model',game_inputs_sent=False,
                 wall_s=time.perf_counter()-began,results=results)
    (args.output/'benchmark.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps({'frames':len(results),'wall_s':summary['wall_s'],
                     'cache_reused':all(r['calibration_cache_status'].startswith('reused') for r in results)}))


if __name__=='__main__':main()
