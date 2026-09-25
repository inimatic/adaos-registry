"""Consumer test double for the admitted exported-tool seam, not a browser.

No provider implementation, external calls, or Core authentication is exercised.
"""
import copy
import json
from pathlib import Path
import pytest
import jsonschema

PROVIDER = 'gmail_cbs_cleanroom_skill.'
CONTRACTS = json.loads((Path(__file__).parent / 'public_tool_contracts.json').read_text(encoding='utf-8'))


def validate_call(target, params, reply):
    assert target.startswith(PROVIDER)
    contract = CONTRACTS[target[len(PROVIDER):]]
    jsonschema.validate(params, contract['input_schema'])
    if isinstance(reply, dict):
        jsonschema.validate(reply, contract['output_schema'])


def resolve(value, state, event):
    if isinstance(value, str) and value.startswith(('$state.', '$event.')):
        prefix, path = value.split('.', 1)
        result = state if prefix == '$state' else event
        for key in path.split('.'):
            result = result[key]
        return result
    if isinstance(value, dict):
        if value.get('kind') == 'expression':
            assert value['op'] == 'if'
            branch = 'then' if resolve(value['condition'], state, event) else 'else'
            return resolve(value[branch], state, event)
        return {key: resolve(item, state, event) for key, item in value.items()}
    return value


class Consumer:
    def __init__(self, app):
        self.app = app
        self.page = app['desktop']['pageSchema']
        self.state = copy.deepcopy(self.page['initialState'])
        self.calls = []

    def widget(self, id):
        widgets = self.page['widgets'] + [w for m in self.app['modals'].values() for w in m['schema']['widgets']]
        return next(w for w in widgets if w['id'] == id)

    def visible(self, id):
        condition = self.widget(id).get('visibleIf')
        if not condition:
            return True
        assert condition in ("$state.selected_message_id === ''", "$state.selected_message_id !== ''")
        return (self.state['selected_message_id'] == '') == ('===' in condition)

    def read(self, id, reply):
        assert self.visible(id)
        source = self.widget(id)['dataSource']
        self.calls.append((source['name'], resolve(source['params'], self.state, {})))
        validate_call(*self.calls[-1], reply)
        return copy.deepcopy(reply)

    def act(self, id, event=None, result=None, confirm=True):
        for action in self.widget(id).get('actions', []):
            if action.get('confirmation') and not confirm:
                return False
            params = resolve(action.get('params', {}), self.state, event or {})
            if action['type'] == 'updateState':
                self.state.update(params)
            elif action['type'] == 'callSkill':
                self.calls.append((action['target'], params))
                validate_call(action['target'], params, result)
                if isinstance(result, Exception) or result is False:
                    return False
                if isinstance(result, dict) and (result.get('ok') is False or result.get('status') == 'pending'):
                    return False
            elif action['type'] != 'openModal':
                raise AssertionError(action['type'])
        return True


@pytest.fixture
def consumer():
    path = Path(__file__).resolve().parents[1] / 'webui.json'
    return Consumer(json.loads(path.read_text(encoding='utf-8'))['ui']['application'])
