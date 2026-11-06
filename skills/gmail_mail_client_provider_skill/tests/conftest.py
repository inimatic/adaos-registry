import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contract():
    return json.loads((ROOT / 'tests/gmail_contract.json').read_text(encoding='utf-8'))


@pytest.fixture
def app(monkeypatch, tmp_path, contract):
    spec = importlib.util.spec_from_file_location('gmail_candidate', ROOT / 'handlers/main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.access, 'require', Mock())
    monkeypatch.setattr(module.access, 'application', lambda: {'application_id': 'synthetic-app', 'subject_ref': 'user:test'})
    monkeypatch.setattr(module.access, 'caller', lambda: {'kind': 'user', 'id': 'test'})
    monkeypatch.setattr(module, 'skill_data_root', lambda: tmp_path)
    manifest = yaml.safe_load((ROOT / 'skill.yaml').read_text(encoding='utf-8'))

    def initialize(path):
        # Test double: executes declared SQL, but does NOT qualify Core's ledger or cutover.
        with closing(sqlite3.connect(tmp_path / path)) as db, db:
            for migration in manifest['data_lifecycle']['databases'][0]['migrations']:
                for sql in migration['statements']:
                    db.execute(sql)

    monkeypatch.setattr(module, 'ensure_database', Mock(side_effect=initialize))
    examples = contract['synthetic_examples']
    for name in ['connection_status', 'begin_connection', 'list_messages', 'get_message', 'list_labels', 'modify_message', 'trash_message', 'send_message']:
        key = 'send_confirmed' if name == 'send_message' else name
        value = examples.get(key, {'ok': True, 'authorization_url': 'https://accounts.google.com/synthetic', 'expires_at': '2030-01-01T00:00:00Z'})
        monkeypatch.setattr(module.gmail, name, Mock(side_effect=lambda *a, _value=value, **k: copy.deepcopy(_value)))
    return module
