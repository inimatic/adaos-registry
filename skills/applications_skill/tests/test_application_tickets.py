import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest


@pytest.fixture
def adapter():
    path = Path(__file__).parents[1] / 'handlers' / 'main.py'
    source = ast.parse(path.read_text(encoding='utf-8'))
    names = {'list_dashboard_positions', '_ticket_scope', '_scoped_ticket', '_ticket_actor', '_get_scoped_ticket', '_bounded_ticket_projection',
             'list_application_tickets', 'show_application_ticket',
             'create_application_ticket', 'comment_application_ticket'}
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    env = {'Any': Any, 'Mapping': Mapping, 'json': json, 'tool': lambda **_: lambda f: f}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), env)
    grants = []
    env['access'] = SimpleNamespace(require=grants.append, caller=lambda: {'id': 'user:owner'})
    env['applications'] = SimpleNamespace(
        get_application=lambda app: {'application': {'application_id': app, 'legacy_project_id': 'project:one'}, 'component_inventory': [{'component_ref': 'scenario:one', 'ownership': 'owned'}, {'component_ref': 'skill:shared', 'ownership': 'shared'}]},
        list_application_components=lambda app: [
            {'component_ref': 'scenario:one', 'ownership': 'owned'},
            {'component_ref': 'skill:shared', 'ownership': 'shared'},
        ])
    env['grants'] = grants
    return env


def ticket(ticket_id, kind, identifier, component, revision=3):
    return {'ticket_id': ticket_id, 'target_scope': {'type': kind, 'id': identifier},
            'component_ref': component, 'revision': revision}


def test_bounded_scopes_deduplicate_and_exclude_foreign_and_shared(adapter):
    own = ticket('own', 'application', 'one', 'application:one')
    legacy = ticket('legacy', 'scenario', 'one', 'scenario:one')
    foreign = ticket('foreign', 'application', 'other', 'scenario:one')
    shared = ticket('shared', 'skill', 'shared', 'skill:shared')
    calls = []
    def list_tickets(**kwargs):
        calls.append(kwargs)
        return [own, legacy, foreign, shared]
    adapter['development_tickets'] = SimpleNamespace(list_tickets=list_tickets)
    result = adapter['list_application_tickets']('one', 'find', 'in_progress')
    assert {x['ticket_id'] for x in result['items']} == {'own', 'legacy'}
    assert calls and all(c.get('target_id') or c.get('component_ref') for c in calls)
    assert all(c['limit'] == 100 and c['search'] == 'find' for c in calls)
    assert not any(c.get('component_ref') == 'skill:shared' for c in calls)
    assert adapter['grants'] == ['development.read.tickets', 'applications.read']


def test_detail_and_comment_reject_foreign_or_stale_without_write(adapter):
    writes = []
    foreign = ticket('x', 'application', 'other', 'application:other')
    sdk = SimpleNamespace(get_ticket=lambda _: foreign, list_events=lambda **_: [],
                          operate_ticket=lambda *a, **k: writes.append((a, k)))
    adapter['development_tickets'] = sdk
    with pytest.raises(ValueError):
        adapter['show_application_ticket']('one', 'x')
    with pytest.raises(ValueError):
        adapter['comment_application_ticket']('one', 'x', 'draft', 3)
    sdk.get_ticket = lambda _: ticket('x', 'scenario', 'one', 'scenario:one', 4)
    with pytest.raises(ValueError):
        adapter['comment_application_ticket']('one', 'x', 'draft', 3)
    assert not writes


def test_create_and_comment_require_real_acknowledgements(adapter):
    sdk = SimpleNamespace(create_ticket=lambda *a, **k: {'ok': False},
                          get_ticket=lambda _: ticket('x', 'project', 'one', 'project:one'),
                          operate_ticket=lambda *a, **k: {'ticket': {'ticket_id': 'x', 'revision': 3}})
    adapter['development_tickets'] = sdk
    with pytest.raises(ValueError):
        adapter['create_application_ticket']('one', 'draft')
    with pytest.raises(ValueError):
        adapter['comment_application_ticket']('one', 'x', 'draft', 3)
    sdk.operate_ticket = lambda *a, **k: {'ticket': {'ticket_id': 'x', 'revision': 4}}
    assert adapter['comment_application_ticket']('one', 'x', 'draft', 3)['revision'] == 4
    adapter['access'].caller = lambda: None
    with pytest.raises(PermissionError):
        adapter['create_application_ticket']('one', 'draft')


def test_writes_use_authenticated_actor_and_exact_observed_revision(adapter):
    calls = []
    current = ticket('x', 'project', 'one', 'project:one')
    def create(summary, **kwargs):
        calls.append(('create', summary, kwargs))
        return {'ok': True, 'ticket': current, 'ticket_duplicate': True}
    def operate(ticket_id, operation, **kwargs):
        calls.append(('operate', ticket_id, operation, kwargs))
        return {'ticket': dict(current, revision=4)}
    adapter['development_tickets'] = SimpleNamespace(create_ticket=create,
        get_ticket=lambda _: current, operate_ticket=operate)
    assert adapter['create_application_ticket']('one', 'request')['duplicate'] is True
    assert calls[0][2]['target_scope'] == {'type': 'project', 'id': 'one'}
    assert calls[0][2]['actor'] == 'user:owner'
    adapter['comment_application_ticket']('one', 'x', 'draft', 3)
    assert calls[1] == ('operate', 'x', 'comment', {'actor': 'user:owner', 'payload': {'body': 'draft'}, 'expected_revision': 3})


def test_failed_sdk_write_propagates_without_retry(adapter):
    calls = []
    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError('revision conflict')
    adapter['development_tickets'] = SimpleNamespace(
        get_ticket=lambda _: ticket('x', 'project', 'one', 'project:one'), operate_ticket=fail)
    with pytest.raises(RuntimeError, match='revision conflict'):
        adapter['comment_application_ticket']('one', 'x', 'draft', 3)
    assert len(calls) == 1


def test_dashboard_positions_preserve_core_rows_and_fail_closed(adapter):
    positions = [{'id': 'private_alpha'}, {'id': 'public_stable'}]
    adapter['applications'].get_application = lambda app: {'application': {'application_id': app}, 'dashboard': {'positions': positions}}
    assert adapter['list_dashboard_positions']('one') == {'items': positions}
    assert adapter['grants'] == ['applications.read']
    adapter['applications'].get_application = lambda app: {'application': {'application_id': app}}
    with pytest.raises(ValueError):
        adapter['list_dashboard_positions']('one')
    adapter['applications'].get_application = lambda app: {'application': {'application_id': 'foreign'}, 'dashboard': {'positions': positions}}
    with pytest.raises(ValueError):
        adapter['list_dashboard_positions']('one')


def test_owned_skill_declares_empty_local_lifecycle():
    import yaml
    manifest = yaml.safe_load((Path(__file__).parents[1] / 'skill.yaml').read_text(encoding='utf-8'))
    lifecycle = manifest['data_lifecycle']
    assert lifecycle['schema'] == 'adaos.skill.data_lifecycle.v1'
    assert lifecycle['execution'] == 'native_tools'
    for field in ('databases', 'state_files', 'coordination_files', 'operational_evidence_files', 'reconstructible_directories'):
        assert lifecycle[field] == []


def test_history_resolves_comment_references_from_the_exact_scoped_ticket(adapter):
    own = ticket('x', 'project', 'one', 'project:one')
    own['comments'] = [{'id': 'comment.own', 'body': 'Canonical discussion body'}]
    adapter['development_tickets'] = SimpleNamespace(get_ticket=lambda _: own, list_events=lambda **_: [
        {'ticket_id': 'foreign', 'event_id': 'foreign', 'payload': {'kind': 'commented', 'comment_id': 'comment.own'}},
        {'ticket_id': 'x', 'event_id': 'own', 'payload': {'kind': 'commented', 'comment_id': 'comment.own'}, 'recorded_at': '2026-11-03'},
    ])
    result = adapter['show_application_ticket']('one', 'x')
    assert result['events'] == [{'event_id': 'own', 'kind': 'commented', 'body': 'Canonical discussion body', 'recorded_at': '2026-11-03'}]
