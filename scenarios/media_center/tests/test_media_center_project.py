from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import yaml


REGISTRY_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = REGISTRY_ROOT / "projects" / "media_center"


def _core_root() -> Path:
    configured = os.environ.get("ADAOS_CORE_ROOT", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(Path(__file__).resolve().parents)
    candidates.append(Path(__file__).resolve().parents[4] / "adaos-media-center")
    for candidate in candidates:
        if (candidate / "src" / "adaos" / "abi" / "project.v1.schema.json").is_file():
            return candidate
    return candidates[-1]


CORE_ROOT = _core_root()


def test_media_center_project_is_a_complete_distribution_boundary() -> None:
    manifest = yaml.safe_load((PROJECT_ROOT / "project.yaml").read_text(encoding="utf-8"))
    schema_path = CORE_ROOT / "src" / "adaos" / "abi" / "project.v1.schema.json"
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(manifest)

    owned = {item["ref"]: item for item in manifest["components"]["owned"]}
    assert set(owned) == {
        "scenario:media_center",
        "skill:media_center_skill",
        "skill:media_library_agent",
        "skill:media_control_skill",
    }
    assert owned["scenario:media_center"]["role"] == "primary"
    assert owned["skill:media_library_agent"]["exposure"] == "project_only"
    assert manifest["components"]["dependencies"] == []
    assert all(
        "skill:mediaserver" not in item.get("components", [])
        for item in manifest["install"]["features"]
    )
    assert {item["id"] for item in manifest["entrypoints"]} == {
        "library",
        "tv",
        "remote",
        "embedded",
    }
    bindings = {item["id"]: item["bindings"] for item in manifest["entrypoints"]}
    assert bindings["tv"]["presentation_profile"] == "tv"
    assert bindings["tv"]["shared_surface"] is True
    assert bindings["remote"]["presentation_profile"] == "mobile_control"
    assert manifest["lifecycle"]["uninstall"]["runtime_data"] == "retain"
    assert manifest["lifecycle"]["uninstall"]["source_artifacts"] == "retain"


def test_scenario_requires_only_project_owned_runtime_components() -> None:
    scenario = yaml.safe_load(
        (REGISTRY_ROOT / "scenarios" / "media_center" / "scenario.yaml").read_text(encoding="utf-8")
    )
    assert set(scenario["runtime"]["skills"]["required"]) == {
        "media_center_skill",
        "media_library_agent",
        "media_control_skill",
    }
    assert "mediaserver" not in scenario["depends"]
    assert "media_indexer_skill" not in scenario["depends"]


def test_registry_index_matches_media_center_distribution_manifests() -> None:
    registry = json.loads(
        (REGISTRY_ROOT / "registry.json").read_text(encoding="utf-8")
    )
    skills = {item["id"]: item for item in registry["skills"]}
    scenarios = {item["id"]: item for item in registry["scenarios"]}

    for skill_id in (
        "media_center_skill",
        "media_library_agent",
        "media_control_skill",
    ):
        manifest = yaml.safe_load(
            (REGISTRY_ROOT / "skills" / skill_id / "skill.yaml").read_text(
                encoding="utf-8"
            )
        )
        entry = skills[skill_id]
        assert entry["version"] == manifest["version"]
        assert entry["description"] == manifest["description"]
        assert entry["tools_count"] == len(manifest["tools"])

    scenario = yaml.safe_load(
        (REGISTRY_ROOT / "scenarios" / "media_center" / "scenario.yaml").read_text(
            encoding="utf-8"
        )
    )
    entry = scenarios["media_center"]
    assert entry["version"] == scenario["version"]
    assert entry["description"] == scenario["description"]
    assert set(entry["skills"]["required"]) == set(
        scenario["runtime"]["skills"]["required"]
    )
    assert "optional" not in entry["skills"]
