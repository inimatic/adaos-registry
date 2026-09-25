"""Canonical projection and hermetic portable adapter conformance (SDK doubles)."""
import hashlib
import json
from pathlib import Path
import pytest
import yaml
from jsonschema import validate

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = json.loads((ROOT / 'tests/canonical_mail_contract.json').read_text(encoding='utf-8'))
OPS = {x['operation_id']: x for x in CANONICAL['operations']}


def invoke(app, name, **args):
    validate(args, OPS[name]['input_schema'])
    result = getattr(app, 'portable_' + name)(**args)
    validate(result, OPS[name]['output_schema'])
    if not result['ok']:
        assert result['error'] in OPS[name]['errors']
    return result


def test_exact_compiled_contract_projection(app):
    cap = yaml.safe_load((ROOT / 'contracts/provider.cbs.yaml').read_text(encoding='utf-8'))['capability']
    tools = {t['name']: t for t in app.test_manifest['tools']}
    compiled = {'schema': 'adaos.capability.contract.v1', 'capability_ref': cap['ref'],
                'version': cap['version'], 'title': cap['title'], 'operations': [],
                'state_ports': [], 'dependencies': [], 'compatibility': {}}
    for key in ('invariants', 'effects', 'authority_requirements', 'conformance_refs'):
        compiled[key] = cap[key]
    for op in cap['operations']:
        tool = tools[op['tool']]
        assert op['tool'] == 'portable_' + op['operation_id']
        compiled['operations'].append({k: op[k] for k in ('operation_id', 'errors')} | {
            k: tool[k] for k in ('input_schema', 'output_schema')})
    expected = {k: v for k, v in CANONICAL.items() if k != 'contract_digest'}
    assert compiled == expected
    digest = 'sha256:' + hashlib.sha256(json.dumps(compiled, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    assert digest == CANONICAL['contract_digest'] == 'sha256:f694f5fc917ed4a76330740a7ff555c1c6e0514781f3b29d84c7ce55f5d61698'


def test_reads_and_pagination(app, envelope, message):
    app.gmail.connection_status.return_value = {'ok': True, 'status': 'connected', 'email_address': 'test@example.test'}
    app.gmail.connection_status.side_effect = None
    assert invoke(app, 'connection_status')['status'] == 'connected'
    for method, result in [('list_labels', {'labels': [{'id': 'INBOX', 'name': 'Inbox'}]}),
                           ('list_message_summaries', {'messages': [message], 'nextPageToken': 'next'}),
                           ('get_message', message)]:
        getattr(app.gmail, method).side_effect = None
        getattr(app.gmail, method).return_value = envelope(method, result)
    assert invoke(app, 'list_labels')['items'] == [{'id': 'INBOX', 'name': 'Inbox'}]
    result = invoke(app, 'list_messages', label='INBOX', query='hello', unread='true', starred=False, tab='primary')
    assert result['items'][0]['threadId'] == 't1'
    assert result['pagination'][0]['nextPageToken'] == 'next'
    assert app.gmail.list_message_summaries.call_args.kwargs['query'] == '(hello) is:unread -is:starred category:primary'
    app.gmail.list_messages.assert_not_called()
    app.gmail.get_message.assert_not_called()
    assert invoke(app, 'get_message', id='m1')['item']['body'] == 'Synthetic body'
    app.gmail.get_message.assert_called_once()


def test_list_messages_uses_one_summary_batch_without_n_plus_one(app, envelope, message):
    summaries = [dict(message, id=f'm{i}', threadId=f't{i}') for i in range(25)]
    app.gmail.list_message_summaries.side_effect = None
    app.gmail.list_message_summaries.return_value = envelope(
        'list_message_summaries',
        {'messages': summaries, 'nextPageToken': 'next-page'},
    )

    result = invoke(app, 'list_messages', label='INBOX', page_token='current')

    assert result['ok'] is True
    assert len(result['items']) == 25
    assert result['nextPageToken'] == 'next-page'
    app.gmail.list_message_summaries.assert_called_once_with(
        account_id='google.gmail', query='', label_ids=['INBOX'],
        page_token='current', max_results=25,
    )
    app.gmail.list_messages.assert_not_called()
    app.gmail.get_message.assert_not_called()


@pytest.mark.parametrize('action,target,add', [('archive','INBOX',False), ('mark_read','UNREAD',False),
    ('mark_unread','UNREAD',True), ('star','STARRED',True), ('unstar','STARRED',False),
    ('label','custom',True), ('remove_label','custom',False), ('trash',None,False)])
def test_mutations(app, envelope, message, action, target, add):
    method = 'trash_message' if action == 'trash' else 'modify_message'
    getattr(app.gmail, method).side_effect = None
    getattr(app.gmail, method).return_value = envelope(method, message)
    assert invoke(app, 'mutate_message', id='m1', action=action, label='custom')['item']['id'] == 'm1'
    if target:
        args = app.gmail.modify_message.call_args.kwargs
        assert args['add_label_ids'] == ([target] if add else [])
        assert args['remove_label_ids'] == ([] if add else [target])


CASES = [('connection_status', {}), ('begin_connection', {}), ('list_labels', {}),
         ('list_messages', {}), ('get_message', {'id': 'm1'}),
         ('mutate_message', {'id': 'm1', 'action': 'archive'}), ('prepare_send', {}),
         ('send_message', {'command_id': '00000000-0000-4000-8000-000000000001',
                           'values': {'to': 'x@example.test', 'subject': 'x', 'body': 'x'}})]


@pytest.mark.parametrize('name,args', CASES)
def test_authority_denial_precedes_io(app, name, args):
    app.access.require.side_effect = PermissionError('PRIVATE')
    assert invoke(app, name, **args)['error'] == 'permission_denied'
    app.ensure_database.assert_not_called()
    for method in ('connection_status', 'begin_connection', 'list_labels', 'list_messages', 'get_message', 'modify_message', 'send_message'):
        getattr(app.gmail, method).assert_not_called()


@pytest.mark.parametrize('code,expected', [('gmail_reconnect_required','account_not_connected'),
    ('gmail_account_not_connected','account_not_connected'), ('gmail_required_scope_missing','permission_denied'),
    ('gmail_permission_denied','permission_denied'), ('gmail_provider_unavailable','provider_unavailable')])
@pytest.mark.parametrize('name,args', CASES[:6])
def test_provider_errors_are_canonical_and_redacted(app, name, args, code, expected):
    for method in ('connection_status', 'begin_connection', 'list_labels', 'list_messages', 'list_message_summaries', 'get_message', 'modify_message'):
        getattr(app.gmail, method).side_effect = app.gmail.GmailProviderError(code)
    result = invoke(app, name, **args)
    assert result['error'] == ('oauth_not_configured' if name == 'begin_connection' and expected == 'account_not_connected' else expected)
    assert 'NEVER_EXPOSE' not in str(result)


def test_send_replay_and_uncertainty(app, envelope):
    values = {'to': 'x@example.test', 'subject': 'Synthetic', 'body': 'Transient content'}
    command = invoke(app, 'prepare_send')['command_id']
    app.gmail.send_message.side_effect = None
    app.gmail.send_message.return_value = envelope('send_message', {'id': 'sent'})
    assert invoke(app, 'send_message', command_id=command, values=values)['delivery_status'] == 'confirmed'
    assert invoke(app, 'send_message', command_id=command, values=values)['duplicate']
    app.gmail.send_message.assert_called_once()
    command = invoke(app, 'prepare_send')['command_id']
    app.gmail.send_message.side_effect = RuntimeError('PRIVATE')
    assert invoke(app, 'send_message', command_id=command, values=values)['error'] == 'send_unconfirmed'
    assert invoke(app, 'send_message', command_id=command, values=values)['delivery_status'] == 'unknown'
    assert app.gmail.send_message.call_count == 2


def test_connection_handoff_bounds(app):
    app.gmail.begin_connection.side_effect = None
    app.gmail.begin_connection.return_value = {'ok': True, 'authorization_url': 'https://example.test/authorize', 'expires_at': 'later'}
    result = invoke(app, 'begin_connection')
    assert result['authorization_url'] == 'https://example.test/authorize'


@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('missing', ['caller', 'application'])
def test_missing_context_fails_closed(app, name, args, missing):
    getattr(app.access, missing).return_value = None
    assert invoke(app, name, **args)['error'] == 'permission_denied'
    app.ensure_database.assert_not_called()
    app.gmail.send_message.assert_not_called()
