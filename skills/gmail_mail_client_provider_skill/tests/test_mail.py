import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email import policy
from email.parser import BytesParser
import sqlite3
import uuid

import jsonschema
import pytest


def draft():
    return {'to': 'recipient@example.test', 'subject': 'Synthetic subject', 'body': 'Synthetic transient content'}


def test_contract_examples_validate(contract):
    examples = contract['synthetic_examples']
    jsonschema.validate(examples['connection_status'], contract['response_schemas']['connection_status'])
    for name in ['list_messages', 'get_message', 'list_labels', 'modify_message', 'trash_message', 'send_confirmed']:
        jsonschema.validate(examples[name], contract['response_schemas']['operation'])


def test_search_and_pagination(app):
    result = app.list_messages(query='from:sender@example.test', tab='inbox', unread=True, starred=True, label='Label_1', page_token='page-1')
    app.gmail.list_messages.assert_called_once_with(query='from:sender@example.test', label_ids=['INBOX','UNREAD','STARRED','Label_1'], page_token='page-1', max_results=25, account_id='google.gmail')
    app.gmail.get_message.assert_called_once_with('msg-1', format='metadata', account_id='google.gmail')
    assert result['nextPageToken'] == 'page-2'
    assert result['pagination'][0]['nextPageToken'] == 'page-2'
    assert result['items'][0]['subject'] == 'Example'
    assert 'body' not in result['items'][0]


def test_empty_and_folder_reads(app):
    app.gmail.list_messages.side_effect = None
    app.gmail.list_messages.return_value = {'ok':True,'provider_id':'google.gmail','operation':'list_messages','result':{}}
    for folder, label in [('sent','SENT'),('trash','TRASH'),('starred','STARRED')]:
        result = app.list_messages(tab=folder)
        assert result['items'] == [] and result['nextPageToken'] == ''
        assert app.gmail.list_messages.call_args.kwargs['label_ids'] == [label]
    assert app.get_message('') == {'ok':True,'item':{}}
    app.gmail.get_message.assert_not_called()


def test_reader_and_labels(app):
    assert app.get_message('msg-1')['item']['body'] == 'Example body'
    app.gmail.get_message.assert_called_once_with('msg-1',format='full',account_id='google.gmail')
    assert app.list_labels()['items'] == [{'id':'INBOX','name':'INBOX'}]
    app.gmail.list_labels.assert_called_once_with(account_id='google.gmail')


def test_nested_mime_never_interprets_html(app):
    data = base64.urlsafe_b64encode('Synthetic Unicode \u043f\u0438\u0441\u044c\u043c\u043e'.encode()).decode().rstrip('=')
    part = {'mimeType':'multipart/mixed','parts':[{'mimeType':'text/html','body':{'data':'PHNjcmlwdD4='}}, {'mimeType':'multipart/alternative','parts':[{'mimeType':'text/plain','body':{'data':data}}]}, {'mimeType':'text/plain','filename':'private.txt','body':{'data':data}}]}
    assert app._body(part) == 'Synthetic Unicode \u043f\u0438\u0441\u044c\u043c\u043e'
    assert 'could not be decoded' in app._body({'mimeType':'text/plain','body':{'data':'!'}})


@pytest.mark.parametrize('action,add,remove', [('archive',[],['INBOX']),('mark_read',[],['UNREAD']),('mark_unread',['UNREAD'],[]),('star',['STARRED'],[]),('unstar',[],['STARRED']),('label',['Label_1'],[]),('remove_label',[],['Label_1'])])
def test_mutation_mapping(app, action, add, remove):
    assert app.mutate_message('msg-1',action,label='Label_1')['ok']
    app.gmail.modify_message.assert_called_once_with('msg-1',add_label_ids=add,remove_label_ids=remove,account_id='google.gmail')


def test_trash(app):
    assert app.mutate_message('msg-1','trash')['ok']
    app.gmail.trash_message.assert_called_once_with('msg-1',account_id='google.gmail')


@pytest.mark.parametrize('code,state', [('gmail_account_not_connected','disconnected'),('gmail_reconnect_required','expired'),('gmail_permission_denied','permission_denied'),('gmail_required_scope_missing','permission_denied')])
def test_expected_connection_states_are_successful_reads_but_failed_writes(app, code, state):
    exc = app.gmail.GmailProviderError(code,status_code=503,retryable=True)
    app.gmail.connection_status.side_effect = exc
    result = app.connection_status()
    assert result['ok'] is True and result['status'] == state and result['email_address'] == ''
    app.gmail.list_messages.side_effect = exc
    assert app.list_messages() == {
        'ok': True,
        'status': state,
        'message': 'Connect Gmail to load mail.',
        'items': [],
        'nextPageToken': '',
        'pagination': [],
    }
    app.gmail.modify_message.side_effect = exc
    assert app.mutate_message('msg-1','archive')['ok'] is False


def test_provider_unavailable_is_not_a_successful_read(app):
    exc = app.gmail.GmailProviderError('gmail_provider_unavailable',status_code=503,retryable=True)
    app.gmail.connection_status.side_effect = exc
    result = app.connection_status()
    assert result['ok'] is False and result['status'] == 'error'


def test_connect_and_status(app):
    assert app.begin_connection()['authorization_url'].startswith('https://')
    assert app.connection_status()['status'] == 'connected'


def test_send_survives_reopen_and_never_persists_content(app, tmp_path, caplog):
    command = app.prepare_send()['command_id']
    first = app.send_message(command,draft())
    assert first['ok'] and first['delivery_status'] == 'confirmed'
    assert app.send_message(command,draft())['duplicate']
    app.gmail.send_message.assert_called_once()
    raw = app.gmail.send_message.call_args.args[0]
    parsed = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
    assert parsed['To'] == draft()['to'] and parsed.get_content().strip() == draft()['body']
    assert app.ensure_database.call_count == 2
    disk = (tmp_path/app.DATABASE).read_bytes()
    for value in draft().values():
        assert value.encode() not in disk and value not in caplog.text
    with closing(sqlite3.connect(tmp_path/app.DATABASE)) as db:
        assert db.execute('SELECT status FROM send_commands').fetchone() == ('confirmed',)


def test_uncertain_send_is_never_retried(app):
    app.gmail.send_message.side_effect = app.gmail.GmailProviderError('gmail_provider_unavailable',status_code=503,retryable=True)
    command = str(uuid.uuid4())
    assert app.send_message(command,draft())['delivery_status'] == 'unknown'
    assert app.send_message(command,draft())['ok'] is False
    app.gmail.send_message.assert_called_once()


def test_malformed_success_stays_reserved(app):
    app.gmail.send_message.side_effect = None
    app.gmail.send_message.return_value = {'ok':True,'provider_id':'google.gmail','operation':'send_message','result':{}}
    command = str(uuid.uuid4())
    assert app.send_message(command,draft())['delivery_status'] == 'unknown'
    assert app.send_message(command,draft())['delivery_status'] == 'unknown'
    app.gmail.send_message.assert_called_once()


def test_html_reader_extracts_inert_text(app):
    html='<html><head><style>secret style</style></head><body><script>danger()</script><p>Hello &amp; welcome</p><img src="https://tracker.example.test/pixel"></body></html>'
    part={'mimeType':'text/html','body':{'data':base64.urlsafe_b64encode(html.encode()).decode()}}
    assert app._body(part)=='Hello & welcome'


def test_concurrent_duplicate_has_one_provider_call(app):
    command = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: app.send_message(command,draft()), range(2)))
    assert any(x['ok'] for x in results)
    app.gmail.send_message.assert_called_once()


def test_subject_scoped_reservations(app, monkeypatch):
    command = str(uuid.uuid4())
    app.send_message(command,draft())
    monkeypatch.setattr(app.access,'application',lambda: {'application_id':'synthetic-app','subject_ref':'user:other'})
    assert app.send_message(command,draft())['ok']
    assert app.gmail.send_message.call_count == 2


@pytest.mark.parametrize('field,value', [('to','a@example.test\r\nBcc: b@example.test'),('subject','Subject\nBcc: b@example.test'),('to','invalid')])
def test_send_validation_precedes_io(app,field,value):
    values=draft(); values[field]=value
    with pytest.raises(ValueError): app.send_message(str(uuid.uuid4()),values)
    app.gmail.send_message.assert_not_called()
    app.ensure_database.assert_not_called()


def test_declared_sql_preserves_legacy_synthetic_rows(app,tmp_path):
    app.ensure_database(app.DATABASE)
    with closing(sqlite3.connect(tmp_path/app.DATABASE)) as db, db:
        db.execute("INSERT INTO send_commands VALUES ('legacy','command','unknown',NULL)")
    app.ensure_database(app.DATABASE)
    with closing(sqlite3.connect(tmp_path/app.DATABASE)) as db:
        assert db.execute('SELECT scope,command_id,status FROM send_commands').fetchone()==('legacy','command','unknown')
