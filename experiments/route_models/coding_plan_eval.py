"""Development-only model evaluation through the officially supported OpenCode tool.

No runtime integration: the production API client remains on the standard endpoint.
Each request uses an isolated scratch directory and denies all agent tools.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]


class CodingPlanEvaluationClient:
    def complete(self,prompt,*,model='glm-5.3-flash',images=(),max_tokens=4096,
                 timeout_s=90,effort='low'):
        if model not in {'glm-5.3','glm-5.3-flash'} or images and model=='glm-5.3':
            raise ValueError('coding_evaluation_model_modality_invalid')
        executable=ROOT/'data/glm-coding-tool/node_modules/.bin/opencode'
        if not executable.exists():raise ValueError('coding_evaluation_tool_not_installed')
        key=(ROOT/'glm/apikey.txt').read_text().strip()
        if not key or any(c.isspace() for c in key):raise ValueError('coding_evaluation_credentials_invalid')
        state=ROOT/'data/glm-coding-tool/state'
        env=dict(os.environ)
        env.update({'GLM_CODING_API_KEY':key,'OPENCODE_DISABLE_AUTOUPDATE':'true',
                    'OPENCODE_DISABLE_CLAUDE_CODE':'true'})
        for field,name in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),
                           ('XDG_CACHE_HOME','cache'),('XDG_STATE_HOME','state')]:
            location=state/name;location.mkdir(parents=True,exist_ok=True);env[field]=str(location)
        config={'enabled_providers':['zhipuai-coding-plan'],'permission':{'*':'deny'},
            'agent':{'evaluation':{'mode':'primary','steps':2,
                'prompt':'Evaluate the supplied navigation software test case. Return only the requested JSON. Do not use any tools.',
                'permission':{'*':'deny'}}},
            'provider':{'zhipuai-coding-plan':{'options':{
                'apiKey':'{env:GLM_CODING_API_KEY}',
                'baseURL':'https://open.bigmodel.cn/api/coding/paas/v4'},
                'models':{model:{'limit':{'context':1000000,'output':min(8192,max_tokens)},
                    'modalities':{'input':['text','image'] if model.endswith('flash') else ['text'],'output':['text']},
                    'options':{'thinking':{'type':'enabled'},'reasoning_effort':effort}}}}}}
        env['OPENCODE_CONFIG_CONTENT']=json.dumps(config)
        with tempfile.TemporaryDirectory(prefix='wow-glm-evaluation-') as folder:
            command=[str(executable),'run','--pure','--format','json','--agent','evaluation',
                     '--model','zhipuai-coding-plan/'+model,'--variant',effort,
                     '--title','WoW navigation offline evaluation','--dir',folder]
            for i,raw in enumerate(images):
                image=Path(folder)/f'image-{i}.png';image.write_bytes(raw)
                command+=['--file',str(image)]
            command+=['--',prompt]
            began=time.monotonic()
            try:
                result=subprocess.run(command,cwd=folder,env=env,capture_output=True,text=True,
                                      timeout=max(30,timeout_s)+20)
            except subprocess.TimeoutExpired:
                raise ValueError('coding_evaluation_timeout') from None
        events=[]
        for line in result.stdout.splitlines():
            try:events.append(json.loads(line.replace(key,'[REDACTED]')))
            except ValueError:pass
        answers=[event.get('part',{}).get('text','') for event in events if event.get('type')=='text']
        endings=[event.get('part',{}) for event in events if event.get('type')=='step_finish']
        errors=[event.get('error',{}) for event in events if event.get('type')=='error']
        output='\n'.join(answers)
        if not output:
            # Selected tool errors are useful; never expose an unfiltered process dump.
            detail=json.dumps(errors,ensure_ascii=False).replace(key,'[REDACTED]')[:1200]
            if not errors:detail=result.stderr.replace(key,'[REDACTED]')[-1200:]
            return {'raw_output':'','finish_reason':'error','model':model,'usage':None,
                'elapsed_s':round(time.monotonic()-began,3),'transport':'official_opencode_coding_plan',
                'tool_exit':result.returncode,'tool_error':detail}
        text=output.strip()
        if text.startswith('```') and text.endswith('```'):
            text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
        end=endings[-1] if endings else {}
        return {'raw_output':text,'finish_reason':'stop' if end.get('reason')=='stop' else end.get('reason','unknown'),
            'model':model,'usage':end.get('tokens'),'elapsed_s':round(time.monotonic()-began,3),
            'transport':'official_opencode_coding_plan','reasoning_effort':effort,
            'tool_exit':result.returncode,'tool_cost':end.get('cost'),
            'tool_calls_observed':sum(e.get('type')=='tool_use' for e in events)}
