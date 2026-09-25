"""Hermetic SDK boundary doubles; these do not qualify Core/HTTP enforcement."""
import importlib.util
from pathlib import Path
import sqlite3
import socket
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec
import pytest
import yaml


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network is forbidden in hermetic tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def app(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('gmail_owned_handlers', root / 'handlers/main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    access = SimpleNamespace(require=Mock(), caller=Mock(return_value={'kind': 'user', 'id': 'synthetic'}),
        application=Mock(return_value={'application_id': 'gmail_cbs_cleanroom', 'subject_ref': 'user:synthetic'}))
    monkeypatch.setattr(module, 'access', access)
    class ProviderError(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__('NEVER_EXPOSE_REMOTE_DETAIL')
    provider = SimpleNamespace(GmailProviderError=ProviderError)
    for name in ('connection_status', 'begin_connection', 'reusable_connections',
                 'attach_reusable_connection', 'list_messages', 'list_message_summaries', 'get_message', 'list_labels',
                 'modify_message', 'trash_message', 'send_message'):
        setattr(provider, name, create_autospec(getattr(module.gmail, name), side_effect=AssertionError('Unconfigured provider double')))
    monkeypatch.setattr(module, 'gmail', provider)
    manifest = yaml.safe_load((root / 'skill.yaml').read_text(encoding='utf-8'))
    def initialize(path):
        db = sqlite3.connect(tmp_path / path)
        try:
            for migration in manifest['data_lifecycle']['databases'][0]['migrations']:
                for sql in migration['statements']:
                    db.execute(sql)
            db.commit()
        finally:
            db.close()
    monkeypatch.setattr(module, 'ensure_database', Mock(side_effect=initialize))
    monkeypatch.setattr(module, 'skill_data_root', lambda: tmp_path)
    module.test_manifest = manifest
    module.test_root = tmp_path
    return module


@pytest.fixture
def envelope():
    return lambda operation, result: {'ok': True, 'provider_id': 'google.gmail', 'operation': operation, 'result': result}


@pytest.fixture
def message():
    return {'id': 'm1', 'threadId': 't1', 'labelIds': ['INBOX', 'UNREAD'],
        'payload': {'mimeType': 'text/plain', 'headers': [{'name': 'Subject', 'value': 'Synthetic subject'}],
                    'body': {'data': 'U3ludGhldGljIGJvZHk'}}}
