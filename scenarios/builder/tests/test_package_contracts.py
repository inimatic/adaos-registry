from pathlib import Path
import hashlib
import yaml


def test_companion_browser_reads_declare_bounded_causal_policies():
    root = Path(__file__).resolve().parents[3] / "skills"
    expected = {
        "builder_automation_skill": {"get_state"},
        "prompt_engineer_skill": {"prompt_list_dev_objects", "prompt_list_project_objects",
                                  "prompt_list_project_files", "prompt_read_project_file", "prompt_get_git_log"},
    }
    for skill, tools in expected.items():
        manifest = yaml.safe_load((root / skill / "skill.yaml").read_text(encoding="utf-8"))
        routes = manifest["data_routes"]
        assert {route["tool"] for route in routes} == tools
        for route in routes:
            policy = route["read_policy"]
            assert route["owner"] == "skill:" + skill
            assert policy["mode"] == "explicit"
            assert policy["max_request_hz"] == 1
            assert policy["preserve_last_value"] is True
            assert set(policy["triggers"]) == {"mount", "explicit_refresh", "state_dependency_changed", "targeted_invalidation"}
            assert policy["invalidation_tags"] == route["update_source"]

def test_profile_covers_skill_capabilities():
    root = Path(__file__).resolve().parents[3]
    skill = yaml.safe_load((root / "skills/builder_skill/skill.yaml").read_text(encoding="utf-8"))
    project = yaml.safe_load((root / "projects/adaos_builder/project.yaml").read_text(encoding="utf-8"))
    declared = {item["id"] for item in project["permission_profile"]["required"]}
    assert set(skill["capabilities"]) <= declared
    assert "application_permissions" not in project
    assert "application_roles" not in project  # No application-owned role matrix.



def test_companion_activation_matches_passive_tool_handlers():
    root = Path(__file__).resolve().parents[3]
    for name, count in [("builder_automation_skill", 3), ("prompt_engineer_skill", 27)]:
        package = root / "skills" / name
        manifest = yaml.safe_load((package / "skill.yaml").read_text(encoding="utf-8"))
        activation = manifest["runtime"]["activation"]
        assert activation["mode"] == "on_demand"
        assert activation["startup_allowed"] is False
        assert activation["background_refresh"] is False
        assessment = activation["assessment"]
        assert assessment["handler_digest"] == "sha256:" + hashlib.sha256((package / "handlers/main.py").read_bytes()).hexdigest()
        assert assessment["tool_count"] == count == len(manifest["tools"])
        assert assessment["classification"] == "tool_only"
        assert assessment["declared_subscriptions"] == assessment["observed_subscriptions"] == []
        assert assessment["dynamic_subscription_count"] == 0
        assert assessment["reasons"] == ["tool_invocation_can_import_handler_on_demand"]
        if name == "prompt_engineer_skill":
            assert next(tool for tool in manifest["tools"] if tool["name"] == "prompt_git_delete")["side_effects"] == "local_write"
