"""Hermetic checks of delegation to the public Core access boundary.

The denying SDK double does not qualify HTTP ingress or installed grants.
"""
import importlib.util
import inspect
from pathlib import Path
import sys

import pytest
import yaml
from adaos.sdk import access

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ("media_center_skill", "media_library_agent", "media_control_skill")


def load_handlers(skill, entry):
    root = ROOT / "skills" / skill
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module, _ = entry.split(":")
    spec = importlib.util.spec_from_file_location(
        "access_contract_" + skill + module.replace(".", "_"),
        root / (module.replace(".", "/") + ".py"),
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize("skill", SKILLS)
@pytest.mark.parametrize("denial", ["Core denied the invocation",
                                     "preview_data_profile_read_denied",
                                     "preview_data_profile_readonly"])
def test_every_export_denies_before_domain_work(skill, monkeypatch, denial):
    manifest = yaml.safe_load((ROOT / "skills" / skill / "skill.yaml").read_text(encoding="utf-8"))
    calls = []
    def deny(permission):
        calls.append(permission)
        raise PermissionError(denial)
    monkeypatch.setattr(access, "require", deny)
    modules = {}
    tools = {tool["name"]: tool for tool in manifest["tools"]}
    for name in manifest["exports"]["tools"]:
        definition = tools[name]
        if denial == "preview_data_profile_readonly" and definition["side_effects"] in ("none", "read_only"):
            continue
        permission = "workspace.read" if definition["side_effects"] in ("none", "read_only") else "workspace.write"
        assert definition["permissions"] == [permission]
        assert definition["application_access"]["permission"] == permission
        entry = definition["entry"]
        module = modules.setdefault(entry.split(":")[0], None)
        if module is None:
            module = modules[entry.split(":")[0]] = load_handlers(skill, entry)
        handler = getattr(module, entry.split(":")[1])
        kwargs = {key: None for key, value in inspect.signature(handler).parameters.items()
                  if value.default is inspect.Parameter.empty and value.kind in
                  (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
        calls.clear()
        with pytest.raises(PermissionError, match=denial):
            handler(**kwargs)
        assert calls == [permission], name


def test_permission_profile_covers_all_owned_capabilities_without_local_roles():
    project = yaml.safe_load((ROOT / "projects/media_center/project.yaml").read_text(encoding="utf-8"))
    assert "application_permissions" not in project
    assert not project.get("application_roles")
    profile = project["permission_profile"]
    declared = {item["id"] for section in ("required", "optional") for item in profile[section]}
    for skill in SKILLS:
        manifest = yaml.safe_load((ROOT / "skills" / skill / "skill.yaml").read_text(encoding="utf-8"))
        assert set(manifest["capabilities"]) <= declared
        # Domain authorization stays with the trusted Core grant check. No
        # application role capability or caller-supplied identity bypasses it.
        assert all(set(t["application_access"]) == {"permission"} for t in manifest["tools"])


def test_manifest_preview_profiles():
    project = yaml.safe_load((ROOT / "projects/media_center/project.yaml").read_text(encoding="utf-8"))
    declaration = project["development"]["preview_data"]
    assert declaration["default_profile_id"] == "demo-fixture"
    items = declaration["profiles"]
    assert [p["profile_id"] for p in items] == [
        "demo-fixture", "isolated-sandbox", "local-library-readonly"]
    assert [(p["mode"], p["source_ref"], p["read_policy"], p["write_policy"]) for p in items] == [
        ("fixture", "fixture:media_center:demo-v1", "fixture", "none"),
        ("sandbox", "sandbox:media_center:local", "sandbox", "sandbox"),
        ("live_readonly", "skill-data:media_center_skill:workspace", "scoped_live", "none")]
    for p in items:
        assert p["schema"] == "adaos.builder.binding_profile.v1"
        assert p["capabilities"] == ["media.catalog"]
        assert p["owner"] == "project:media_center"
        assert "owner_ref" not in p
        assert p["logical_schema_ref"] == "adaos.media.catalog.v1"
    assert items[0]["implementation_mappings"] == [{"logical_ref": "media.catalog",
        "implementation_ref": "skill:media_center_skill:fixture", "status": "fixture_only"}]
    assert (items[2]["sensitivity"], items[2]["redaction"]) == ("personal", "required")

