import json
from types import SimpleNamespace

from experiments.route_models import coding_plan_eval


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(coding_plan_eval,'ROOT',tmp_path)
    key=tmp_path/'glm/apikey.txt';key.parent.mkdir();key.write_text('private-test-key')
    executable=tmp_path/'data/glm-coding-tool/node_modules/.bin/opencode'
    executable.parent.mkdir(parents=True);executable.touch()


def test_coding_evaluation_uses_supported_tool_and_disables_tools(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    def run(command,**kwargs):
        assert command[0].endswith('/.bin/opencode') and '--pure' in command
        assert '--file' in command and '--agent' in command
        assert command[command.index('--variant')+1]=='low'
        assert command[command.index('--title')+1]=='WoW navigation offline evaluation'
        assert 'private-test-key' not in str(command)
        config=json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
        assert config['permission']=={'*':'deny'}
        assert config['agent']['evaluation']['permission']=={'*':'deny'}
        assert config['agent']['evaluation']['steps']>1  # Avoid first-turn forced summary.
        options=config['provider']['zhipuai-coding-plan']['options']
        assert options['baseURL']=='https://open.bigmodel.cn/api/coding/paas/v4'
        assert options['apiKey']=='{env:GLM_CODING_API_KEY}'
        events=[{'type':'text','part':{'text':'```json\n{"ok":true}\n```'}},
                {'type':'step_finish','part':{'reason':'stop','tokens':{'total':42}}}]
        return SimpleNamespace(stdout='\n'.join(map(json.dumps,events)),stderr='',returncode=0)
    monkeypatch.setattr(coding_plan_eval.subprocess,'run',run)
    result=coding_plan_eval.CodingPlanEvaluationClient().complete('visual test',images=[b'png'])
    assert json.loads(result['raw_output'])=={'ok':True}
    assert result['tool_calls_observed']==0 and result['usage']['total']==42
    assert 'private-test-key' not in json.dumps(result)


def test_tool_failure_is_preserved_without_key_or_standard_api_fallback(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    calls=[]
    def run(*args,**kwargs):
        calls.append(True)
        return SimpleNamespace(stdout='',stderr='failure private-test-key',returncode=1)
    monkeypatch.setattr(coding_plan_eval.subprocess,'run',run)
    result=coding_plan_eval.CodingPlanEvaluationClient().complete('test',model='glm-5.3')
    assert len(calls)==1 and result['finish_reason']=='error'
    assert result['tool_error']=='failure [REDACTED]'
