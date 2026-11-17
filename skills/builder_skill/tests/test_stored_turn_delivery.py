"""Timeout after commit must not lose the user's admitted outbound turn."""
import ast
from pathlib import Path
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from functools import partial
import pytest


def projection(monkeypatch, *, appended=False, persisted=True):
    path=Path(__file__).resolve().parents[1]/'handlers/main.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'_builder_publication_context','_project_external_user_turn'}]
    reads=[];published=[];lookups=[]
    def receipt(identity,**scope):
        reads.append((identity,scope));return {'ok':True,'persisted':persisted}
    import adaos.sdk.io.out as io_out
    monkeypatch.setattr(io_out,'chat_message_receipt',receipt)
    def communication(**kw):
        lookups.append(kw)
        raise AssertionError('A captured project destination must not follow another current tab')
    sdk=SimpleNamespace(communication_context=communication,publish=lambda **kw:published.append(kw) or {'status':'committed'})
    env={'Any':Any,'Mapping':Mapping,'DIALOG_CHANNEL_ID':'builder','sdk_conversation':sdk,
         '_LOG':SimpleNamespace(warning=lambda *a,**kw:None),
         '_is_api_tool_call':lambda meta:True,'_safe_emit_chat':lambda *a,**kw:appended,
         '_external_user_turn_message_id':lambda text,meta:'stored-exact-turn'}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),env)
    return partial(env['_project_external_user_turn'],_meta=None,binding=None,topic_ref=None),reads,published,lookups


def binding(owner='skill:builder_skill'):
    return {'communication_binding':{'status':'ready','owner':owner,'conversation_id':'owned-builder',
                                    'thread_id':'prompt-project:scenario:retouch'}}


def test_timeout_after_commit_attaches_delivery_to_exact_captured_turn(monkeypatch):
    project,reads,published,lookups=projection(monkeypatch)
    assert project(' exact brief ',webspace_id='desktop',session=binding(),_meta={'conversation_id':'foreign-current-tab'})=='stored-exact-turn'
    assert reads==[('stored-exact-turn',{'conversation_id':'owned-builder','thread_id':'prompt-project:scenario:retouch',
                  'webspace_id':'desktop','channel_id':'builder','owner':'skill:builder_skill','role':'user','text':'exact brief'})]
    assert published==[{'conversation_id':'owned-builder','thread_id':'prompt-project:scenario:retouch',
                       'text':'exact brief','publication_key':'builder-stored:stored-exact-turn','existing_message_id':'stored-exact-turn'}]
    assert not lookups


def test_uncommitted_or_wrong_scoped_receipt_never_publishes(monkeypatch):
    project,reads,published,lookups=projection(monkeypatch,persisted=False)
    with pytest.raises(RuntimeError,match='not_acknowledged'):
        project('brief',webspace_id='desktop',session=binding())
    assert reads and not published and not lookups


@pytest.mark.parametrize('session',[{},binding('core:general_assistant')])
def test_missing_or_foreign_binding_never_falls_back_to_active_tab(monkeypatch,session):
    project,reads,published,lookups=projection(monkeypatch)
    with pytest.raises(RuntimeError,match='not_acknowledged'):
        project('brief',webspace_id='desktop',session=session)
    assert not reads and not published and not lookups


def test_normal_ack_uses_same_core_deduplication_key(monkeypatch):
    project,reads,published,_=projection(monkeypatch,appended=True)
    project('brief',webspace_id='desktop',session=binding())
    project('brief',webspace_id='desktop',session=binding())
    assert not reads and len(published)==2
    assert published[0]==published[1]
