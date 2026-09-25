"""Core boundary delegation tests, not real authorization ingress evidence."""
import ast
from pathlib import Path
import pytest


CASES = [('connection_status', {}), ('begin_connection', {}), ('list_labels', {}), ('list_messages', {}),
         ('reusable_connections', {}), ('attach_reusable_connection', {}),
         ('get_message', {'id': 'm1'}), ('mutate_message', {'id': 'm1', 'action': 'archive'}),
         ('prepare_send', {}), ('send_message', {'command_id': 'x', 'to': 'x@example.test', 'subject': 'x', 'body': 'x'})]


@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('missing', ['caller', 'application'])
def test_missing_trusted_context_fails_closed(app, name, args, missing):
    getattr(app.access, missing).return_value = None
    with pytest.raises(PermissionError):
        getattr(app, name)(**args)
    app.ensure_database.assert_not_called()
    app.gmail.list_message_summaries.assert_not_called()
    for method in ('connection_status', 'begin_connection', 'reusable_connections', 'attach_reusable_connection', 'list_messages', 'get_message', 'list_labels', 'modify_message', 'trash_message', 'send_message'):
        getattr(app.gmail, method).assert_not_called()


@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('denied', ['workspace', 'providers.google.gmail'])
def test_denials_precede_io(app, name, args, denied):
    def require(permission):
        if permission.startswith(denied):
            raise PermissionError('synthetic_denial')
    app.access.require.side_effect = require
    with pytest.raises(PermissionError):
        getattr(app, name)(**args)
    app.ensure_database.assert_not_called()
    app.gmail.list_message_summaries.assert_not_called()
    for method in ('reusable_connections', 'attach_reusable_connection', 'send_message'):
        getattr(app.gmail, method).assert_not_called()


def test_declared_permissions_and_no_role_store(app):
    for entry in app.test_manifest['tools']:
        assert entry['application_access']['permission'] == 'providers.google.gmail'
        expected = 'workspace.read' if entry['side_effects'] == 'none' else 'workspace.write'
        assert expected in entry['permissions']
    sql = str(app.test_manifest['data_lifecycle'])
    assert 'CREATE TABLE IF NOT EXISTS send_commands' in sql
    for forbidden in ('CREATE TABLE roles', 'CREATE TABLE grants', 'CREATE TABLE users'):
        assert forbidden not in sql
    source = (Path(__file__).resolve().parents[1] / 'handlers/main.py').read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module.startswith('adaos.'):
            assert node.module.startswith('adaos.sdk')
