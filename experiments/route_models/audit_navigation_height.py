"""Audit recorded public height values without inventing floor identities."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.perception.travel_channel import TravelFlags


def audit(path):
    samples=[];previous=None
    for line in path.read_text().splitlines():
        row=json.loads(line)
        if row.get('kind')!='quest_observation':continue
        nav=row.get('data',{}).get('navigation')
        if not isinstance(nav,dict):continue
        identity=(nav.get('map_id'),nav.get('sequence'))
        if identity==previous:continue
        previous=identity
        samples.append(nav)
    heights=[n['height'] for n in samples if type(n.get('height')) in (int,float) and math.isfinite(n['height'])]
    states={name:[n for n in samples if n.get('flags',0)&flag] for name,flag in
            [('flying',TravelFlags.FLYING),('falling',TravelFlags.FALLING),('swimming',TravelFlags.SWIMMING)]}
    return dict(run=path.parent.name,file=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        samples=len(samples),readable_height=len(heights),height_min=min(heights) if heights else None,
        height_max=max(heights) if heights else None,
        observed='missing' if not heights else 'constant' if min(heights)==max(heights) else 'variable',
        state_samples={key:len(value) for key,value in states.items()},
        floor_identity_verified=False)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runs',type=Path,default=ROOT/'data/runs')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    rows=[audit(p) for p in sorted(args.runs.glob('*/events.jsonl'))]
    rows=[r for r in rows if r['samples']]
    summary=dict(runs=len(rows),samples=sum(r['samples'] for r in rows),
        constant_runs=sum(r['observed']=='constant' for r in rows),
        variable_runs=sum(r['observed']=='variable' for r in rows),
        runs_with_state={name:sum(r['state_samples'][name]>0 for r in rows) for name in ('flying','falling','swimming')})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(summary=summary,runs=rows,
        scope='observed height variation only; equal values do not establish the same floor',
        game_inputs_sent=False),indent=2))
    print(json.dumps(summary))


if __name__=='__main__':main()
