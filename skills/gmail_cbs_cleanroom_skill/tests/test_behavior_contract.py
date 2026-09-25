"""Trial behavior checkpoint using the existing provider-owned SDK doubles.

No real account, authorization ingress or Core credential store is exercised.
"""
import pytest
from jsonschema import validate
from unittest.mock import Mock


def configure(app, operation, response):
    method = getattr(app.gmail, operation)
    method.side_effect = None
    method.return_value = response


@pytest.mark.parametrize('portable', [False, True])
def test_summary_body_is_loaded_only_after_explicit_selection(app, envelope, message, monkeypatch, portable):
    prefix = 'portable_' if portable else ''
    configure(app, 'list_message_summaries', envelope(
        'list_message_summaries', {'messages': [message]}))
    decode_body = Mock(wraps=app._body)
    monkeypatch.setattr(app, '_body', decode_body)

    listed = getattr(app, prefix + 'list_messages')()

    row = listed['items'][0]
    assert (row['body'] if portable else row['Message']['body']) == ''
    decode_body.assert_not_called()
    app.gmail.get_message.assert_not_called()
    app.gmail.list_message_summaries.assert_called_once()

    configure(app, 'get_message', envelope('get_message', message))
    selected = getattr(app, prefix + 'get_message')(id=row['id'])
    detail = selected['item'] if portable else selected['items'][0]
    assert (detail['body'] if portable else detail['Message']['body']) == 'Synthetic body'
    app.gmail.get_message.assert_called_once_with(
        account_id='google.gmail', message_id='m1', format='full')
    assert app.gmail.list_message_summaries.call_count == 1
    app.ensure_database.assert_not_called()


@pytest.mark.parametrize('count', [0, 1, 20, 21])
def test_message_collection_uses_one_core_aggregate(app, envelope, message, count):
    messages = [dict(message, id='m' + str(i)) for i in range(count)]
    configure(app, 'list_message_summaries', envelope('list_message_summaries', {
        'messages': messages, 'nextPageToken': 'next-page'}))

    result = app.list_messages(label_id='', page_token='current-page')

    assert result == {'ok': True,
                      'items': [app._message(m) for m in messages[:20]],
                      'next_page_token': 'next-page'}
    entry = next(t for t in app.test_manifest['tools'] if t['name'] == 'list_messages')
    validate(result, entry['output_schema'])
    app.gmail.list_message_summaries.assert_called_once_with(
        account_id='google.gmail', query='', label_ids=[],
        page_token='current-page', max_results=20)
    for operation in ('list_messages', 'get_message', 'send_message',
                      'modify_message', 'trash_message'):
        getattr(app.gmail, operation).assert_not_called()
    app.ensure_database.assert_not_called()


def test_discovery_then_explicit_attachment(app):
    account = {'account_id': 'google.gmail', 'email_address': 'user@example.test',
               'status': 'connected', 'attached': False}
    configure(app, 'reusable_connections', {'ok': True, 'accounts': [account]})
    discovered = app.reusable_connections()
    assert discovered == {'ok': True, 'items': [{
        'id': 'google.gmail', 'email_address': 'user@example.test',
        'status': 'connected', 'attached': False}]}
    app.gmail.attach_reusable_connection.assert_not_called()
    configure(app, 'attach_reusable_connection', {
        'ok': True, **account, 'attached': True, 'reused_credential': True})
    attached = app.attach_reusable_connection(discovered['items'][0]['id'])
    assert attached == {'ok': True, 'id': 'google.gmail',
                        'email_address': 'user@example.test', 'status': 'connected',
                        'attached': True, 'reused_credential': True}
    for name, result in [('reusable_connections', discovered),
                         ('attach_reusable_connection', attached)]:
        entry = next(t for t in app.test_manifest['tools'] if t['name'] == name)
        validate(result, entry['output_schema'])
        getattr(app.gmail, name).assert_called_once_with(account_id='google.gmail')
    for name in ('begin_connection', 'send_message', 'modify_message', 'trash_message'):
        getattr(app.gmail, name).assert_not_called()
    app.ensure_database.assert_not_called()


def test_no_connection_does_not_auto_attach(app):
    configure(app, 'reusable_connections', {'ok': True, 'accounts': []})
    assert app.reusable_connections() == {'ok': True, 'items': []}
    app.gmail.attach_reusable_connection.assert_not_called()


def test_unknown_attachment_is_rejected_before_provider_io(app):
    assert app.attach_reusable_connection('another-account')['error'] == 'invalid_input'
    app.gmail.attach_reusable_connection.assert_not_called()


@pytest.mark.parametrize('operation', ['reusable_connections', 'attach_reusable_connection'])
@pytest.mark.parametrize('code', ['gmail_reusable_account_not_found',
                                 'gmail_permission_denied', 'gmail_reconnect_required',
                                 'gmail_provider_unavailable'])
def test_reuse_failures_are_bounded_and_not_retried(app, operation, code):
    getattr(app.gmail, operation).side_effect = app.gmail.GmailProviderError(code)
    result = getattr(app, operation)()
    assert result == {'ok': False, 'error': code, 'message': app.ERRORS[code]}
    assert 'NEVER_EXPOSE_REMOTE_DETAIL' not in str(result)
    assert getattr(app.gmail, operation).call_count == 1
    app.gmail.begin_connection.assert_not_called()
    app.gmail.send_message.assert_not_called()
