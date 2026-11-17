"""Hermetic trusted-boundary doubles; real Core/HTTP authorization is independently qualified."""
import ast
from pathlib import Path
import uuid

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]
CALLS=[('connection_status',{}),('begin_connection',{}),('list_messages',{}),('get_message',{'id':'msg-1'}),('list_labels',{}),('mutate_message',{'id':'msg-1','action':'trash'}),('prepare_send',{}),('send_message',{'command_id':str(uuid.uuid4()),'values':{'to':'a@example.test','subject':'Test','body':'Synthetic'}})]


@pytest.mark.parametrize('name,kwargs',CALLS)
@pytest.mark.parametrize('platform_role',['owner','member','child','guest'])
def test_all_roles_delegate_permission_denials_to_core(app,monkeypatch,name,kwargs,platform_role):
    # No package role defaults: even the owner fails when Core denies the grant.
    def deny(permission):
        raise PermissionError('trusted_boundary_denied')
    monkeypatch.setattr(app.access,'require',deny)
    with pytest.raises(PermissionError): getattr(app,name)(**kwargs)
    for op in ['connection_status','begin_connection','list_messages','get_message','list_labels','modify_message','trash_message','send_message']:
        getattr(app.gmail,op).assert_not_called()
    app.ensure_database.assert_not_called()


@pytest.mark.parametrize('name,kwargs',CALLS)
def test_missing_application_or_caller_fails_closed(app,monkeypatch,name,kwargs):
    monkeypatch.setattr(app.access,'application',lambda:None)
    with pytest.raises(PermissionError): getattr(app,name)(**kwargs)
    app.ensure_database.assert_not_called()
    app.gmail.send_message.assert_not_called()


@pytest.mark.parametrize('name,kwargs',CALLS)
def test_absent_caller_is_never_owner(app,monkeypatch,name,kwargs):
    monkeypatch.setattr(app.access,'caller',lambda:None)
    with pytest.raises(PermissionError): getattr(app,name)(**kwargs)
    app.ensure_database.assert_not_called()


@pytest.mark.parametrize('name,kwargs',CALLS)
def test_success_outputs_match_owned_schema(app,name,kwargs):
    import jsonschema
    manifest=yaml.safe_load((ROOT/'skill.yaml').read_text(encoding='utf-8'))
    declaration=next(t for t in manifest['tools'] if t['name']==name)
    value=getattr(app,name)(**kwargs)
    jsonschema.validate(value,declaration['output_schema'])
    expected='workspace.write' if name in ['begin_connection','mutate_message','prepare_send','send_message'] else 'workspace.read'
    assert [call.args[0] for call in app.access.require.call_args_list]==[expected,'providers.google.gmail']


@pytest.mark.parametrize('name,kwargs',CALLS)
def test_denied_provider_permission(app,monkeypatch,name,kwargs):
    def require(permission):
        if permission=='providers.google.gmail': raise PermissionError('provider_denied')
    monkeypatch.setattr(app.access,'require',require)
    with pytest.raises(PermissionError): getattr(app,name)(**kwargs)
    app.ensure_database.assert_not_called()


def test_manifest_exports_permissions_and_no_local_role_store(app):
    manifest=yaml.safe_load((ROOT/'skill.yaml').read_text(encoding='utf-8'))
    assert set(manifest['exports']['tools']) == {name for name,_ in CALLS}
    for declaration in manifest['tools']:
        assert callable(getattr(app,declaration['name']))
        assert declaration['application_access']['permission']=='providers.google.gmail'
        assert 'providers.google.gmail' in declaration['permissions']
    sql=' '.join(manifest['data_lifecycle']['databases'][0]['migrations'][0]['statements'])
    assert 'send_commands' in sql and 'role' not in sql and 'grant' not in sql
    source=(ROOT/'handlers/main.py').read_text(encoding='utf-8')
    tree=ast.parse(source)
    imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any(x.startswith('adaos.services') for x in imports)
    assert not any(x in source for x in ['requests','httpx','localStorage','sdk.data.secrets'])
