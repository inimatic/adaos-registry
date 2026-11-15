"""New maintenance requirements against the real persistent Core service."""
import importlib.util
from pathlib import Path

import pytest

from adaos.sdk import maintenance as sdk
from adaos.services.node_maintenance import NodeMaintenanceService


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    service = NodeMaintenanceService(workspace=tmp_path / 'workspace',
        base=tmp_path / 'runtime', node_id='owner-node')
    monkeypatch.setattr(sdk, '_authorize', lambda write=False: None)
    monkeypatch.setattr(sdk, 'from_context', lambda: service)
    path = Path(__file__).resolve().parents[1] / 'handlers/maintenance.py'
    spec = importlib.util.spec_from_file_location('management_maintenance', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, service


def test_review_has_no_storage_effect_and_unknown_size_stays_unknown(maintenance, tmp_path):
    handler, service = maintenance
    destination = tmp_path / 'separate-storage/evidence'
    before = service.locations.settings()
    review = handler.plan_location('owner-node', role='e2e_evidence',
        desired_path=str(destination), expected_revision=0, min_free_bytes=0)
    assert review['target_node_id'] == 'owner-node'
    assert review['moves_data'] is review['switches_writers'] is False
    assert service.locations.settings() == before
    assert not destination.exists()
    row = next(row for row in handler.snapshot('storage', 'owner-node')['locations']
               if row['id'] == 'e2e_evidence')
    assert row['measured_bytes'] is None
    assert row['measured_display'] == 'Not measured'


def test_successive_saves_reopen_and_exact_acknowledgement(maintenance, tmp_path):
    handler, service = maintenance
    first = handler.plan_location('owner-node', role='e2e_evidence',
        desired_path=str(tmp_path / 'first'), expected_revision=0, min_free_bytes=0)
    receipt = handler.apply_location('owner-node', first['plan_digest'])
    assert receipt['revision'] == 1
    second = handler.plan_location('owner-node', role='e2e_evidence',
        desired_path=str(tmp_path / 'second'), expected_revision=receipt['revision'], min_free_bytes=0)
    assert handler.apply_location('owner-node', second['plan_digest'])['revision'] == 2
    replay = handler.apply_location('owner-node', first['plan_digest'])
    assert replay['replayed'] and replay['revision'] == 1
    reopened = NodeMaintenanceService(workspace=service.locations.workspace,
        base=service.locations.base, node_id='owner-node')
    row = next(row for row in reopened.snapshot()['locations'] if row['id'] == 'e2e_evidence')
    assert row['desired_path'] == str(tmp_path / 'second')
    assert row['active_path'] == first['active_path']
    assert reopened.locations.settings()['revision'] == 2


def test_outdated_editor_cannot_replace_acknowledged_setting(maintenance, tmp_path):
    handler, service = maintenance
    old = handler.plan_location('owner-node', role='e2e_evidence',
        desired_path=str(tmp_path / 'old-editor'), expected_revision=0, min_free_bytes=0)
    current = handler.plan_location('owner-node', role='e2e_evidence',
        desired_path=str(tmp_path / 'current-editor'), expected_revision=0, min_free_bytes=0)
    handler.apply_location('owner-node', current['plan_digest'])
    with pytest.raises(ValueError, match='stale'):
        handler.apply_location('owner-node', old['plan_digest'])
    assert service.locations.settings()['roles']['e2e_evidence']['desired_path'] == str(tmp_path / 'current-editor')
