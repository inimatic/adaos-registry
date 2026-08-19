from __future__ import annotations

from pathlib import Path

import jsonschema
import yaml


REGISTRY_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = REGISTRY_ROOT / "projects" / "media_center"
CORE_ROOT = Path(__file__).resolve().parents[4] / "adaos-media-center"


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
    assert manifest["components"]["dependencies"] == [
        {
            "ref": "skill:mediaserver",
            "version": ">=0.1.0",
            "lifecycle": "shared",
            "relations": ["uses"],
        }
    ]
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


def test_scenario_requires_every_project_runtime_component() -> None:
    scenario = yaml.safe_load(
        (REGISTRY_ROOT / "scenarios" / "media_center" / "scenario.yaml").read_text(encoding="utf-8")
    )
    assert set(scenario["runtime"]["skills"]["required"]) == {
        "media_center_skill",
        "media_library_agent",
        "media_control_skill",
        "mediaserver",
    }
    assert "media_indexer_skill" not in scenario["depends"]
