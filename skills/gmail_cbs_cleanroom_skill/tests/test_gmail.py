import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
import pytest
from jsonschema import validate


def respond(app, method, value):
    mock = getattr(app.gmail, method)
    mock.side_effect = None
    mock.return_value = value


def check_schema(app, name, response):
    entry = next(t for t in app.test_manifest['tools'] if t['name'] == name)
    validate(response, entry['output_schema'])


def test_read_search_label_and_reader_mapping(app, envelope, message):
    respond(app, 'list_message_summaries', envelope('list_message_summaries', {'messages': [message], 'nextPageToken': 'p2'}))
    respond(app, 'get_message', envelope('get_message', message))
    result = app.list_messages(query='from:sender@example.test', label_id='INBOX', unread=True, starred=False)
    check_schema(app, 'list_messages', result)
    app.gmail.list_message_summaries.assert_called_once_with(account_id='google.gmail', query='(from:sender@example.test) is:unread -is:starred', label_ids=['INBOX'], page_token='', max_results=20)
    app.gmail.list_messages.assert_not_called()
    app.gmail.get_message.assert_not_called()
    assert result['items'][0]['Message']['body'] == ''
    assert result['next_page_token'] == 'p2'
    result = app.get_message('m1')
    assert result['items'][0]['Message']['body'] == 'Synthetic body'
    check_schema(app, 'get_message', result)
    assert app.get_message('')['items'] == []
    respond(app, 'list_labels', envelope('list_labels', {'labels': [{'id': 'INBOX', 'name': 'Inbox', 'unneeded': 'private'}]}))
    result = app.list_labels()
    assert result['items'] == [{'id': 'INBOX', 'title': 'Inbox'}]
    check_schema(app, 'list_labels', result)


@pytest.mark.parametrize('action,value,add,remove', [('archive', None, [], ['INBOX']), ('unread', True, ['UNREAD'], []), ('unread', False, [], ['UNREAD']), ('starred', True, ['STARRED'], []), ('starred', False, [], ['STARRED']), ('label', 'Label_1', ['Label_1'], [])])
def test_exact_mutations(app, envelope, action, value, add, remove):
    respond(app, 'modify_message', envelope('modify_message', {'id': 'm1', 'threadId': 't1', 'labelIds': add}))
    response = app.mutate_message('m1', action, value)
    assert response['ok']
    app.gmail.modify_message.assert_called_once_with(account_id='google.gmail', message_id='m1', add_label_ids=add, remove_label_ids=remove)
    check_schema(app, 'mutate_message', response)


def test_trash_and_failure_are_authoritative(app, envelope):
    respond(app, 'trash_message', envelope('trash_message', {'id': 'm1', 'threadId': 't1', 'labelIds': ['TRASH']}))
    assert app.mutate_message('m1', 'trash')['labels'] == ['TRASH']
    app.gmail.trash_message.side_effect = app.gmail.GmailProviderError('gmail_permission_denied')
    assert app.mutate_message('m1', 'trash')['ok'] is False
    assert not app.mutate_message('m1', 'unread', 'true')['ok']


@pytest.mark.parametrize('code,status', [('gmail_account_not_connected', 'disconnected'), ('gmail_reconnect_required', 'auth_required'), ('gmail_permission_denied', 'permission_denied'), ('gmail_required_scope_missing', 'permission_denied'), ('gmail_provider_unavailable', 'offline'), ('google_oauth_not_configured', 'provider_error')])
@pytest.mark.parametrize('name,args', [('connection_status', {}), ('list_labels', {}), ('list_messages', {}), ('get_message', {'id': 'm1'})])
def test_read_lifecycle_projection_settles_and_is_redacted(app, code, status, name, args, caplog, capsys):
    getattr(app.gmail, 'list_message_summaries' if name == 'list_messages' else name).side_effect = app.gmail.GmailProviderError(code)
    result = getattr(app, name)(**args)
    assert result['ok'] is True
    assert 'error' not in result
    if name == 'connection_status':
        assert result['items'][0]['status'] == status
        assert result['items'][0]['least_privilege_note'] == app.ERRORS[code]
    else:
        assert result['items'] == []
        assert result['status'] == status
        assert result['notice'] == app.ERRORS[code]
    check_schema(app, name, result)
    assert 'NEVER_EXPOSE' not in json.dumps(result) + caplog.text + str(capsys.readouterr())


@pytest.mark.parametrize('name,args', [('connection_status', {}), ('list_labels', {}), ('list_messages', {}), ('get_message', {'id': 'm1'})])
@pytest.mark.parametrize('provider_error', [False, True])
def test_unexpected_read_failure_does_not_settle(app, name, args, provider_error):
    error = app.gmail.GmailProviderError('unexpected_provider_failure') if provider_error else RuntimeError('PRIVATE_REMOTE_RESPONSE')
    getattr(app.gmail, 'list_message_summaries' if name == 'list_messages' else name).side_effect = error
    result = getattr(app, name)(**args)
    assert result['ok'] is False
    assert 'items' not in result
    assert 'PRIVATE_REMOTE_RESPONSE' not in str(result)
    check_schema(app, name, result)


@pytest.mark.parametrize('code', ['gmail_account_not_connected', 'gmail_reconnect_required', 'gmail_permission_denied', 'gmail_required_scope_missing', 'gmail_provider_unavailable', 'google_oauth_not_configured'])
def test_lifecycle_failure_remains_failure_for_mutations(app, code):
    app.gmail.trash_message.side_effect = app.gmail.GmailProviderError(code)
    result = app.mutate_message('m1', 'trash')
    assert result['ok'] is False
    assert result['error'] == code
    check_schema(app, 'mutate_message', result)


def test_connected_and_oauth_projection(app):
    respond(app, 'connection_status', {'ok': True, 'status': 'connected', 'email_address': 'owner@example.test', 'extra': 'DROP'})
    result = app.connection_status()
    assert result['items'][0]['status'] == 'connected'
    respond(app, 'begin_connection', {'ok': True, 'authorization_url': 'https://accounts.google.com/synthetic', 'expires_at': '2026-01-01T00:00:00Z', 'extra': 'DROP'})
    result = app.begin_connection()
    assert 'extra' not in result
    check_schema(app, 'begin_connection', result)


def test_reusable_connection_requires_explicit_attach_and_stays_redacted(app):
    respond(app, 'reusable_connections', {
        'ok': True,
        'provider_id': 'google.gmail',
        'accounts': [{
            'provider_id': 'google.gmail',
            'account_id': 'google.gmail',
            'email_address': 'owner@example.test',
            'status': 'connected',
            'scopes': ['https://www.googleapis.com/auth/gmail.modify'],
            'attached': False,
        }],
    })
    discovered = app.reusable_connections()
    assert discovered == {
        'ok': True,
        'items': [{
            'id': 'google.gmail',
            'email_address': 'owner@example.test',
            'status': 'connected',
            'attached': False,
        }],
    }
    check_schema(app, 'reusable_connections', discovered)
    app.gmail.attach_reusable_connection.assert_not_called()

    respond(app, 'attach_reusable_connection', {
        'ok': True,
        'provider_id': 'google.gmail',
        'account_id': 'google.gmail',
        'email_address': 'owner@example.test',
        'status': 'connected',
        'scopes': ['https://www.googleapis.com/auth/gmail.modify'],
        'attached': True,
        'reused_credential': True,
    })
    attached = app.attach_reusable_connection()
    assert attached['attached'] is True
    assert attached['reused_credential'] is True
    assert 'token' not in json.dumps(attached).lower()
    check_schema(app, 'attach_reusable_connection', attached)
    app.gmail.attach_reusable_connection.assert_called_once_with(
        account_id='google.gmail'
    )


def test_send_duplicate_content_free_storage(app, envelope, caplog, capsys):
    respond(app, 'send_message', envelope('send_message', {'id': 'sent1', 'threadId': 't1'}))
    command = app.prepare_send()['command_id']
    arguments = dict(command_id=command, to='recipient@example.test', subject='Synthetic subject', body='Synthetic body')
    first = app.send_message(**arguments)
    assert first['ok']
    assert app.send_message(**arguments)['duplicate']
    app.gmail.send_message.assert_called_once()
    raw = app.gmail.send_message.call_args.kwargs['raw']
    assert b'Synthetic body' in base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    with closing(sqlite3.connect(app.test_root / app.DATABASE)) as db:
        stored = str(db.execute('SELECT * FROM send_commands').fetchall())
    assert 'Synthetic body' not in stored + caplog.text + str(capsys.readouterr())
    assert 'recipient@example.test' not in stored
    check_schema(app, 'send_message', first)


def test_unknown_send_never_retries(app):
    command = app.prepare_send()['command_id']
    app.gmail.send_message.side_effect = app.gmail.GmailProviderError('gmail_provider_unavailable')
    args = (command, 'recipient@example.test', 'Synthetic subject', 'Synthetic body')
    assert app.send_message(*args)['delivery_status'] == 'unknown'
    assert app.send_message(*args)['error'] == 'gmail_send_unconfirmed'
    app.gmail.send_message.assert_called_once()


def test_concurrent_claim_and_subject_isolation(app, envelope):
    respond(app, 'send_message', envelope('send_message', {'id': 'sent1', 'threadId': 't1'}))
    command = app.prepare_send()['command_id']
    args = (command, 'recipient@example.test', 'Synthetic subject', 'Synthetic body')
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: app.send_message(*args), range(2)))
    assert any(r['ok'] for r in responses)
    app.gmail.send_message.assert_called_once()
    app.access.application.return_value = {'application_id': 'gmail_cbs_cleanroom', 'subject_ref': 'user:other'}
    assert app.send_message(*args)['error'] == 'invalid_input'


def test_validation_does_not_consume_command(app, envelope):
    respond(app, 'send_message', envelope('send_message', {'id': 'sent1', 'threadId': 't1'}))
    command = app.prepare_send()['command_id']
    assert not app.send_message(command, 'bad\r\nBcc: x@example.test', 'Subject', 'Synthetic body')['ok']
    assert app.send_message(command, 'x@example.test', 'Subject', 'Synthetic body')['ok']
    assert app.ensure_database.call_count == 2


def test_html_is_inert_and_plain_alternative_preferred(app):
    html = '<p>Synthetic visible text</p><script>SECRET_SCRIPT</script><style>HIDDEN_STYLE</style>'
    part = {'mimeType': 'text/html', 'body': {'data': base64.urlsafe_b64encode(html.encode()).decode()}}
    result = app._body(part)
    assert 'Synthetic visible text' in result
    assert 'SECRET_SCRIPT' not in result and '<p>' not in result and 'HIDDEN_STYLE' not in result
    plain = {'mimeType': 'text/plain', 'body': {'data': 'U3ludGhldGljIGJvZHk'}}
    assert app._body({'mimeType': 'multipart/alternative', 'parts': [plain, part]}) == 'Synthetic body'


def test_malformed_provider_response_is_never_success(app):
    respond(app, 'connection_status', {'ok': True, 'status': 'expired'})
    assert app.connection_status()['ok'] is False
    respond(app, 'list_labels', {'ok': True, 'operation': 'get_message', 'result': {'labels': []}})
    assert app.list_labels()['ok'] is False


def test_unexpected_send_error_is_redacted_and_not_retried(app):
    command = app.prepare_send()['command_id']
    app.gmail.send_message.side_effect = OSError('PRIVATE_REMOTE_RESPONSE')
    args = (command, 'x@example.test', 'Synthetic subject', 'Synthetic body')
    result = app.send_message(*args)
    assert not result['ok'] and 'PRIVATE_REMOTE_RESPONSE' not in str(result)
    assert app.send_message(*args)['error'] == 'gmail_send_unconfirmed'
    app.gmail.send_message.assert_called_once()
