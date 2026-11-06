from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('context_chat_handlers', ROOT/'handlers/__init__.py', submodule_search_locations=[str(ROOT/'handlers')])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from context_chat_handlers.store import Store
from context_chat_handlers.service import PREFIX, Runtime, create_app, text_file
from adaos.services.ai.chatgpt import API, AUTH, PLAN_SCOPE, AccountError, ChatGPTAccount, KeyringVault, validate_id_token


class Vault:
    def __init__(self, data=None): self.data = data
    def read(self): return dict(self.data) if self.data else None
    def write(self, data): self.data = dict(data)
    def clear(self): self.data = None


class Disconnected:
    def __init__(self, store): self.store=store
    def status(self): return {'connected': False, 'plan_enabled': False, 'pending': False, 'email': ''}
    def cancel_sign_in(self): return {'ok': True}


class Connected(Disconnected):
    def status(self): return {'connected': True, 'plan_enabled': True, 'pending': False, 'email': 'test@example.invalid'}
    def models(self): return [{'id': 'test-model', 'label': 'Test model'}]
    def stream(self, model, inputs, instructions, cancelled):
        if 'Обнови память' in inputs[-1]['content']:
            yield json.dumps({'summary':'Test summary','preferences':'Short answers','agreements':'','source_ids':[]})
        else:
            yield 'Hello '
            yield 'world'


def test_default_chat_uses_core_sdk_and_has_no_connection_management(tmp_path, monkeypatch):
    from adaos.sdk import ai
    from adaos.services.ai.service import AIService
    from adaos.services.ai.bridge import dispatch
    from adaos.services.ai.models import descriptor
    class Provider:
        def status(self): return {'connected': True, 'plan_enabled': True, 'binding_ref': 'core-binding', 'generation': 1}
        def binding_ref(self): return 'core-binding'
        def models(self, refresh=False): return [descriptor({'id': 'test-model'})]
        def stream(self, model, inputs, instructions, cancelled, **options):
            yield 'Shared Core answer'
            options['on_usage']({'input_tokens': 5, 'output_tokens': 3})
    core = AIService(tmp_path / 'core', 'user:owner', provider=Provider())
    monkeypatch.setattr(ai, '_invoke', lambda op, **args: dispatch(core, 'user:owner', 'skill:context_chat_service', op, args))
    app = create_app(tmp_path / 'chat')
    app.state.store.set_setting('auto_memory', False)
    client = TestClient(app)
    project = client.post(PREFIX+'/api/projects', json={'title': 'SDK project'}).json()['id']
    chat = app.state.store.default_chat(project)
    path = PREFIX + '/api/projects/' + project + '/chats/' + chat['id']
    result = client.put(path+'/profile', json={'expected_revision': chat['profile_revision'], 'profile': {'model': 'test-model'}})
    assert result.status_code == 200, result.text
    first = client.post(path+'/messages', json={'text': 'Hello', 'request_key': 'same-intent'}).json()
    run = wait_run(app.state.store, project, first['run_id'])
    assert run['status'] == 'completed', run
    assert run['snapshot']['principal_ref'] == 'user:owner'
    assert app.state.store.messages(project)[-1]['content'] == 'Shared Core answer'
    assert client.post(path+'/messages', json={'text': 'Hello', 'request_key': 'same-intent'}).json() == first
    for operation in ('start', 'disconnect', 'verify', 'cancel'):
        assert client.post(PREFIX+'/api/auth/'+operation).status_code == 410
    assert not (tmp_path / 'chat/files/chatgpt-session.enc').exists()


def test_large_history_is_paged_without_losing_export_or_leaking_projects(tmp_path):
    app = create_app(tmp_path, Disconnected)
    store = app.state.store
    project = store.create_project('Long')['id']
    other = store.create_project('Other')['id']
    for i in range(123):
        store.add_message(project, 'user' if i % 2 == 0 else 'assistant', str(i))
    secret = store.add_message(other, 'user', 'private other project')
    client = TestClient(app)
    latest = client.get(PREFIX+'/api/projects/'+project).json()
    assert len(latest['messages']) == 50 and latest['history']['total'] == 123
    assert latest['messages'][0]['content'] == '73' and latest['history']['has_older']
    older = client.get(PREFIX+'/api/projects/'+project, params={'before': latest['history']['first']}).json()
    assert older['messages'][0]['content'] == '23' and older['messages'][-1]['content'] == '72'
    newer = client.get(PREFIX+'/api/projects/'+project, params={'after': older['history']['last']}).json()
    assert [m['id'] for m in newer['messages']] == [m['id'] for m in latest['messages']]
    export = client.get(PREFIX+'/api/projects/'+project+'/export').json()
    assert len(export['messages']) == 123 and secret not in {m['id'] for m in export['messages']}
    assert client.get(PREFIX+'/api/projects/'+project, params={'limit': 100000}).status_code == 422


def test_context_uses_bounded_recent_suffix_and_keeps_user_memory(tmp_path):
    store = Store(tmp_path)
    project = store.create_project('Long')['id']
    store.save_memory(project, {'summary': 'Earlier important fact'}, 0)
    for i in range(260):
        store.add_message(project, 'user' if i % 2 == 0 else 'assistant', str(i)+':'+'x'*2000)
    _, inputs = Runtime(store, Disconnected(store)).context(project)
    assert 'Earlier important fact' in inputs[0]['content']
    assert inputs[1]['role'] == 'user'
    assert inputs[-1]['content'].startswith('259:')
    assert all(not row['content'].startswith('0:') for row in inputs[1:])
    assert sum(len(row['content']) for row in inputs) <= 130000
    assert len(store.messages(project)) == 260


def xlsx_fixture(extra=None):
    import io, zipfile
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Бюджет" r:id="s1"/><sheet name="Notes" r:id="s2"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="s1" Target="worksheets/sheet1.xml"/><Relationship Id="s2" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        archive.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Доход</t></si></sst>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>')
        archive.writestr('xl/worksheets/sheet2.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>Привет</t></is></c><c r="B1"><f>WEBSERVICE("https://invalid.test")</f></c></row></sheetData></worksheet>')
        if extra: archive.writestr(*extra)
    return stream.getvalue()


def test_xlsx_extracts_sheets_strings_and_cached_formulas_locally():
    text = text_file('Budget.XLSX', base64.b64encode(xlsx_fixture()).decode())
    assert 'Лист: Бюджет' in text and 'Лист: Notes' in text
    assert '| A1 | Доход |' in text and '| B1 | 2 |' in text and '| A1 | Привет |' in text
    assert 'формула без сохранённого результата' in text
    assert 'WEBSERVICE' not in text


def test_xlsx_above_old_upload_limit_and_expansion_limit():
    import os
    raw = xlsx_fixture(('media.bin', os.urandom(3*1024*1024)))
    assert len(raw) > 2*1024*1024
    assert 'Доход' in text_file('large.xlsx', base64.b64encode(raw).decode())
    with pytest.raises(ValueError, match='32 МБ'):
        text_file('bomb.xlsx', base64.b64encode(xlsx_fixture(('padding.xml', b'x'*(33*1024*1024)))).decode())
    with pytest.raises(ValueError, match='10 МБ'):
        text_file('large.txt', base64.b64encode(b'x'*(10*1024*1024+1)).decode())


def test_web_search_is_opt_in_and_persists_safe_citations(tmp_path):
    seen, citations = [], []
    output = 'Fresh fact [1]'
    def handler(request):
        seen.append(json.loads(request.content))
        annotations = [{'type':'url_citation', 'start_index':11,'end_index':14,'url':'https://example.org/source','title':'Source'}, {'type':'url_citation','start_index':0,'end_index':5,'url':'javascript:alert(1)','title':'Unsafe'}]
        events = [{'type':'response.output_text.delta','delta':output}, {'type':'response.completed','response':{'output':[{'type':'message','content':[{'type':'output_text','text':output,'annotations':annotations}]}]}}]
        return httpx.Response(200, text='\n\n'.join('data: '+json.dumps(e) for e in events)+'\n\n')
    store = Store(tmp_path)
    account = ChatGPTAccount(store, Vault({'access_token':'fixture','expires_at':time.time()+3600,'scope':PLAN_SCOPE}), httpx.Client(transport=httpx.MockTransport(handler)))
    assert ''.join(account.stream('fixture', [], '', threading.Event())) == output
    assert 'tools' not in seen[0]
    assert ''.join(account.stream('fixture', [], '', threading.Event(), web_search=True, on_citations=citations.extend)) == output
    assert seen[1]['tools'] == [{'type':'web_search'}] and seen[1]['store'] is False
    assert len(citations) == 1 and citations[0]['url'] == 'https://example.org/source'
    project = store.create_project('Tools')['id']
    message = store.add_message(project, 'assistant', output)
    store.message_tools(message, web_search=True)
    store.message_tools(message, citations=citations)
    snapshot = Store(tmp_path).snapshot(project)
    assert snapshot['messages'][0]['web_search'] and snapshot['messages'][0]['citations'] == citations


def test_web_search_rejection_has_actionable_error_and_does_not_retry(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={'error':{'message':'unsupported tool'}})
    account = ChatGPTAccount(Store(tmp_path), Vault({'access_token':'fixture','expires_at':time.time()+3600,'scope':PLAN_SCOPE}), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AccountError, match='выключите поиск'):
        list(account.stream('fixture', [], '', threading.Event(), web_search=True))
    assert len(requests) == 1


def pdf_fixture(image_bytes=0, blank=False, encrypted=False):
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, NumberObject
    writer = PdfWriter(); page = writer.add_blank_page(width=595, height=842)
    if not blank:
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content = DecodedStreamObject(); content.set_data(b'BT /F1 12 Tf 72 720 Td (Synthetic thesis content.) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(content)
    if image_bytes:
        image = DecodedStreamObject(); image.set_data(b'x'*image_bytes)
        image.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),NameObject('/Width'):NumberObject(1024),NameObject('/Height'):NumberObject(image_bytes//3072),NameObject('/ColorSpace'):NameObject('/DeviceRGB'),NameObject('/BitsPerComponent'):NumberObject(8)})
        page['/Resources'][NameObject('/XObject')] = DictionaryObject({NameObject('/Im1'):writer._add_object(image)})
    if encrypted: writer.encrypt('fixture-password')
    output = io.BytesIO(); writer.write(output); return output.getvalue()


def test_pdf_thesis_with_seven_megabytes_of_images_extracts_text_in_worker():
    raw = pdf_fixture(image_bytes=7*1024*1024)
    assert 7*1024*1024 < len(raw) < 10*1024*1024
    text = text_file('thesis.pdf', base64.b64encode(raw).decode())
    assert '[Страница 1]' in text and 'Synthetic thesis content.' in text


@pytest.mark.parametrize('raw,reason', [('scan','OCR'),('encrypted','паролем'),('broken','Не удалось')])
def test_pdf_scan_encryption_and_damage_have_clear_errors(raw,reason):
    value = pdf_fixture(blank=True) if raw=='scan' else pdf_fixture(encrypted=True) if raw=='encrypted' else b'%PDF-1.7\nbroken'
    with pytest.raises(ValueError,match=reason):
        text_file('document.pdf',base64.b64encode(value).decode())


def test_docx_with_seven_megabytes_of_pictures_uses_the_common_file_limit():
    import io,zipfile,os
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Текст диплома</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr('word/media/image1.png',os.urandom(7*1024*1024))
    assert 7*1024*1024 < len(stream.getvalue()) < 10*1024*1024
    assert text_file('thesis.docx',base64.b64encode(stream.getvalue()).decode())=='Текст диплома'


@pytest.mark.parametrize('bound',[False,True])
def test_rehydrate_initializes_only_application_data_and_verifies_gateway_binding(tmp_path,monkeypatch,bound):
    import context_chat_handlers.binding as binding
    import context_chat_handlers.lifecycle as lifecycle
    import adaos.sdk.data.skill_env as env
    import adaos.sdk.data.lifecycle as data_lifecycle
    calls=[]
    monkeypatch.setattr(env,'skill_data_root',lambda:tmp_path)
    monkeypatch.setattr(data_lifecycle,'ensure_database',lambda path:calls.append(('database',path)))
    monkeypatch.setattr(binding,'verify_binding',lambda value:calls.append(('binding',value['candidate_id'])))
    if bound:
        path=tmp_path/binding.BINDING;path.parent.mkdir(parents=True);path.write_text(json.dumps({'candidate_id':'verified-fixture'}),encoding='utf-8')
    lifecycle.rehydrate_runtime()
    assert calls==[('binding','verified-fixture')] if bound else calls==[('database','db/context_chat.db')]


def test_docx_markdown_keeps_document_order_headings_lists_tables_emphasis_links():
    import io,zipfile
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive:
        archive.writestr('word/document.xml','''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
          <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Глава 1</w:t></w:r></w:p>
          <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Важно</w:t></w:r><w:r><w:t> </w:t></w:r><w:hyperlink r:id="good"><w:r><w:t>Источник</w:t></w:r></w:hyperlink><w:hyperlink r:id="bad"><w:r><w:t>Опасная ссылка</w:t></w:r></w:hyperlink></w:p>
          <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Пункт списка</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Критерий</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Оценка</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>Текст | анализ</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>5</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>''')
        archive.writestr('word/styles.xml','<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>')
        archive.writestr('word/numbering.xml','<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')
        archive.writestr('word/_rels/document.xml.rels','<Relationships><Relationship Id="good" Target="https://example.org/paper"/><Relationship Id="bad" Target="javascript:alert(1)"/></Relationships>')
    text=text_file('thesis.docx',base64.b64encode(raw.getvalue()).decode())
    assert text.startswith('# Глава 1') and '**Важно**' in text and '- Пункт списка' in text
    assert '[Источник](<https://example.org/paper>)' in text and 'javascript:' not in text
    assert '| Критерий | Оценка |' in text and '| Текст \\| анализ | 5 |' in text
    assert text.index('Глава 1')<text.index('Важно')<text.index('Пункт списка')<text.index('Критерий')


def test_docx_rejects_entity_expansion():
    import io,zipfile
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive: archive.writestr('word/document.xml','<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>')
    with pytest.raises(ValueError,match='XML'):
        text_file('bad.docx',base64.b64encode(raw.getvalue()).decode())


def test_project_isolation_and_delete_cascade(tmp_path):
    store=Store(tmp_path)
    a=store.create_project('A',context='A private context')['id']
    b=store.create_project('B')['id']
    source=store.sources(a)[0]['id']
    with pytest.raises(KeyError): store.change_source(b,source,delete=True)
    assert store.sources(b)==[]
    store.add_message(a,'user','private',model='historical-model')
    store.delete_project(a)
    assert store.projects()[0]['id']==b
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0]==0


def test_source_removal_invalidates_memory_and_context(tmp_path):
    store=Store(tmp_path); p=store.create_project('A')['id']
    s=store.add_source(p,'Private source','text','remembered data')
    store.save_memory(p,{'summary':'remembered data'},store.project(p)['memory_revision'])
    store.change_source(p,s,delete=True)
    assert store.project(p)['memory']=={}
    assert store.project(p)['memory_status']=='stale'
    assert 'remembered data' not in Runtime(store,Disconnected(store)).context(p)[1][0]['content']


def test_memory_compare_and_swap_preserves_user_edits(tmp_path):
    store=Store(tmp_path); p=store.create_project('A')['id']
    store.save_memory(p,{'summary':'user edit'},0)
    with pytest.raises(ValueError): store.save_memory(p,{'summary':'stale inference'},0)
    assert store.project(p)['memory']['summary']=='user edit'


def test_adding_context_keeps_existing_memory_until_rebuild(tmp_path):
    store=Store(tmp_path); p=store.create_project('A')['id']
    store.save_memory(p,{'preferences':'Keep my correction'},0)
    store.add_source(p,'Extra context','text','New information')
    assert store.project(p)['memory']['preferences']=='Keep my correction'
    assert store.project(p)['memory_status']=='stale'


def test_api_crud_and_disconnected_send(tmp_path):
    client=TestClient(create_app(tmp_path,Disconnected))
    created=client.post(PREFIX+'/api/projects',json={'title':'Personal','context':'my history'})
    assert created.status_code==200
    p=created.json()['id']
    assert client.get(PREFIX+'/api/projects/'+p).json()['sources'][0]['title']=='Начальный контекст'
    assert client.post(PREFIX+'/api/projects/'+p+'/messages',json={'text':'hello'}).status_code==400
    assert client.get(PREFIX+'/api/projects/'+p).json()['messages']==[]
    assert client.patch(PREFIX+'/api/projects/'+p,json={'title':'Renamed'}).json()['title']=='Renamed'
    assert client.delete(PREFIX+'/api/projects/'+p).status_code==400
    assert client.delete(PREFIX+'/api/projects/'+p+'?confirmed=true').status_code==200
    assert client.get(PREFIX+'/api/state').json()['projects']==[]


def test_chat_worker_records_actual_model_and_completes(tmp_path):
    app=create_app(tmp_path,Connected); client=TestClient(app)
    p=client.post(PREFIX+'/api/projects',json={'title':'A'}).json()['id']
    client.patch(PREFIX+'/api/projects/'+p,json={'model':'test-model'})
    assert client.post(PREFIX+'/api/projects/'+p+'/messages',json={'text':'Hi'}).status_code==200
    for _ in range(100):
        data=client.get(PREFIX+'/api/projects/'+p).json()
        if data['runs'][0]['status']=='completed': break
        time.sleep(.01)
    assert data['runs'][0]['status']=='completed'
    assert data['messages'][-1]['content']=='Hello world'
    assert data['messages'][-1]['model']=='test-model'
    assert data['project']['memory']['summary']=='Test summary'


def test_foreign_attachment_is_rejected_before_message_write(tmp_path):
    app=create_app(tmp_path,Connected); store=app.state.store
    a=store.create_project('A',context='secret')['id']; b=store.create_project('B')['id']
    store.update_project(b,model='test-model')
    with pytest.raises(ValueError): app.state.runtime.launch(b,'chat','Hello',[store.sources(a)[0]['id']])
    assert store.messages(b)==[]


def test_model_changes_do_not_rewrite_history(tmp_path):
    store=Store(tmp_path); p=store.create_project('A')['id']
    store.add_message(p,'assistant','answer',model='old-model')
    store.update_project(p,model='new-model')
    assert store.messages(p)[0]['model']=='old-model'


def test_document_allowlist_and_binary_rejection():
    assert text_file('notes.txt',base64.b64encode('Привет'.encode()).decode())=='Привет'
    with pytest.raises(ValueError): text_file('movie.mp4',base64.b64encode(b'video').decode())
    with pytest.raises(ValueError): text_file('notes.txt',base64.b64encode(b'bad\0data').decode())


def test_oauth_pkce_and_invalid_state(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    account=ChatGPTAccount(Store(tmp_path),Vault())
    result=account.start(); values=parse_qs(urlsplit(result['url']).query)
    assert values['client_id']==['dynamic_agent_client']
    assert values['code_challenge_method']==['S256']
    assert PLAN_SCOPE in values['scope'][0]
    response=httpx.get(values['redirect_uri'][0]+'?state=invalid&code=ignored',trust_env=False)
    assert response.status_code==400 and account.pending is not None
    account.pending=None


def test_refresh_rotation_and_redacted_status(tmp_path):
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'access_token':'rotated-access','refresh_token':'rotated-refresh','expires_in':3600,'scope':PLAN_SCOPE})
    vault=Vault({'access_token':'old-access','refresh_token':'old-refresh','client_id':'issued','scope':PLAN_SCOPE,'expires_at':0,'email':'test@example.invalid'})
    account=ChatGPTAccount(Store(tmp_path),vault,httpx.Client(transport=httpx.MockTransport(handler)))
    assert account.access_token()=='rotated-access'
    assert vault.read()['refresh_token']=='rotated-refresh'
    body=requests[0].content.decode()
    assert 'grant_type=refresh_token' in body and 'scope=' not in body
    assert 'access_token' not in json.dumps(account.status())


@pytest.mark.parametrize('granted', [True,False])
def test_oauth_callback_exchanges_code_validates_identity_and_checks_scope(tmp_path,granted):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    def encode(raw): return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    public=key.public_key().public_numbers()
    jwks={'keys':[{'kid':'callback-key','kty':'RSA','n':encode(public.n.to_bytes(256,'big')),'e':encode(public.e.to_bytes(3,'big'))}]}
    seen=[]; vault=Vault()
    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/jwks.json'):
            return httpx.Response(200,json=jwks)
        header=encode(json.dumps({'alg':'RS256','kid':'callback-key'}).encode())
        payload=encode(json.dumps({'iss':AUTH,'aud':'issued-client','sub':'verified-subject','nonce':account.pending['nonce'],'exp':time.time()+300,'email':'test@example.invalid'}).encode())
        body=header+'.'+payload
        token=body+'.'+encode(key.sign(body.encode(),padding.PKCS1v15(),hashes.SHA256()))
        return httpx.Response(200,json={'id_token':token,'access_token':'private-access','refresh_token':'private-refresh','scope':PLAN_SCOPE if granted else 'openid email','expires_in':3600})
    account=ChatGPTAccount(Store(tmp_path),vault,httpx.Client(transport=httpx.MockTransport(handler)))
    account.start()
    callback=account.pending['redirect_uri']+'?state='+account.pending['state']+'&code=valid-code&client_id=issued-client'
    assert httpx.get(callback,trust_env=False).status_code==200
    assert account.status()['plan_enabled'] is granted
    assert account.pending is None
    assert account.store.setting('client_id')=='issued-client'
    assert b'code_verifier=' in seen[0].content
    if granted:
        assert vault.read()['subject']=='verified-subject'
        assert 'private-access' not in json.dumps(account.status())
    else:
        assert vault.read() is None
        assert 'разрешение' in account.status()['error']


@pytest.mark.parametrize('terminal,valid', [('response.completed',True),('response.failed',False),(None,False)])
def test_stream_requires_completed_and_uses_supported_contract(tmp_path,terminal,valid):
    seen=[]
    def handler(request):
        seen.append(json.loads(request.content))
        lines=['data: '+json.dumps({'type':'response.output_text.delta','delta':'partial'})]
        if terminal: lines.append('data: '+json.dumps({'type':terminal,'response':{'error':{'code':'subscription_sharing_usage_limit_exceeded'}}}))
        return httpx.Response(200,text='\n\n'.join(lines)+'\n\n')
    vault=Vault({'access_token':'test-secret','expires_at':time.time()+3600,'scope':PLAN_SCOPE})
    account=ChatGPTAccount(Store(tmp_path),vault,httpx.Client(transport=httpx.MockTransport(handler)))
    stream=account.stream('test-model',[{'role':'user','content':'Hi'}],'instructions',threading.Event())
    assert next(stream)=='partial'
    if valid: assert list(stream)==[]
    else:
        with pytest.raises(AccountError): list(stream)
    assert seen[0]['store'] is False and seen[0]['stream'] is True
    assert not {'conversation','previous_response_id','background','max_output_tokens'}.intersection(seen[0])


def test_id_token_signature_audience_and_nonce():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    def encode(raw): return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    public=key.public_key().public_numbers()
    jwks={'keys':[{'kid':'test-key','kty':'RSA','n':encode(public.n.to_bytes(256,'big')),'e':encode(public.e.to_bytes(3,'big'))}]}
    header=encode(json.dumps({'alg':'RS256','kid':'test-key'}).encode())
    payload=encode(json.dumps({'iss':AUTH,'aud':'issued-client','sub':'subject','nonce':'correct','exp':time.time()+60}).encode())
    body=header+'.'+payload
    token=body+'.'+encode(key.sign(body.encode(),padding.PKCS1v15(),hashes.SHA256()))
    assert validate_id_token(token,'issued-client','correct',jwks)['sub']=='subject'
    with pytest.raises(AccountError): validate_id_token(token,'issued-client','wrong',jwks)
    with pytest.raises(AccountError): validate_id_token(token,'another-client','correct',jwks)


def wait_run(store, project_id, run_id):
    for _ in range(300):
        run = store.run(project_id, run_id)
        if run['status'] not in ('queued', 'running'):
            return run
        time.sleep(.01)
    pytest.fail('Worker did not reach a terminal state')


def connected_runtime(tmp_path, account_type=Connected):
    store=Store(tmp_path); store.set_setting('auto_memory', False)
    project=store.create_project('Beta')['id']; store.update_project(project, model='test-model')
    return store, project, Runtime(store, account_type(store))


def test_idempotent_send_survives_lost_http_response(tmp_path):
    store,p,runtime=connected_runtime(tmp_path)
    first=runtime.launch(p,'chat','One turn',request_key='request-1')
    assert wait_run(store,p,first)['status']=='completed'
    assert runtime.launch(p,'chat','One turn',request_key='request-1')==first
    assert len(store.messages(p))==2
    assert len(store.runs(p))==1


def test_failed_turn_retry_retains_user_and_preserves_original_partial_answer(tmp_path):
    class Flaky(Connected):
        calls=[]
        def stream(self,model,inputs,instructions,cancelled):
            self.calls.append(inputs)
            if len(self.calls)==1:
                yield 'Partial'
                raise AccountError('Connection interrupted')
            yield 'Recovered answer'
    store,p,runtime=connected_runtime(tmp_path,Flaky)
    first=runtime.launch(p,'chat','My exact question',request_key='first')
    assert wait_run(store,p,first)['status']=='failed'
    ids=[row['id'] for row in store.messages(p)]
    second=runtime.launch(p,'chat',request_key='retry-first',retry_id=first)
    assert wait_run(store,p,second)['status']=='completed'
    assert [row['id'] for row in store.messages(p)][:2]==ids
    assert len(store.messages(p))==3 and store.messages(p)[1]['content']=='Partial'
    assert store.messages(p)[-1]['content']=='Recovered answer'
    assert sum(row['content']=='My exact question' for row in runtime.account.calls[-1])==1
    with pytest.raises(ValueError): runtime.launch(p,'chat',retry_id=first)


def test_turn_freezes_model_and_explicit_excluded_attachment(tmp_path):
    entered,release=threading.Event(),threading.Event()
    class Captured(Connected):
        def stream(self,model,inputs,instructions,cancelled):
            entered.set(); assert release.wait(3)
            self.captured=(model,inputs)
            yield 'Original model answer'
    store,p,runtime=connected_runtime(tmp_path,Captured)
    source=store.add_source(p,'Attachment','text','Attachment content')
    store.change_source(p,source,included=False)
    run=runtime.launch(p,'chat','Question',[source])
    assert entered.wait(3)
    store.add_source(p,'Later','text','This arrived after send')
    store.update_project(p,model='later-model')
    release.set(); assert wait_run(store,p,run)['status']=='completed'
    model,inputs=runtime.account.captured
    assert model=='test-model'
    assert 'Attachment content' in inputs[0]['content']
    assert 'This arrived after send' not in inputs[0]['content']
    assert store.messages(p)[-1]['model']=='test-model'


def test_late_cancelled_worker_cannot_overwrite_retried_answer(tmp_path):
    entered,release,finished=threading.Event(),threading.Event(),threading.Event()
    class Late(Connected):
        def stream(self,model,inputs,instructions,cancelled):
            if not entered.is_set():
                entered.set(); assert release.wait(3)
                yield 'Late old answer'
                finished.set()
            else:
                yield 'New successful answer'
    store,p,runtime=connected_runtime(tmp_path,Late)
    first=runtime.launch(p,'chat','Question'); assert entered.wait(3)
    runtime.cancel_project(p)
    second=runtime.launch(p,'chat',retry_id=first)
    assert wait_run(store,p,second)['status']=='completed'
    release.set()
    for _ in range(100):
        if first not in runtime.cancellations: break
        time.sleep(.01)
    assert store.messages(p)[-1]['content']=='New successful answer'
    assert store.messages(p)[-1]['status']=='completed'
    assert store.run(p,first)['status']=='cancelled'


def test_restart_preserves_data_and_marks_inflight_interrupted(tmp_path):
    store,p,_=connected_runtime(tmp_path)
    run,message=store.reserve_turn(p,'chat','Question','test-model',[], 'inflight')
    store.run_progress(run,message,'Partial response','running')
    store.save_memory(p,{'summary':'Keep this'},0)
    reopened=Store(tmp_path)
    assert reopened.messages(p)[-1]['content']=='Partial response'
    assert reopened.messages(p)[-1]['status']=='interrupted'
    assert reopened.run(p,run)['status']=='interrupted'
    assert reopened.project(p)['memory']['summary']=='Keep this'
    assert reopened.request_run(p,'inflight')['id']==run


def test_v1_database_upgrade_preserves_project_and_immutable_checksum(tmp_path):
    import sqlite3
    from context_chat_handlers.store import SCHEMA,now
    from adaos.domain.relational_storage import RelationalMigration
    folder=tmp_path/'db'; folder.mkdir()
    digest=RelationalMigration(version=1,name='context_chat_initial',statements=tuple(SCHEMA),idempotent=True,dialects=('sqlite',)).checksum
    with sqlite3.connect(folder/'context_chat.db') as db:
        for statement in SCHEMA: db.execute(statement)
        db.execute('CREATE TABLE adaos_schema_migrations(version INTEGER PRIMARY KEY,name TEXT,checksum TEXT,applied_at TEXT,staged INTEGER)')
        db.execute('INSERT INTO adaos_schema_migrations VALUES(1,?,?,?,0)',('context_chat_initial',digest,now()))
        db.execute('INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)',('legacy','Existing project',now(),now()))
    store=Store(tmp_path)
    assert store.project('legacy')['title']=='Existing project'
    with store.db() as db:
        assert db.execute('SELECT checksum FROM adaos_schema_migrations WHERE version=1').fetchone()[0]==digest
        assert db.execute('SELECT count(*) FROM adaos_schema_migrations').fetchone()[0]==4
    Store(tmp_path)


def test_memory_failure_keeps_answer_and_user_memory(tmp_path):
    class BadMemory(Connected):
        def stream(self,model,inputs,instructions,cancelled):
            yield 'invalid JSON' if 'Обнови память' in inputs[-1]['content'] else 'Answer'
    store,p,runtime=connected_runtime(tmp_path,BadMemory)
    store.set_setting('auto_memory',True); store.save_memory(p,{'summary':'User correction'},0)
    run=runtime.launch(p,'chat','Question'); result=wait_run(store,p,run)
    assert result['status']=='completed' and result['error']
    assert store.messages(p)[-1]['content']=='Answer'
    assert store.project(p)['memory']['summary']=='User correction'
    assert store.project(p)['memory_status']=='stale'


def test_export_is_project_scoped_and_excludes_credentials(tmp_path):
    app=create_app(tmp_path,Disconnected); client=TestClient(app); store=app.state.store
    p=store.create_project('Portable',context='Source text')['id']
    other=store.create_project('Other',context='Other private context')['id']
    store.set_setting('client_id','private-registration'); store.set_setting('host_id','private-host')
    exported=client.get(PREFIX+'/api/projects/'+p+'/export').json()
    assert exported['schema']=='context_chat.project.v1'
    assert exported['sources'][0]['content']=='Source text'
    assert not any(value in json.dumps(exported) for value in ['private-registration','private-host','Other private context',other])


def test_cross_site_and_rebound_host_requests_cannot_mutate_data(tmp_path):
    app=create_app(tmp_path,Disconnected); client=TestClient(app)
    for headers in [{'Origin':'https://attacker.invalid'},{'Host':'attacker.invalid'},{'Sec-Fetch-Site':'cross-site'}]:
        assert client.post(PREFIX+'/api/projects',json={'title':'Forbidden'},headers=headers).status_code==403
    assert app.state.store.projects()==[]


def test_audit_records_actions_without_user_content_or_query(tmp_path):
    app=create_app(tmp_path,Disconnected); client=TestClient(app)
    client.post(PREFIX+'/api/projects?private=hidden-query',json={'title':'hidden-content'})
    with app.state.store.db() as db: rows=[dict(row) for row in db.execute('SELECT * FROM audit_events')]
    assert rows[0]['action']=='POST '+PREFIX+'/api/projects'
    assert rows[0]['status']==200
    assert 'hidden-' not in json.dumps(rows)


def test_account_verification_requires_nonempty_completed_inference(tmp_path):
    def handler(request):
        if request.url.path.endswith('/models'):
            return httpx.Response(200,json={'models':[{'slug':'account-text','display_name':'Account text','visibility':'list'}]})
        return httpx.Response(200,text='data: {"type":"response.output_text.delta","delta":"OK"}\n\ndata: {"type":"response.completed"}\n\n')
    vault=Vault({'access_token':'secret','scope':PLAN_SCOPE,'client_id':'issued','subject':'subject','expires_at':time.time()+3600})
    account=ChatGPTAccount(Store(tmp_path),vault,httpx.Client(transport=httpx.MockTransport(handler)))
    assert not account.status()['inference_verified']
    account.verify()
    for _ in range(200):
        if account.probe['status']!='running': break
        time.sleep(.01)
    assert account.probe['status']=='completed'
    assert account.status()['inference_verified']
    assert account.status()['verified_model']=='account-text'
    assert 'secret' not in json.dumps(account.status())


def test_empty_completed_response_is_not_account_verification(tmp_path):
    transport=httpx.MockTransport(lambda request:httpx.Response(200,text='data: {"type":"response.completed"}\n\n'))
    vault=Vault({'access_token':'secret','scope':PLAN_SCOPE,'expires_at':time.time()+3600})
    account=ChatGPTAccount(Store(tmp_path),vault,httpx.Client(transport=transport))
    with pytest.raises(AccountError): list(account.stream('text',[], '',threading.Event()))
    assert not account.status()['inference_verified']


def test_expired_unrefreshable_session_requires_reauthorization(tmp_path):
    account=ChatGPTAccount(Store(tmp_path),Vault({'access_token':'expired','scope':PLAN_SCOPE,'expires_at':0}))
    assert account.status()['needs_reauth']
    assert not account.status()['plan_enabled']


def test_revoked_refresh_disables_plan_until_reauthorization(tmp_path):
    vault=Vault({'access_token':'old','refresh_token':'revoked','scope':PLAN_SCOPE,'expires_at':0,'client_id':'issued','subject':'user'})
    transport=httpx.MockTransport(lambda request:httpx.Response(400,json={'error':'invalid_grant'}))
    account=ChatGPTAccount(Store(tmp_path),vault,httpx.Client(transport=transport))
    with pytest.raises(AccountError): account.access_token()
    assert account.status()['needs_reauth']
    assert not account.status()['plan_enabled']
    assert vault.read()['refresh_token']=='revoked'
    resumed=ChatGPTAccount(account.store,vault,httpx.Client(transport=transport))
    assert resumed.status()['needs_reauth']


def test_http_usage_limit_is_actionable_and_does_not_expose_provider_body(tmp_path):
    transport=httpx.MockTransport(lambda request:httpx.Response(429,text='secret-provider-body'))
    account=ChatGPTAccount(Store(tmp_path),Vault({'access_token':'token','scope':PLAN_SCOPE,'expires_at':time.time()+3600}),httpx.Client(transport=transport))
    with pytest.raises(AccountError,match='ресурсы плана') as error:
        list(account.stream('text',[], '',threading.Event()))
    assert 'secret-provider-body' not in str(error.value)


def fake_keyring(monkeypatch):
    import keyring
    records={}
    monkeypatch.setattr(keyring,'get_password',lambda service,identity:records.get((service,identity)))
    monkeypatch.setattr(keyring,'set_password',lambda service,identity,value:records.__setitem__((service,identity),value))
    monkeypatch.setattr(keyring,'delete_password',lambda service,identity:records.pop((service,identity),None))
    return records


def test_large_oauth_session_is_encrypted_with_only_small_key_in_os_vault(tmp_path,monkeypatch):
    records=fake_keyring(monkeypatch)
    vault=KeyringVault('stable-host',tmp_path/'files/session.enc')
    session={'access_token':'large-private-token'*1500,'refresh_token':'rotating-refresh','id_token':'identity'*1200}
    vault.write(session)
    assert vault.read()==session
    assert len(next(iter(records.values())))==44
    assert b'large-private-token' not in vault.path.read_bytes()
    assert b'rotating-refresh' not in vault.path.read_bytes()
    assert len(vault.path.read_bytes())>10000
    vault.clear()
    assert vault.read() is None and not records


def test_atomic_credential_write_failure_keeps_previous_session(tmp_path,monkeypatch):
    fake_keyring(monkeypatch)
    vault=KeyringVault('stable-host',tmp_path/'files/session.enc')
    vault.write({'access_token':'previous'})
    import adaos.services.ai.chatgpt as account_module
    def fail_replace(*args): raise OSError('Simulated disk error')
    monkeypatch.setattr(account_module.os,'replace',fail_replace)
    with pytest.raises(AccountError): vault.write({'access_token':'new-secret'})
    assert vault.read()=={'access_token':'previous'}
    assert len(list(vault.path.parent.iterdir()))==1


def test_credential_key_survives_runtime_path_change_when_data_is_migrated(tmp_path,monkeypatch):
    import shutil
    fake_keyring(monkeypatch)
    old=KeyringVault('same-host',tmp_path/'old/session.enc'); old.write({'access_token':'preserved'})
    target=tmp_path/'new/session.enc';target.parent.mkdir();shutil.copyfile(old.path,target)
    upgraded=KeyringVault('same-host',target)
    assert upgraded.read()=={'access_token':'preserved'}


def test_drain_for_unstarted_owner_does_not_contact_other_runtime(tmp_path):
    from context_chat_handlers.lifecycle import drain_root,MARKER
    def forbidden(request): pytest.fail('An unstarted owner must not contact a different service')
    receipt=drain_root(tmp_path,http=httpx.Client(transport=httpx.MockTransport(forbidden)))
    assert receipt['writers_stopped'] and (tmp_path/MARKER).is_file()


def test_drain_checks_runtime_identity_before_sending_shutdown(tmp_path):
    from context_chat_handlers.lifecycle import drain_root,INSTANCE,atomic_json,owner
    atomic_json(tmp_path/INSTANCE,{'owner':owner(tmp_path),'pid':123})
    seen=[]
    def handler(request):
        seen.append(request.method)
        return httpx.Response(200,json={'owner':'different-owner','pid':123})
    with pytest.raises(RuntimeError,match='different runtime'):
        drain_root(tmp_path,http=httpx.Client(transport=httpx.MockTransport(handler)),alive=lambda pid:True)
    assert seen==['GET']


def test_drain_waits_for_verified_service_process_exit(tmp_path):
    from context_chat_handlers.lifecycle import drain_root,INSTANCE,MARKER,atomic_json,owner
    atomic_json(tmp_path/INSTANCE,{'owner':owner(tmp_path),'pid':123})
    running=[True]
    def handler(request):
        if request.method=='GET': return httpx.Response(200,json={'owner':owner(tmp_path),'pid':123})
        nonce=json.loads((tmp_path/MARKER).read_text())['nonce']
        assert request.headers['X-ContextChat-Owner']==nonce
        running[0]=False
        return httpx.Response(200,json={'ok':True})
    result=drain_root(tmp_path,http=httpx.Client(transport=httpx.MockTransport(handler)),alive=lambda pid:running[0])
    assert result['writers_stopped']


def test_drain_endpoint_requires_owner_nonce_and_blocks_new_writes(tmp_path):
    from context_chat_handlers.lifecycle import MARKER,atomic_json,owner
    app=create_app(tmp_path,Disconnected);client=TestClient(app)
    exited=threading.Event();app.state.shutdown=exited.set
    atomic_json(tmp_path/MARKER,{'owner':owner(tmp_path),'nonce':'private-owner-nonce'})
    assert client.post(PREFIX+'/api/projects',json={'title':'Blocked'}).status_code==503
    assert client.get(PREFIX+'/api/state').status_code==200
    assert client.post(PREFIX+'/internal/drain').status_code==403
    assert not exited.is_set()
    assert client.post(PREFIX+'/internal/drain',headers={'X-ContextChat-Owner':'private-owner-nonce'}).status_code==200
    assert exited.wait(2)
    assert not app.state.runtime.accepting


def test_native_data_contract_inventories_encrypted_credentials_and_rejects_unknown_files(tmp_path):
    import yaml
    from context_chat_handlers.lifecycle import MARKER,INSTANCE,atomic_json,owner
    from adaos.services.applications.data_lifecycle import require_native_tools,declared_databases,declared_state_files,declared_coordination_files,inventory
    manifest=yaml.safe_load((ROOT/'skill.yaml').read_text(encoding='utf-8'))
    require_native_tools(manifest)
    Store(tmp_path)
    (tmp_path/'files').mkdir();(tmp_path/'files/chatgpt-session.enc').write_bytes(b'opaque-encrypted-fixture')
    atomic_json(tmp_path/MARKER,{'owner':owner(tmp_path),'nonce':'fixture'})
    atomic_json(tmp_path/INSTANCE,{'owner':owner(tmp_path),'pid':123})
    def check(): inventory(tmp_path,declared_databases(manifest),state_files=declared_state_files(manifest),coordination_files=declared_coordination_files(manifest))
    check()
    (tmp_path/'untracked-private-file').write_text('must not be silently dropped')
    with pytest.raises(ValueError,match='Undeclared runtime data'): check()


def test_real_service_process_drains_without_stopping_another_service(tmp_path):
    import os,socket,subprocess
    from context_chat_handlers.lifecycle import drain_root,MARKER,owner
    root=tmp_path/'data';(root/'db').mkdir(parents=True)
    Store(root)  # Model Core's governed schema initialization before process start.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'ADAOS_SKILL_ENV_PATH':str(root/'db/skill_env.json'),'ADAOS_SERVICE_PORT':str(port),'PYTHONPATH':str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH','')}
    child=subprocess.Popen([sys.executable,'-m','handlers.service'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        with httpx.Client(trust_env=False,timeout=1) as http:
            for _ in range(100):
                try:
                    health=http.get(f'http://127.0.0.1:{port}'+PREFIX+'/health').json()
                    if health.get('owner')==owner(root): break
                except httpx.HTTPError: pass
                if child.poll() is not None: pytest.fail('Fixture service exited before readiness')
                time.sleep(.1)
            else: pytest.fail('Fixture service did not become ready')
        receipt=drain_root(root,port=port)
        assert receipt['writers_stopped'] and (root/MARKER).exists()
        assert child.wait(timeout=5)==0
    finally:
        if child.poll() is None: child.terminate();child.wait(timeout=5)


def bound_fixture(tmp_path):
    import hashlib,shutil
    from adaos.domain.artifact_release import ArtifactSourceRef,ArtifactPackageRef,ProjectRelease
    from context_chat_handlers.binding import SCHEMA
    root=tmp_path/'context_chat-fixture';source=root/'skills/context_chat_service'
    for name in ['handlers','web']:
        shutil.copytree(ROOT/name,source/name,ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(ROOT/'skill.yaml',source/'skill.yaml')
    ref=ArtifactSourceRef(forge='github',repository='example/fixture',revision='0'*40,path_scope=('skills/context_chat_service/',))
    package=ArtifactPackageRef(kind='skill',artifact_id='context_chat_service',version='0.2.2',digest='sha256:'+'a'*64,manifest_digest='sha256:'+'b'*64,source_ref=ref)
    release=ProjectRelease(project_id='context_chat',version='0.2.2',source_ref=ref,components=(package,)).seal()
    path=root/'.adaos/releases'/(release.release_digest.split(':')[1]+'.json');path.parent.mkdir(parents=True);path.write_text(json.dumps(release.to_dict()),encoding='utf-8')
    env=root/'skills/.runtime/context_chat_service/v0.2/data/db/skill_env.json';env.parent.mkdir(parents=True)
    value={'schema':SCHEMA,'application_id':'context_chat','candidate_id':root.name,'release_digest':release.release_digest,'component_digest':package.digest,
           'runtime_root':str(root.resolve()),'source_root':str(source.resolve()),'env_path':str(env.resolve()),
           'files':{p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}}
    return value,env.parent.parent


def test_runtime_binding_verifies_release_component_and_source(tmp_path):
    from context_chat_handlers.binding import verify_binding
    value,_=bound_fixture(tmp_path)
    result=verify_binding(value)
    assert result['release_digest']==value['release_digest']
    assert str(result['source'])==value['source_root']


def test_runtime_binding_rejects_changed_code(tmp_path):
    from context_chat_handlers.binding import verify_binding
    value,_=bound_fixture(tmp_path)
    (Path(value['source_root'])/'web/app.js').write_text('tampered code')
    with pytest.raises(RuntimeError,match='integrity'): verify_binding(value)


def test_runtime_binding_cannot_route_to_another_owner(tmp_path):
    from context_chat_handlers.binding import verify_binding
    value,_=bound_fixture(tmp_path)
    value['env_path']=str(tmp_path/'foreign/db/skill_env.json')
    with pytest.raises(RuntimeError,match='escaped'): verify_binding(value)


def test_bound_process_uses_selected_release_data_and_drains_real_child(tmp_path):
    import os,socket,subprocess
    from context_chat_handlers.binding import BINDING
    from context_chat_handlers.lifecycle import atomic_json,owner,drain_root
    value,target_data=bound_fixture(tmp_path)
    Store(target_data)
    bridge=tmp_path/'bridge/data';(bridge/'db').mkdir(parents=True)
    atomic_json(bridge/BINDING,value)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'ADAOS_SKILL_ENV_PATH':str(bridge/'db/skill_env.json'),'ADAOS_SERVICE_PORT':str(port),'PYTHONPATH':str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH','')}
    env.pop('CONTEXT_CHAT_PINNED_RELEASE',None)
    child=subprocess.Popen([sys.executable,'-m','handlers.service'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        with httpx.Client(trust_env=False,timeout=1) as http:
            for _ in range(120):
                try:
                    health=http.get(f'http://127.0.0.1:{port}'+PREFIX+'/health').json()
                    if health.get('owner')==owner(target_data): break
                except httpx.HTTPError: pass
                if child.poll() is not None: pytest.fail('Bound fixture exited before readiness')
                time.sleep(.1)
            else: pytest.fail('Bound fixture did not become ready')
        assert health['release_digest']==value['release_digest']
        assert health['pid']!=child.pid
        assert (target_data/'db/context_chat.db').is_file()
        assert not (bridge/'db/context_chat.db').exists()
        assert drain_root(target_data,port=port)['writers_stopped']
        assert child.wait(timeout=5)==0
    finally:
        if child.poll() is None: child.terminate();child.wait(timeout=5)
