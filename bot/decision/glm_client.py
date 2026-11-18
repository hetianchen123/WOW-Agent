"""Explicit GLM experiment client. Never falls back to another provider's key."""
import base64
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from ..core.paths import ROOT
from .route_planner import NoRedirect


class GLMError(ValueError):
    pass


class GLMClient:
    def __init__(self, key_file=None):
        self.key_file=Path(key_file) if key_file else ROOT/'glm/apikey.txt'

    def complete(self,prompt,*,model='glm-5.3-flash',images=(),max_tokens=4096,
                 timeout_s=60,effort='low'):
        if model not in {'glm-5.3','glm-5.3-flash'} or effort not in {'low','high','max'}:
            raise GLMError('glm_model_or_effort_invalid')
        if images and model=='glm-5.3':
            raise GLMError('glm_5_3_text_only')
        try:
            key=self.key_file.read_text().strip()
        except OSError:
            raise GLMError('glm_credentials_unavailable') from None
        if not key or any(c.isspace() for c in key):
            raise GLMError('glm_credentials_invalid')
        content=[{'type':'text','text':prompt}]
        for raw in images:
            content.append({'type':'image_url','image_url':{
                'url':'data:image/png;base64,'+base64.b64encode(raw).decode()}})
        payload={'model':model,'messages':[{'role':'user','content':content}],
            'thinking':{'type':'enabled','clear_thinking':False},'reasoning_effort':effort,
            'response_format':{'type':'json_object'},'temperature':1,'top_p':.95,
            'max_tokens':min(8192,max(256,max_tokens)),'stream':False}
        request=Request('https://open.bigmodel.cn/api/paas/v4/chat/completions',
            data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',
                                                       'Authorization':'Bearer '+key})
        began=time.monotonic()
        try:
            with build_opener(NoRedirect()).open(request,timeout=min(120,max(1,timeout_s))) as response:
                raw=response.read(512*1024+1)
            if len(raw)>512*1024:raise GLMError('glm_response_too_large')
            data=json.loads(raw);choice=data['choices'][0]
            answer=choice['message']['content']
            if not isinstance(answer,str):raise GLMError('glm_content_missing')
        except HTTPError as exc:
            # Record only a bounded provider error code, never headers/error body.
            code='unknown'
            try:
                body=json.loads(exc.read(16384))
                candidate=str(body.get('error',{}).get('code','unknown'))
                if candidate.isalnum() and len(candidate)<32:code=candidate
            except (ValueError,TypeError,AttributeError,OSError):pass
            raise GLMError(f'glm_http_{exc.code}_code_{code}') from None
        except (URLError,TimeoutError,OSError):
            raise GLMError('glm_transport_error') from None
        except (KeyError,IndexError,TypeError,ValueError) as exc:
            if isinstance(exc,GLMError):raise
            raise GLMError('glm_response_invalid') from None
        return {'model':data.get('model',model),'raw_output':answer.replace(key,'[REDACTED]'),
                'finish_reason':choice.get('finish_reason'),'usage':data.get('usage'),
                'elapsed_s':round(time.monotonic()-began,3),'reasoning_effort':effort}
