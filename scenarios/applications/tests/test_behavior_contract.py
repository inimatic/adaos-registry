"""Hermetic consumer examples at the admitted Root MCP binding boundary.

Synthetic API records exercise the shipped WebUI declarations. This deliberately
does not emulate the Application service, persist domain state, or qualify browser
command execution / Core authorization. Those checks belong to the worker.
"""
import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest


PACKAGE = Path(__file__).resolve().parents[1]
APP = json.loads((PACKAGE / 'webui.json').read_text(encoding='utf-8'))['ui']['application']
PAGE = APP['desktop']['pageSchema']
WIDGETS = {w['id']: w for w in PAGE['widgets']}
WIDGETS.update({w['id']: w for m in APP['modals'].values() for w in m['schema']['widgets']})
CONTRACTS = {c['id']: c for c in json.loads(
    (PACKAGE / 'tests/fixtures/behavior_root_contracts.json').read_text(encoding='utf-8'))}
DIGEST = 'sha256:' + 'b' * 64


def path(record, dotted):
    for key in dotted.split('.'):
        if not isinstance(record, dict) or key not in record:
            return None
        record = record[key]
    return copy.deepcopy(record)


def resolve(value, state, event=None):
    if isinstance(value, dict):
        return {k: resolve(v, state, event) for k, v in value.items()}
    if isinstance(value, str) and value.startswith(('$state.', '$event.')):
        root, dotted = value.split('.', 1)
        return path(state if root == '$state' else event, dotted)
    return copy.deepcopy(value)


def enabled(action, state):
    # Only the boolean guard subset used by these declared commands; no service
    # outcomes or command success behavior are implemented by this test helper.
    expression = re.sub(r'\$state\.([\w.]+)',
                        lambda m: repr(path(state, m[1])), action['enabledIf'])
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', ' not ', expression)
    expression = re.sub(r'\btrue\b', 'True', expression)
    expression = re.sub(r'\bfalse\b', 'False', expression)
    return bool(eval(expression.strip(), {'__builtins__': {}}, {}))


def command(widget, on, kind='callMcp'):
    return next(a for a in WIDGETS[widget]['actions']
                if a['on'] == on and a['type'] == kind)


def request(action, state):
    params = resolve(action['params'], state)
    if action.get('idempotencyKey') == 'auto':
        params['idempotency_key'] = 'synthetic-consumer-command'
    jsonschema.validate(params, CONTRACTS[action['target']]['input_schema'])
    return params


@pytest.mark.parametrize('widget,flag', [
    ('applications-list', 'installed_only'),
    ('marketplace-list', 'catalog_only'),
    ('developments-list', 'developed_only'),
])
def test_catalog_response_selects_exact_application_and_discards_previous_context(widget, flag):
    source = WIDGETS[widget]['dataSource']
    jsonschema.validate(source['arguments'], CONTRACTS[source['toolId']]['input_schema'])
    assert source['arguments'][flag] is True
    records = [{'application': {'application_id': name, 'display': {'title': name}}}
               for name in ('consumer-alpha', 'consumer-beta')]
    response = {'response': {'result': {'applications': records}}}
    rendered = path(response, source['resultPath'])
    assert [path(row, WIDGETS[widget]['inputs']['titleKey']) for row in rendered] == [
        'consumer-alpha', 'consumer-beta']
    state = copy.deepcopy(PAGE['initialState'])
    state.update(selectedApplicationId='consumer-alpha', detailApplicationId='consumer-alpha',
                 reviewedPlan={'operation': {'operation_id': 'old'}},
                 accessContext={'grant_id': 'old'}, publicationEvidence=True)
    for action in WIDGETS[widget]['actions']:
        if action['on'] == 'select':
            assert action['type'] == 'updateState'
            state.update(resolve(action['params'], state, rendered[1]))
    assert state['selectedApplicationId'] == 'consumer-beta'
    assert state['detailApplicationId'] == ''
    assert state['reviewedPlan'] == state['accessContext'] == {}
    assert state['publicationEvidence'] is False
    detail = WIDGETS['application-details']['dataSource']
    arguments = resolve(detail['arguments'], state)
    jsonschema.validate(arguments, CONTRACTS[detail['toolId']]['input_schema'])
    assert arguments == {'application_id': 'consumer-beta'}
    assert not enabled(command('lifecycle-actions', 'click:install'), state)


@pytest.mark.parametrize('status,reason,message', [
    ('blocked', 'setup', 'Configure the required provider'),
    ('unavailable', 'node_unavailable', 'Reconnect the selected node'),
    ('degraded', 'revision_conflict', 'Refresh and review the current revision'),
])
def test_application_failure_reason_is_visible_from_public_detail(status, reason, message):
    widget = WIDGETS['application-health-summary']
    response = {'response': {'result': {'application': {
        'attention': {'status': status, 'reason': reason, 'message': message}}}}}
    record = path(response, widget['dataSource']['resultPath'])
    assert [path(record, field['path']) for field in widget['inputs']['fields']] == [
        status, reason, message]


@pytest.mark.parametrize('kind,on', [('install', 'confirm-install'), ('update', 'confirm-update'),
                                   ('remove', 'confirm-remove'), ('select_track', 'confirm-select-track')])
@pytest.mark.parametrize('status', ['planned', 'pending', 'failed', 'conflict', 'succeeded'])
def test_only_reviewable_plan_can_submit_exact_digest(kind, on, status):
    operation = {'kind': kind, 'operation_id': 'appop.consumer', 'plan_digest': DIGEST,
                 'status': status, 'plan': {'permission_review': {'approval_required': True}}}
    state = {'reviewedPlan': {'operation': operation}, 'permissionApproved': True}
    action = command('modal-review-actions', 'click:' + on)
    assert enabled(action, state) is (status == 'planned')
    if status == 'planned':
        first = request(action, state)
        assert first == request(action, state) == {
            'operation_id': 'appop.consumer', 'plan_digest': DIGEST,
            'idempotency_key': 'appop.consumer'}
        if kind in ('install', 'update'):
            state['permissionApproved'] = False
            assert not enabled(action, state)
        state['permissionApproved'] = True
        operation['plan_digest'] = ''
        assert not enabled(action, state)


def test_pending_remove_and_cancel_do_not_apply_or_keep_a_review():
    state = copy.deepcopy(PAGE['initialState'])
    state.update(selectedApplicationId='consumer-alpha', detailApplicationId='consumer-alpha',
                 installationRevision=9, reviewedPlan={'operation': {'operation_id': 'old'}})
    steps = [a for a in WIDGETS['lifecycle-actions']['actions'] if a['on'] == 'click:remove']
    assert [a['type'] for a in steps] == ['updateState', 'openModal']
    state.update(resolve(steps[0]['params'], state))
    assert state['reviewedPlan'] == {}
    assert not enabled(command('modal-review-actions', 'click:confirm-remove'), state)
    review = command('modal-review-actions', 'click:review-remove')
    assert enabled(review, state)
    payload = request(review, state)
    assert payload['application_id'] == 'consumer-alpha'
    assert payload['expected_revision'] == 9
    assert payload['kind'] == 'remove'
    assert review['target'] == 'applications.plan'
    cancel = command('modal-review-actions', 'click:cancel-review', 'updateState')
    state.update(resolve(cancel['params'], state))
    assert state['pendingLifecycleKind'] == ''
    assert state['reviewedPlan'] == {}
    assert state['permissionApproved'] is False


def test_changing_removal_policy_invalidates_previously_reviewed_digest():
    state = {'reviewedPlan': {'operation': {'operation_id': 'appop.consumer', 'plan_digest': DIGEST}},
             'removeDataPolicy': 'retain'}
    change = command('review-remove-data-policy', 'change', 'updateState')
    state.update(resolve(change['params'], state, {'value': 'snapshot_then_delete'}))
    assert state['removeDataPolicy'] == 'snapshot_then_delete'
    assert state['reviewedPlan'] == {}
    assert not enabled(command('modal-review-actions', 'click:confirm-remove'), state)
