"""M1 qualification: deterministic providers, synthetic documents, no network."""
import base64
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import test_context_chat as baseline
from context_chat_handlers.store import Store, digest
from context_chat_handlers.legacy_store import Store as LegacyStore
from context_chat_handlers.service import PREFIX, create_app
from context_chat_handlers.runtime import Runtime
from context_chat_handlers.profiles import descriptor,validate_profile
from context_chat_handlers.openai_account import AccountError


class Account:
    binding='owner-account-a'
    def __init__(self,store): self.store=store; self.calls=[]
    def status(self): return {'plan_enabled':self.binding is not None,'binding_ref':self.binding,'generation':0}
    def models(self,refresh=False): return [descriptor({'id':'reasoner','capabilities':{'reasoning_efforts':['low','high'],'default_reasoning_effort':'low','web_search':True}}),descriptor({'id':'unknown'}),descriptor({'id':'offline-tools','capabilities':{'reasoning_efforts':[],'web_search':False}})]
    def stream(self,model,inputs,instructions,cancelled,**options):
        self.calls.append({'model':model,'inputs':inputs,'options':options})
        yield 'Synthetic answer'


@pytest.fixture
def workspace(tmp_path):
    app=create_app(tmp_path,Account); store=app.state.store; store.set_setting('auto_memory',False)
    project=store.create_project('Student reviews')['id']
    a=store.create_chat(project,'Student A')['id']; b=store.create_chat(project,'Student B')['id']
    return app,store,project,a,b,TestClient(app)


def configure(store,project,chat,account,**changes):
    current=store.chat(project,chat)
    profile=validate_profile({**current['profile'],'model':'reasoner',**changes},account.models(),account.binding)
    store.save_profile(project,chat,profile,current['profile_revision'])
    return profile


def wait(store,project,run):
    for _ in range(300):
        result=store.run(project,run)
        if result['status'] not in ('queued','running'): return result
        time.sleep(.01)
    raise AssertionError('Synthetic inference did not finish')


def test_legacy_migration_preserves_history_sources_and_ciphertext_with_private_snapshot(tmp_path):
    old=LegacyStore(tmp_path); project=old.create_project('Legacy','Description','Rubric')['id']
    old.update_project(project,model='legacy-model'); old.save_memory(project,{'summary':'Remember'},old.project(project)['memory_revision'])
    first=old.add_message(project,'user','Identical'); second=old.add_message(project,'user','Identical')
    old.message_tools(first,web_search=True); old.message_tools(first,citations=[{'url':'https://example.invalid','title':'Citation','start_index':0,'end_index':3}])
    path=tmp_path/'files/chatgpt-session.enc'; path.parent.mkdir(); path.write_bytes(b'encrypted-owner-fixture')
    before=old.snapshot(project,full_history=True)
    store=Store(tmp_path); after=store.snapshot(project,full_history=True)
    assert [m['id'] for m in after['messages']]==[first,second]
    assert [m['content'] for m in after['messages']]==[m['content'] for m in before['messages']]
    assert after['messages'][0]['citations']==before['messages'][0]['citations']
    assert after['chat']['memory']==before['project']['memory']
    assert after['chat']['profile']['model']=='legacy-model'
    assert after['sources'][0]['id']==before['sources'][0]['id'] and after['sources'][0]['included']
    assert path.read_bytes()==b'encrypted-owner-fixture'
    with store.db() as db: receipt=json.loads(db.execute('SELECT receipt FROM migration_journal').fetchone()[0])
    assert receipt['before']==receipt['after']
    recovery=Path(receipt['recovery_ref']); assert (recovery/'before.sqlite').exists()
    assert (recovery/'chatgpt-session.enc').read_bytes()==path.read_bytes()
    again=Store(tmp_path)
    assert again.chat(project,'legacy-'+project)['conversation_ref']==after['chat']['conversation_ref']
    with again.db() as db: assert db.execute('SELECT count(*) FROM migration_journal').fetchone()[0]==1


def test_governed_schema_migration_is_verified_without_reexecuting_sql(tmp_path):
    from context_chat_handlers.store import MIGRATIONS
    from adaos.services.applications.sqlite_data_transition import initialize_sqlite_schema
    from adaos.domain.relational_storage import RelationalMigration
    old=LegacyStore(tmp_path); project=old.create_project('Managed',context='Preserved')['id']
    migrations=[RelationalMigration(version=v,name=n,statements=tuple(sql),idempotent=v==1,dialects=('sqlite',)) for v,n,sql in MIGRATIONS]
    initialize_sqlite_schema(old.path,migrations,development=True)
    migrated=Store(tmp_path)
    source=migrated.sources(project,True)[0]
    assert source['digest']==digest('Preserved')
    with migrated.db() as db: assert json.loads(db.execute('SELECT receipt FROM migration_journal').fetchone()[0])['recovery_authority']=='core.application_data_lifecycle'


def test_students_isolate_upload_memory_history_and_share_only_explicit_rubric(workspace):
    app,store,p,a,b,_=workspace; runtime=app.state.runtime
    student=store.add_source(p,'Student A thesis','file','private-a-content',chat_id=a)
    rubric=store.add_source(p,'Rubric','text','common-rubric',chat_id=a)
    store.change_source(p,rubric,included=True,chat_id=b)
    chat=store.chat(p,a); store.save_memory(p,{'summary':'private-a-memory'},chat['memory_revision'],chat_id=a)
    store.add_message(p,'user','private-a-question',chat_id=a)
    _,inputs,_=runtime.context_snapshot(p,b)
    text=json.dumps(inputs)
    assert 'common-rubric' in text and all(v not in text for v in ('private-a-content','private-a-memory','private-a-question'))
    assert not next(s for s in store.sources(p,chat_id=b) if s['id']==student)['included']
    assert store.messages(p,chat_id=b)==[]


def test_chat_projection_does_not_expose_the_legacy_default_chat_memory(workspace):
    _,store,p,a,b,client=workspace
    main=store.default_chat(p)
    store.save_memory(p,{'summary':'default-chat-private-facts'},main['memory_revision'])
    data=client.get(PREFIX+f'/api/projects/{p}/chats/{b}').json()
    assert 'default-chat-private-facts' not in json.dumps(data)
    assert 'memory' not in data['project'] and data['chat']['memory']=={}


def test_project_default_model_changes_only_seed_future_chats(workspace):
    app,store,p,a,b,client=workspace
    configure(store,p,a,app.state.account)
    original=store.chat(p,a)['profile']
    response=client.patch(PREFIX+f'/api/projects/{p}/metadata',json={'default_model':'unknown','expected_revision':0})
    assert response.status_code==200
    assert store.chat(p,a)['profile']==original and store.chat(p,b)['profile']['model']==''
    new=client.post(PREFIX+f'/api/projects/{p}/chats',json={'title':'New review'}).json()
    assert new['profile']['model']=='unknown' and new['profile']['account_binding']==Account.binding
    assert new['profile']['effort_mode']=='provider_default' and new['profile']['tools']==[]
    assert client.patch(PREFIX+f'/api/projects/{p}/metadata',json={'title':'Stale','expected_revision':0}).status_code==400


def test_all_chat_parent_operations_reject_cross_project(workspace):
    _,store,p,a,_,client=workspace; other=store.create_project('Other')['id']
    source=store.add_source(other,'private','text','secret')
    assert client.get(PREFIX+f'/api/projects/{other}/chats/{a}').status_code==404
    assert client.patch(PREFIX+f'/api/projects/{p}/chats/{a}/sources/{source}',json={'included':True}).status_code==404
    assert client.get(PREFIX+f'/api/projects/{p}/sources/{source}/original').status_code==404
    assert client.put(PREFIX+f'/api/projects/{p}/chats/{a}/sources/{source}',json={'content':'overwrite'}).status_code==404
    with pytest.raises(ValueError): app=Runtime(store,Account(store)).context_snapshot(p,a,[source])


def test_memory_shared_requires_explicit_consent_and_cas(workspace):
    _,store,p,a,b,client=workspace
    url=PREFIX+f'/api/projects/{p}/shared-memory'
    body={'summary':'shared grading instructions','expected_revision':0}
    assert client.put(url,json=body).status_code==400
    assert client.put(url,json={**body,'confirmed_shared':True,'promoted_from':a}).status_code==200
    assert client.put(url,json={**body,'confirmed_shared':True}).status_code==400
    assert 'shared grading instructions' in json.dumps(Runtime(store,Account(store)).context_snapshot(p,b)[1])
    assert store.chat(p,b)['memory']=={}


def test_source_versions_pin_other_chat_and_removal_revokes_every_version(workspace):
    app,store,p,a,b,_=workspace
    source=store.add_source(p,'Rubric','text','version-one',chat_id=a)
    store.change_source(p,source,included=True,chat_id=b)
    store.revise_source(p,source,'version-two',chat_id=a)
    assert store.sources(p,True,a)[0]['content']=='version-two'
    assert store.sources(p,True,b)[0]['content']=='version-one'
    configure(store,p,b,app.state.account)
    run=app.state.runtime.launch(p,'chat','Review',chat_id=b); assert wait(store,p,run)['status']=='completed'
    snapshot=store.run(p,run)['snapshot']
    assert snapshot['context']['source_revisions'][0]['revision']==1
    store.change_source(p,source,delete=True,chat_id=a)
    assert store.sources(p,True,b)==[]
    with pytest.raises(ValueError): store.validate_snapshot_sources(p,snapshot)
    assert store.run(p,run)['snapshot']['inputs']==[]
    assert all(not r['readable'] for r in store.source_versions(p,source))


def test_original_archive_is_opt_in_download_export_and_purge(workspace):
    _,store,p,a,_,client=workspace; base=PREFIX+f'/api/projects/{p}'
    path=base+f'/chats/{a}/sources'; raw=b'# original document'
    upload={'title':'notes.md','file_base64':base64.b64encode(raw).decode()}
    one=client.post(path,json=upload).json()['id']
    assert client.get(base+f'/sources/{one}/original').status_code==404
    assert client.post(PREFIX+'/api/settings',json={'retain_originals':True}).status_code==200
    two=client.post(path,json=upload).json()['id']
    assert client.get(base+f'/sources/{two}/original').content==raw
    exported=client.get(base+'/workspace-export?include_originals=true').json()
    assert base64.b64decode(next(s for s in exported['sources'] if s['id']==two)['original_base64'])==raw
    assert client.post(PREFIX+'/api/settings',json={'retain_originals':False}).status_code==200
    assert client.get(base+f'/sources/{two}/original').status_code==404
    assert store.sources(p,True,a)[0]['content']=='# original document'


def test_profile_effort_tools_cas_unknown_and_no_name_guesses(workspace):
    app,store,p,a,_,client=workspace; url=PREFIX+f'/api/projects/{p}/chats/{a}/profile'
    def write(profile,revision=0,**extra): return client.put(url,json={'profile':profile,'expected_revision':revision,**extra})
    profile={'model':'reasoner','effort_mode':'explicit','effort':'high','tools':['web_search']}
    assert write(profile,capability_revision='stale').status_code==400
    result=write(profile).json(); assert result['profile']['account_binding']==Account.binding
    assert write(profile).status_code==400
    assert write({**profile,'effort':'ultra'},1).status_code==400
    assert write({**profile,'model':'unknown'},1).status_code==400
    assert write({**profile,'model':'offline-tools','effort_mode':'provider_default'},1).status_code==400
    assert descriptor({'id':'reasoning-ultra-test'})['capabilities']['efforts'] is None
    assert write({'model':'unknown','effort_mode':'provider_default','tools':[]},1).status_code==200


def test_immutable_run_profile_context_and_focus_race(workspace):
    app,store,p,a,b,_=workspace; entered,release=threading.Event(),threading.Event()
    profile=configure(store,p,a,app.state.account,effort_mode='explicit',effort='high',tools=['web_search'])
    source=store.add_source(p,'Original','text','original-context',chat_id=a)
    original=app.state.account.stream
    def blocked(*args,**kwargs):
        entered.set(); assert release.wait(5); yield from original(*args,**kwargs)
    app.state.account.stream=blocked
    run=app.state.runtime.launch(p,'chat','original-question',chat_id=a,request_key='same-key')
    assert entered.wait(5)
    configure(store,p,a,app.state.account,effort_mode='explicit',effort='low',tools=[])
    store.add_source(p,'Later','text','later-context',chat_id=a)
    configure(store,p,b,app.state.account)
    release.set(); assert wait(store,p,run)['status']=='completed'
    call=app.state.account.calls[0]
    assert call['options']['reasoning_effort']=='high' and call['options']['web_search'] is True
    assert 'original-context' in json.dumps(call['inputs']) and 'later-context' not in json.dumps(call['inputs'])
    assert len(store.messages(p,chat_id=b))==0 and len(store.messages(p,chat_id=a))==2
    saved=store.run(p,run)['snapshot']; assert saved['profile']==profile and saved['chat_id']==a
    assert app.state.runtime.launch(p,'chat','ignored',chat_id=a,request_key='same-key')==run


def test_retry_uses_original_snapshot_keeps_partial_and_never_falls_back(workspace):
    app,store,p,a,_,_=workspace; account=app.state.account
    configure(store,p,a,account,effort_mode='explicit',effort='high')
    normal=account.stream
    def fail(*args,**kwargs):
        yield 'Partial answer'; raise AccountError('Synthetic provider rejection')
    account.stream=fail
    first=app.state.runtime.launch(p,'chat','question',chat_id=a); assert wait(store,p,first)['status']=='failed'
    original=store.run(p,first)['snapshot']
    configure(store,p,a,account,effort_mode='explicit',effort='low')
    account.stream=normal
    second=app.state.runtime.launch(p,'chat',chat_id=a,retry_id=first)
    assert wait(store,p,second)['status']=='completed'
    assert store.run(p,second)['snapshot']==original
    assert [m['content'] for m in store.messages(p,chat_id=a)]==['question','Partial answer','Synthetic answer']
    assert account.calls[-1]['options']['reasoning_effort']=='high'
    with pytest.raises(ValueError): app.state.runtime.launch(p,'chat',chat_id=a,retry_id=first)


def test_revoked_or_changed_account_never_uses_a_different_binding(workspace):
    app,store,p,a,_,client=workspace; configure(store,p,a,app.state.account)
    app.state.account.binding='owner-account-b'
    with pytest.raises(ValueError,match='Аккаунт изменился'): app.state.runtime.launch(p,'chat','Do not send',chat_id=a)
    app.state.account.binding=None
    assert client.post(PREFIX+f'/api/projects/{p}/chats/{a}/messages',json={'text':'Do not send'}).status_code==400
    assert not app.state.account.calls and store.messages(p,chat_id=a)==[]


def test_profile_change_during_catalog_admission_cannot_mix_snapshot_revisions(workspace):
    app,store,p,a,_,_=workspace; account=app.state.account
    configure(store,p,a,account,effort_mode='explicit',effort='high')
    models=account.models
    def change_during_read(*args,**kwargs):
        current=store.chat(p,a)
        store.save_profile(p,a,{**current['profile'],'effort':'low'},current['profile_revision'])
        return models()
    account.models=change_during_read
    with pytest.raises(ValueError,match='изменились'):
        app.state.runtime.launch(p,'chat','Do not charge',chat_id=a)
    assert not account.calls and store.messages(p,chat_id=a)==[]


def test_reasoning_adapter_emits_exact_responses_payload_and_honors_binding(tmp_path):
    import httpx
    from context_chat_handlers.openai_account import ChatGPTAccount,PLAN_SCOPE
    payloads=[]
    credentials={'access_token':'fixture-token','expires_at':time.time()+3600,'scope':PLAN_SCOPE,'subject':'subject-a','client_id':'client-a'}
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,text='data: '+json.dumps({'type':'response.output_text.delta','delta':'Answer'})+'\n\ndata: '+json.dumps({'type':'response.completed','response':{'output':[]}})+'\n\n')
    account=ChatGPTAccount(Store(tmp_path),baseline.Vault(credentials),httpx.Client(transport=httpx.MockTransport(handler)))
    assert ''.join(account.stream('exact-model',[{'role':'user','content':'Question'}],'Instructions',threading.Event(),reasoning_effort='high',expected_binding=account.binding_ref()))=='Answer'
    assert payloads[0]['reasoning']=={'effort':'high'} and payloads[0]['model']=='exact-model'
    with pytest.raises(AccountError): list(account.stream('exact-model',[],'',threading.Event(),expected_binding='foreign-binding'))
    assert len(payloads)==1


def test_long_chat_paging_search_archive_presets_and_project_export(workspace):
    _,store,p,a,b,client=workspace; base=PREFIX+f'/api/projects/{p}'
    for i in range(125): store.add_message(p,'user',str(i),chat_id=a)
    latest=client.get(base+f'/chats/{a}').json()
    assert len(latest['messages'])==50 and latest['history']['total']==125
    assert client.get(base+f'/chats/{b}').json()['history']['total']==0
    assert len(client.get(base+'/workspace-export').json()['chats'][1]['messages'])==125
    assert client.patch(base+f'/chats/{a}',json={'archived':True}).status_code==200
    assert a not in {r['id'] for r in client.get(base+'/chats').json()['items']}
    assert client.get(base+'/chats?archived=true&q=Student').json()['items'][0]['id']==a
    source=store.add_source(p,'Shared rubric','text','Rubric',chat_id=a)
    preset=store.save_preset(p,'Common rubric',[source]); store.apply_preset(p,b,preset)
    assert store.sources(p,chat_id=b)[0]['included']
    store.change_source(p,source,delete=True,chat_id=a)
    with pytest.raises(ValueError): store.apply_preset(p,b,preset)
    assert client.delete(base+f'/chats/{a}').status_code==400
    assert client.delete(base+f'/chats/{a}?confirmed=true').status_code==200
    assert client.get(base+f'/chats/{a}').status_code==404
