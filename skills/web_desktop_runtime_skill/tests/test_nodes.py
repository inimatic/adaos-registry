import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def nodes(monkeypatch):
    access = SimpleNamespace(require=Mock())
    system = SimpleNamespace(rename_current_node=Mock(return_value={
        "ok": True,
        "target": "current_node",
        "node_id": "studio",
        "display_name": "Studio",
        "current": "Studio",
        "desired": "Studio",
        "applied": True,
    }))
    sdk = ModuleType("adaos.sdk")
    sdk.access, sdk.system = access, system
    adaos = ModuleType("adaos")
    adaos.sdk = sdk
    monkeypatch.setitem(sys.modules, "adaos", adaos)
    monkeypatch.setitem(sys.modules, "adaos.sdk", sdk)
    path = Path(__file__).resolve().parents[1] / "handlers" / "nodes.py"
    spec = importlib.util.spec_from_file_location("tested_nodes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, access, system


def test_rename_selected_node_is_authorized_and_trimmed(nodes):
    module, access, system = nodes
    result = module.rename_selected_node("  Studio  ")
    access.require.assert_called_once_with("workspace.write")
    system.rename_current_node.assert_called_once_with("Studio")
    assert result["applied"] is True


def test_empty_name_never_mutates(nodes):
    module, access, system = nodes
    with pytest.raises(ValueError, match="display_name_required"):
        module.rename_selected_node("   ")
    access.require.assert_called_once_with("workspace.write")
    system.rename_current_node.assert_not_called()


def test_authorization_denial_never_mutates(nodes):
    module, access, system = nodes
    access.require.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError, match="denied"):
        module.rename_selected_node("Studio")
    system.rename_current_node.assert_not_called()


def test_sdk_failure_propagates_without_retry(nodes):
    module, access, system = nodes
    system.rename_current_node.side_effect = RuntimeError("target_member_unavailable")
    with pytest.raises(RuntimeError, match="target_member_unavailable"):
        module.rename_selected_node("Studio")
    access.require.assert_called_once_with("workspace.write")
    system.rename_current_node.assert_called_once_with("Studio")


def test_public_contract_declares_selected_member_routing():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "skill.yaml").read_text(encoding="utf-8"))
    declaration = next(t for t in manifest["tools"] if t["name"] == "rename_selected_node")
    assert declaration["entry"] == "handlers.main:rename_selected_node"
    assert declaration["side_effects"] == "local_write"
    assert declaration["permissions"] == ["workspace.write"]
    assert declaration["application_access"] == {
        "permission": "workspace.write", "capability": "desktop.settings.write",
    }
    assert declaration["input_schema"]["required"] == ["target_node_id", "display_name"]
    assert "rename_selected_node" in manifest["exports"]["tools"]
