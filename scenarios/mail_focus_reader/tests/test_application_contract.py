"""Provider-seam denials are not Core owner/session authentication evidence."""
import json
from pathlib import Path
import pytest
PROVIDER = 'gmail_cbs_cleanroom_skill.'


@pytest.mark.parametrize('caller',['owner','member','child','guest',None])
def test_caller_payload_cannot_override_provider_denial(consumer,caller):
    consumer.state['selected_message_id']='m1'
    event={'record':{'id':'m1'}, 'role':caller, 'actor':'owner'}
    assert not consumer.act('e_archive',event,result={'ok':False,'error':'permission_denied'})
    assert consumer.calls[-1]==(PROVIDER+'portable_mutate_message',{'id':'m1','action':'archive'})
    assert consumer.state['selected_message_id']=='m1'


def test_semantic_identity_and_no_prototype_execution(consumer):
    intent=consumer.page['meta']['builder']['cbs_intent']
    assert intent['requirements']==[{'id':'mail_messages_manage','capability_ref':'capability:mail.messages.manage','contract_range':'^1.0.0','origin':'human_explicit'}]
    assert consumer.page['id']=='mail_focus_reader'
    def visit(value):
        if isinstance(value,dict):
            assert value.get('kind')!='resourceQuery'
            assert value.get('type')!='resourceOperation'
            for k,v in value.items():
                if k!='meta': visit(v)
        elif isinstance(value,list):
            for item in value: visit(item)
    visit(consumer.app)
    scenario=json.loads((Path(__file__).resolve().parents[1]/'scenario.json').read_text(encoding='utf-8'))
    assert 'application' not in scenario['ui'] or scenario['ui']['application']==consumer.app
    assert scenario['runtime']['skills']['required']==['gmail_cbs_cleanroom_skill']
