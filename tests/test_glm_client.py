import io
import json
from urllib.error import HTTPError

import pytest

from bot.decision import glm_client
from bot.decision.glm_client import GLMClient,GLMError


def test_flash_uses_official_endpoint_and_enabled_thinking_without_logging_key(tmp_path,monkeypatch):
    key=tmp_path/'key';key.write_text('test-secret')
    captured=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self,_):
            return json.dumps({'model':'glm-5.3-flash','choices':[{'finish_reason':'stop',
                'message':{'content':'{"ok":true}','reasoning_content':'not persisted'}}],
                'usage':{'total_tokens':10}}).encode()
    class Opener:
        def open(self,request,timeout):
            assert request.full_url=='https://open.bigmodel.cn/api/paas/v4/chat/completions'
            assert request.get_header('Authorization')=='Bearer test-secret'
            payload=json.loads(request.data)
            assert payload['thinking']['type']=='enabled' and payload['reasoning_effort']=='low'
            assert payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
            captured.append(True);return Response()
    monkeypatch.setattr(glm_client,'build_opener',lambda *_:Opener())
    result=GLMClient(key).complete('test',images=[b'png'])
    assert captured and result['usage']['total_tokens']==10
    assert 'test-secret' not in json.dumps(result) and 'reasoning_content' not in result


def test_account_error_is_distinct_from_perception_failure_and_hides_error_body(tmp_path,monkeypatch):
    key=tmp_path/'key';key.write_text('test-secret')
    class Opener:
        def open(self,request,timeout):
            body=json.dumps({'error':{'code':'1113','message':'test-secret'}}).encode()
            raise HTTPError(request.full_url,429,'error',{},io.BytesIO(body))
    monkeypatch.setattr(glm_client,'build_opener',lambda *_:Opener())
    with pytest.raises(GLMError,match='^glm_http_429_code_1113$'):
        GLMClient(key).complete('test')


def test_text_only_model_never_sends_images_or_uses_another_providers_key(monkeypatch,tmp_path):
    monkeypatch.setenv('DEEPSEEK_API_KEY','must-not-use')
    monkeypatch.setattr(glm_client,'build_opener',lambda *_:pytest.fail('no request expected'))
    client=GLMClient(tmp_path/'missing')
    with pytest.raises(GLMError,match='text_only'):client.complete('test',model='glm-5.3',images=[b'png'])
    with pytest.raises(GLMError,match='credentials_unavailable'):client.complete('test')
