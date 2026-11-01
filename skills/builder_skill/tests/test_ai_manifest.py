"""The installed personal transport must expose observation as well as writes."""
from pathlib import Path

import yaml
import pytest


def test_personal_builder_declares_inference_and_project_observation():
    source = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((source / 'skill.yaml').read_text(encoding='utf-8'))
    assert {'ai.inference', 'workspace.read', 'workspace.write'} <= set(manifest['capabilities'])
    assert {'get_session', 'prototype_chatgpt_edit'} <= set(manifest['exports']['tools'])


@pytest.mark.parametrize('primary', ['scenario:primary', 'scenario:unrelated', 'skill:implementation'])
def test_sdk_selected_project_pins_session_resource_namespace(tmp_path, monkeypatch, primary):
    from test_builder_skill import _load_module
    module = _load_module()
    monkeypatch.setattr(module.developer_compositions, 'get', lambda _: {
        'components': {'owned': [{'role': 'primary', 'ref': primary}]}})
    monkeypatch.setattr(module, '_project_artifact_root', lambda _: tmp_path)
    monkeypatch.setattr(module, '_ensure_session_artifact_root', lambda *_: False)
    session = {'scenario_id': 'primary'}
    binding = {'selection': {'object_type': 'project', 'object_id': 'owner'}}
    if primary == 'scenario:unrelated':
        with pytest.raises(ValueError, match='different primary components'):
            module._sync_session_from_artifacts(session, binding)
        assert 'project_ref' not in session
    elif primary == 'scenario:primary':
        assert module._sync_session_from_artifacts(session, binding)
        assert session['project_ref'] == 'project:owner'
        session['project_ref'] = 'project:another'
        with pytest.raises(ValueError, match='owning Project'):
            module._sync_session_from_artifacts(session, binding)
        monkeypatch.setattr(module, '_ensure_session_artifact_root', lambda *_: True)
        monkeypatch.setattr(module, '_save_session', lambda *_: None)
        fresh = module._session_from_binding('host', {**binding, 'runtime_scenario_id': 'primary'})
        assert fresh['project_ref'] == 'project:owner'
    else:
        assert not module._sync_session_from_artifacts(session, binding)
        assert 'project_ref' not in session
