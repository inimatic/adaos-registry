"""Application closure checks; no provider IO or release admission is performed."""
import ast
import json
from pathlib import Path

import yaml


SCENARIO = Path(__file__).resolve().parents[1]
ROOT = SCENARIO.parents[1]
SKILL = ROOT / 'skills/gmail_cbs_cleanroom_skill'
PROJECT = ROOT / 'projects/gmail_cbs_cleanroom'


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding='utf-8'))


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def test_ui_tools_resolve_to_owned_exports_and_signatures():
    manifest = read_yaml(SKILL / 'skill.yaml')
    declared = {tool['name']: tool for tool in manifest['tools']}
    source = ast.parse((SKILL / 'handlers/main.py').read_text(encoding='utf-8'))
    functions = {node.name: node for node in source.body if isinstance(node, ast.FunctionDef)}
    webui = json.loads((SCENARIO / 'webui.json').read_text(encoding='utf-8'))
    calls = []
    for node in nodes(webui):
        if node.get('type') == 'callSkill':
            calls.append((node['target'], node.get('params', {})))
            assert isinstance(node['invalidates'], list)
        if node.get('kind') == 'skill':
            calls.append((node['name'], node.get('params', {})))
    assert calls
    for target, params in calls:
        owner, name = target.split('.')
        assert owner == manifest['name']
        assert name in manifest['exports']['tools']
        entry = declared[name]['entry'].split(':')[1]
        function = functions[entry]
        arguments = function.args.args
        allowed = {argument.arg for argument in arguments + function.args.kwonlyargs}
        required = {argument.arg for argument in arguments[:len(arguments) - len(function.args.defaults)]}
        assert set(params) <= allowed
        assert required <= set(params)


def test_mutations_keep_selection_drafts_and_causal_refresh():
    webui = json.loads((SCENARIO / 'webui.json').read_text(encoding='utf-8'))
    actions = [node for node in nodes(webui) if node.get('type') == 'callSkill']
    mutations = [a for a in actions if a['target'].endswith('.mutate_message')]
    assert {a['params']['action'] for a in mutations} >= {'unread', 'starred', 'archive', 'trash', 'label'}
    for action in mutations:
        assert action['params']['id'] == '$event.record.id'
        assert 'gmail.mail' in action['invalidates']
    sends = [a for a in actions if a['target'].endswith('.send_message')]
    assert len(sends) == 1
    assert sends[0]['params']['command_id'] == '$state.sendCommand.command_id'
    assert sends[0]['confirmation']
    for node in nodes(webui):
        if node.get('type') == 'ui.form':
            assert node['inputs'].get('resetOnSuccess', False) is False


def test_application_closure_and_permissions():
    project = read_yaml(PROJECT / 'project.yaml')
    skill = read_yaml(SKILL / 'skill.yaml')
    scenario = read_yaml(SCENARIO / 'scenario.yaml')
    assert 'gmail_cbs_cleanroom_skill' in scenario['runtime']['skills']['required']
    assert {member['ref'] for member in project['components']['owned']} == {
        'scenario:gmail_cbs_cleanroom', 'skill:gmail_cbs_cleanroom_skill'}
    profile = project['permission_profile']
    permissions = {entry['id'] for entry in profile['required'] + profile['optional']}
    assert permissions == set(skill['capabilities'])
    assert profile['secrets'] == []
    for tool in skill['tools']:
        assert set(tool['permissions']) <= permissions
        assert tool['application_access']['permission'] in permissions


def test_single_authored_provider_and_compiler_owned_outputs():
    sources = []
    for base in (SCENARIO, PROJECT, SKILL):
        for path in base.rglob('*'):
            if any(part.startswith('.builder') for part in path.relative_to(base).parts):
                continue
            if path.suffix not in {'.yaml', '.yml', '.json'}:
                continue
            document = read_yaml(path)
            if not isinstance(document, dict):
                continue
            schema = document.get('schema', '')
            if schema == 'adaos.cbs.provider_authoring.v1':
                sources.append(path)
            # The canonical JSON under tests is a conformance oracle, not a provider output.
            if 'tests' not in path.relative_to(base).parts:
                assert schema not in {'adaos.capability.contract.v1', 'adaos.binding.definition.v1'}
    assert sources == [SKILL / 'contracts/provider.cbs.yaml']
    provider = read_yaml(sources[0])
    assert provider['capability']['ref'] == 'capability:mail.messages.manage'
    assert provider['capability']['version'] == '1.0.0'
    assert provider['binding']['physical_member'] == 'handlers/main.py'
