from __future__ import annotations

from pathlib import Path

import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _tools() -> dict[str, dict]:
    manifest = yaml.safe_load(
        (SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8")
    )
    return {item["name"]: item for item in manifest["tools"]}


def test_learning_lab_exports_publish_exact_application_access() -> None:
    tools = _tools()
    expected = {
        "get_learning_lab": (
            ["workspace.read"],
            "workspace.read",
            "companion.learning.read",
            "read_only",
        ),
        "learning_lab_action": (
            ["workspace.write"],
            "workspace.write",
            "companion.learning.review",
            "local_write",
        ),
        "get_companion_context": (
            ["workspace.read", "external_provider.use"],
            "workspace.read",
            "companion.context.read",
            "read_only",
        ),
        "list_companion_activity": (
            ["workspace.read", "external_provider.use"],
            "workspace.read",
            "companion.activity.read",
            "read_only",
        ),
        "execute_companion_action": (
            ["workspace.write", "external_provider.use"],
            "workspace.write",
            "companion.action.execute",
            "external_io",
        ),
        "capture_capability_request": (
            ["workspace.write", "external_provider.use"],
            "workspace.write",
            "companion.capability_request.capture",
            "external_io",
        ),
    }

    for tool_name, (permissions, permission, capability, side_effects) in expected.items():
        tool = tools[tool_name]
        assert tool["permissions"] == permissions
        assert tool["application_access"] == {
            "permission": permission,
            "capability": capability,
        }
        assert tool["side_effects"] == side_effects


def test_access_contract_has_no_skill_local_role_store() -> None:
    source = (SKILL_ROOT / "handlers" / "main.py").read_text(encoding="utf-8")
    assert "application_roles" not in source
    assert "permission_profile" not in source
