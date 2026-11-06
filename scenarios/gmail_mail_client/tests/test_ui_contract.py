"""Closure semantics, deliberately excluding Forge release bookkeeping."""
import json
from pathlib import Path

import yaml

SCENARIO = Path(__file__).resolve().parents[1]
WORKSPACE = SCENARIO.parents[1]


def load():
    ui = json.loads((SCENARIO / 'webui.json').read_text(encoding='utf-8'))
    skill = yaml.safe_load((WORKSPACE / 'skills/gmail_mail_client_provider_skill/skill.yaml').read_text(encoding='utf-8'))
    project = yaml.safe_load((WORKSPACE / 'projects/gmail_mail_client/project.yaml').read_text(encoding='utf-8'))
    return ui, skill, project


def walk(value):
    if isinstance(value,dict):
        yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value,list):
        for child in value: yield from walk(child)


def test_every_binding_targets_an_exported_owned_tool():
    ui,skill,_ = load()
    provider = yaml.safe_load((WORKSPACE / 'skills/gmail_mail_client_provider_skill/contracts/provider.cbs.yaml').read_text(encoding='utf-8'))
    declared={x['name']:x for x in skill['tools']}
    for value in walk(ui):
        target=value.get('target') if value.get('type')=='callSkill' else value.get('name') if value.get('kind')=='skill' else None
        if target:
            owner,name=target.split('.',1)
            assert owner=='gmail_mail_client_provider_skill'
            assert name in declared and name in skill['exports']['tools']
            if value.get('kind')=='skill': assert declared[name]['side_effects']=='none'
    assert {x['tool'] for x in provider['capability']['operations']} <= set(declared)


def test_permission_profile_matches_tools_and_no_local_roles():
    _,skill,project=load()
    profile=project['permission_profile']
    permissions={x['id'] for x in profile['required']}
    assert set(skill['capabilities']) <= permissions
    for tool in skill['tools']: assert set(tool['permissions']) <= permissions
    assert not project.get('application_roles')
    assert not profile['secrets'] and not profile['background_actions'] and not profile['llm_model_use']
    assert profile['external_providers'][0]['scopes']==['https://www.googleapis.com/auth/gmail.modify']
    assert (WORKSPACE/'skills/gmail_mail_client_provider_skill/tests/test_application_contract.py').is_file()


def test_accepted_layout_and_semantic_requirement_preserved():
    ui,_,_=load()
    page=ui['ui']['application']['desktop']['pageSchema']
    assert page['layout']['pattern']=='document'
    assert page['layout']['regions'][0]['presentation']=={'compact':'stack','wide':'pane'}
    widgets={x['id']:x for x in page['widgets']}
    assert set(widgets) >= {'mail-topbar-actions','connection-banner','mail-tabs','query-toolbar','label-selector','message-list','message-details','automation-disclosure'}
    intent=page['meta']['builder']['cbs_intent']['requirements'][0]
    assert intent['capability_ref']=='capability:mail.messages.manage' and intent['contract_range']=='^1.0.0'
    assert 'compose-modal' in ui['ui']['application']['modals']
    assert widgets['message-list']['dataSource']['kind']=='skill'
    assert widgets['message-details']['dataSource']['params']['id']=='$state.selectedMessageId'
    assert 'filters' not in widgets['message-list']['inputs']  # Gmail owns full-query filtering.
    assert not any(x.get('kind')=='static' and x.get('value') for x in walk(ui))
    scenario=json.loads((SCENARIO/'scenario.json').read_text(encoding='utf-8'))
    assert scenario['ui'].get('application',ui['ui']['application'])==ui['ui']['application']


def test_compose_and_connection_use_success_gated_sequences():
    ui,_,_=load()
    app=ui['ui']['application']; widgets={x['id']:x for x in app['desktop']['pageSchema']['widgets']}
    compose=[x for x in widgets['mail-topbar-actions']['actions'] if x['on']=='click:action-compose']
    assert [x['type'] for x in compose]==['callSkill','openModal']
    actions=app['modals']['compose-modal']['schema']['widgets'][0]['actions']
    send=next(x for x in actions if x['id']=='compose-send')
    assert send['params']=={'command_id':'$state.sendCommand.command_id','values':'$event.values'}
    connect=[x for x in widgets['connection-banner']['actions'] if x['on']=='click:btn-connect']
    assert [x['type'] for x in connect]==['callSkill','openUrl','updateState']
    assert connect[0]['invalidates']==['gmail.connection']
    assert connect[1]['params']['withAuth'] is False
    assert connect[2]['params']=={'gmailConnection':{}}
    for node in walk(ui):
        if node.get('type')=='updateState':
            assert not {'body','payload','raw_base64url','access_token','refresh_token'} & set(node.get('params',{}))


def test_causal_routes_and_no_live_polling():
    ui,skill,_=load()
    routes={x['surface']:x for x in skill['data_routes']}
    for widget in ui['ui']['application']['desktop']['pageSchema']['widgets']:
        if widget.get('dataSource',{}).get('kind')=='skill':
            route=routes['widget:'+widget['id']]
            assert route['tool']==widget['dataSource']['name'].split('.')[1]
            assert route['read_policy']['preserve_last_value'] is False
            assert 'targeted_invalidation' in route['read_policy']['triggers']
    assert 'poll' not in json.dumps(skill['data_routes']).lower()


def test_pagination_buttons_have_independent_state_updates():
    ui, _, _ = load()
    page = ui['ui']['application']['desktop']['pageSchema']
    widget = next(x for x in page['widgets'] if x['id'] == 'mail-pagination')
    for button_id, token in [('next-page', '$event.record.nextPageToken'), ('first-page', '')]:
        assert any(button['id'] == button_id for button in widget['inputs']['buttons'])
        steps = [action for action in widget['actions'] if action['on'] == f'click:{button_id}']
        assert len(steps) == 1
        assert steps[0]['type'] == 'updateState'
        assert steps[0]['params'] == {'pageToken': token, 'selectedMessageId': ''}
        if button_id == 'next-page':
            assert steps[0]['enabledIf'] == "$event.record.nextPageToken !== ''"
        else:
            assert 'enabledIf' not in steps[0]
    assert not any(action['on'] == 'submit' for action in widget['actions'])


def test_widget_events_do_not_write_the_same_state_key_twice():
    ui, _, _ = load()
    for node in walk(ui):
        writes = set()
        for action in node.get('actions', []):
            if action.get('type') != 'updateState':
                continue
            for key in action.get('params', {}):
                write = (action['on'], key)
                assert write not in writes, (node.get('id'), write)
                writes.add(write)
