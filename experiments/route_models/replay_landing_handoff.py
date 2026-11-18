"""Replay saved local-model outputs at the landing handoff; never touch the game."""
import argparse
from dataclasses import fields,replace
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bot.control.building_access import BuildingAccess
from bot.core.config import SegmentedNavigationConfig
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import TravelSample,TravelFlags,TaskFlags,CameraSample,RecoverySample


def native(values):
    keys={f.name for f in fields(TravelSample)}
    values={k:v for k,v in values.items() if k in keys}
    for key in ('destination','position','world_size'):
        if values.get(key) is not None:values[key]=tuple(values[key])
    values['phase']=QuestState(values['phase']);values['flags']=TravelFlags(values['flags'])
    values['task']=TaskFlags(values.get('task',0))
    if values.get('camera'):values['camera']=CameraSample(**values['camera'])
    if values.get('recovery'):values['recovery']=RecoverySample(**values['recovery'])
    return TravelSample(**values)


def saved_cases(run):
    rows=[json.loads(s) for s in (run/'events.jsonl').read_text().splitlines()]
    cases=[]
    for row in rows:
        if row['kind']!='quest_building_surface':continue
        result=row['data'];folder=ROOT/'data/terrain'/('building-'+result['frame_id'])
        image=folder/'image.png'
        if hashlib.sha256(image.read_bytes()).hexdigest()!=result['image_sha256']:
            raise ValueError('historical_scene_hash_mismatch')
        observations=[r for r in rows if r['kind']=='quest_observation' and
            abs(r['ts']-row['ts'])<1.5 and r['data']['navigation']['position']==result['position']]
        if not observations:raise ValueError('historical_scene_native_pose_missing')
        nearest=min(observations,key=lambda r:abs(r['ts']-row['ts']))
        values=nearest['data']['navigation']
        if values['flags'] & int(TravelFlags.FLYING|TravelFlags.FALLING|TravelFlags.IN_COMBAT):
            raise ValueError('historical_surface_not_stable')
        cases.append(dict(result=result,nav=values,at=row['ts'],native_event=nearest['event_id'],
                          image=str(image)))
    return cases


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--restore-intent',type=Path)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    cases=saved_cases(args.run)
    roof=next(c for c in cases if c['result']['surface']=='structure')
    landing=[c for c in cases if c['result']['surface']=='unknown' and c['nav']['flags'] & int(TravelFlags.MOUNTED)]
    results=[]
    for case in landing:
        events=[];nav=native(case['nav'])
        ctx=SimpleNamespace(heading_age=lambda:0,capture=None,
                            log_event=lambda name,data:events.append(dict(kind=name,data=data)))
        class Request:
            def __init__(self,*_):
                self.frame_id=case['result']['frame_id'];self.folder=Path(case['image']).parent
                self.digest=case['result']['image_sha256']
            def poll(self):return case['result']
            def close(self):pass
        obj=BuildingAccess(SegmentedNavigationConfig(enabled=True),capture=lambda _:None,
                          convention='ccw_west',request_factory=Request)
        obj.remember_building(native(roof['nav']),image=roof['image'],
                             digest=roof['result']['image_sha256'],at=roof['at'])
        obj.step(ctx,1,nav,0)
        obj.step(ctx,1.6,replace(nav,sequence=(nav.sequence+1)%128),0)
        # The time/sequence here are simulated, while original model/pose are saved facts.
        action=obj.step(ctx,1.7,replace(nav,sequence=(nav.sequence+2)%128),0)
        assert action.tap==(nav.sky_dismount_key or '8') and action.outcome is None
        assert obj.state=='dismount' and obj.ground_only and not obj.points
        results.append(dict(frame=case['result']['frame_id'],native_event=case['native_event'],
            old_result='building_surface_unconfirmed',new_action=action.tap,new_state=obj.state,
            scene_classification_unchanged=case['result']['surface'],ground_only=obj.ground_only,
            exterior_verified=False,events=events))
        obj.close()
    if not results:raise ValueError('no_mount_occlusion_samples')
    if args.restore_intent:
        memory=BuildingAccess(SegmentedNavigationConfig(),capture=None,convention='ccw_west',
                              memory_path=args.restore_intent)
        memory.remember_building(native(roof['nav']),image=roof['image'],
                                digest=roof['result']['image_sha256'],at=roof['at'])
    report=dict(run=str(args.run),game_inputs_sent=False,model_rerun=False,
                receipts='simulated sequence/timing; original native pose and original model outputs',
                cases=results,live_building_entry_verified=False)
    (args.output/'handoff-replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(dict(cases=len(results),passed=True,restored_intent=bool(args.restore_intent))))


if __name__=='__main__':main()
