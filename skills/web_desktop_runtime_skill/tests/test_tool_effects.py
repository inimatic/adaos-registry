"""Public effects must describe the least-privileged intent of each tool."""

from pathlib import Path

import yaml


def test_public_tool_effects():
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "skill.yaml").read_text(encoding="utf-8")
    )
    expected = {
        "web_desktop_runtime_drain": "runtime_write",
        "web_desktop_runtime_dispose": "runtime_write",
        "web_desktop_runtime_rehydrate": "runtime_write",
        "get_current_builder_application": "read_only",
        "get_subscription_usage": "read_only",
        "list_applications": "read_only",
        "list_home_widgets": "read_only",
        "list_devices": "read_only",
        "rename_device": "local_write",
        "get_system_overview": "read_only",
        "get_runtime_controls": "read_only",
        "list_developments": "read_only",
        "get_preferences": "read_only",
        "read_profile_avatar": "read_only",
        # Inventory parent metadata is local state, not a hardware command.
        "assign_device_endpoint": "local_write",
        "identify_device": "read_only",
        "update_preferences": "local_write",
        "upload_profile_avatar": "local_write",
        "update_home_item": "local_write",
        "request_core_update": "runtime_write",
        "set_core_autoupdate": "runtime_write",
        "rename_selected_node": "local_write",
        "rename_subnet": "local_write",
        "set_runtime_control": "runtime_write",
        "prepare_connection": "external_write",
        "unlink_node": "external_write",
    }
    declarations = manifest["tools"]
    assert len(declarations) == len(expected)
    assert {tool["name"]: tool["side_effects"] for tool in declarations} == expected
    assert set(manifest["exports"]["tools"]) == set(expected)
    assert manifest["lifecycle"] == {
        "drain": "web_desktop_runtime_drain",
        "rehydrate": "web_desktop_runtime_rehydrate",
        "dispose": "web_desktop_runtime_dispose",
    }
