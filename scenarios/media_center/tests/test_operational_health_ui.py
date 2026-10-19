"""Product contracts for independent operational dimensions and bounded reads."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[3]


def dictionaries(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from dictionaries(value)
    elif isinstance(node, list):
        for value in node:
            yield from dictionaries(value)


def health_widget():
    ui = json.loads((ROOT / 'scenarios/media_center/webui.json').read_text(encoding='utf-8'))
    return next(value for value in dictionaries(ui) if value.get('id') == 'media-center-summary')


def projection(**kwargs):
    path = ROOT / 'skills/media_center_skill/media_center/health.py'
    spec = importlib.util.spec_from_file_location('media_center_health_ui_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.health_projection(**kwargs)


def test_status_preserves_widget_identity_and_separates_six_dimensions():
    widget = health_widget()
    assert widget['type'] == 'item.details'
    fields = widget['inputs']['fields']
    assert len({field['id'] for field in fields}) == len(fields)
    assert {'health.liveness.state', 'health.readiness.state', 'health.ingest.state',
            'health.storage.state', 'health.endpoint.state', 'health.history.state',
            'health.history.failed_jobs', 'health.ingest.last_success_at'} <= {field['path'] for field in fields}
    assert 'colorPath' not in widget['inputs']
    assert widget['inputs']['showReadState'] is True
    assert widget['inputs']['readStatePath'] == 'state'
    assert not widget.get('actions')
    assert widget['dataSource']['name'] == 'media_center_skill.status'
    assert widget['dataSource']['responseStatePath'] == 'state'
    assert widget['dataSource']['preserveLastValue'] is True
    assert widget['dataSource']['maxRequestHz'] == 1
    assert widget['dataSource']['cacheTtlMs'] == 1000


def test_cold_read_contract_is_explicit_for_catalog_and_detail_sources():
    ui = json.loads((ROOT / 'scenarios/media_center/webui.json').read_text(encoding='utf-8'))
    critical = {'library', 'list_collections', 'collection_contents', 'browse_folders',
                'list_playlists', 'get_item', 'metadata_facets', 'status', 'list_roots'}
    sources = [value['dataSource'] for value in dictionaries(ui) if isinstance(value.get('dataSource'), dict)
               and value['dataSource'].get('kind') == 'skill'
               and value['dataSource'].get('name', '').startswith('media_center_skill.')
               and value['dataSource']['name'].split('.', 1)[1] in critical]
    assert {source['name'].split('.', 1)[1] for source in sources} == critical
    manifest = yaml.safe_load((ROOT / 'skills/media_center_skill/skill.yaml').read_text(encoding='utf-8'))
    policies = {route['tool']: route['read_policy'] for route in manifest['data_routes'] if route.get('tool')}
    for source in sources:
        assert source['responseStatePath'] == 'state'
        assert source['preserveLastValue'] is policies[source['name'].split('.', 1)[1]]['preserve_last_value']
        assert source['maxRequestHz'] == policies[source['name'].split('.', 1)[1]]['max_request_hz']
        assert not any(key in source for key in ('pollIntervalMs', 'refreshIntervalMs'))


def test_ready_empty_catalog_is_independent_of_stopped_ingest_and_past_failures():
    health = projection(read_ready=True, sync={'running': False}, job_counts={'failed': 7})
    assert health['readiness'] == {'state': 'ready', 'ready': True}
    assert health['ingest']['state'] == 'stopped'
    assert health['history']['state'] == 'retained_failures'
    assert health['history']['failed_jobs'] == 7
    assert health['history']['gates_readiness'] is False
    assert health['storage']['state'] == health['endpoint']['state'] == 'not_observed'


def test_freshness_and_unknown_values_are_localized_without_becoming_health_claims():
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    for sync, expected in (({}, 'not_observed'),
        ({'running': True, 'state': 'retry_wait', 'consecutive_failures': 1}, 'retry_wait'),
        ({'running': True, 'last_success_at': '2026-10-18T00:00:00+00:00'}, 'stale'),
        ({'running': True, 'last_success_at': now.isoformat(), 'state': 'waiting'}, 'current')):
        health = projection(read_ready=True, sync=sync, now=now)
        assert health['ingest']['state'] == expected
        assert health['readiness']['ready'] is True
    english, russian = [json.loads((ROOT / f'skills/media_center_skill/assets/i18n/{locale}.json').read_text(encoding='utf-8'))
                        for locale in ('en', 'ru')]
    keys = {key for key in english if key.startswith('runtime.media_center.ui.health_')}
    assert keys and keys == {key for key in russian if key.startswith('runtime.media_center.ui.health_')}
    for key in keys:
        assert english[key] and russian[key]
    assert english['runtime.media_center.ui.health_state_not_observed'] == 'Not checked'
    assert russian['runtime.media_center.ui.health_state_not_observed'] == 'Не проверено'
