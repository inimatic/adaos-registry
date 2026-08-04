from __future__ import annotations

from pathlib import Path

from adaos.sdk.developer.conversational import compile_package


ROOT = Path(__file__).resolve().parents[1]


def test_builder_conversational_package_is_valid_and_executes_stories() -> None:
    result = compile_package(ROOT, kind="skill")

    assert result["valid"] is True
    metrics = result["validation_report"]["metrics"]
    assert metrics == {
        "affordances": 3,
        "entities": 0,
        "examples": 6,
        "intents": 3,
        "locales": 2,
        "matchers": 6,
        "outputs": 4,
        "repair_policies": 1,
        "stories": 2,
        "workflow_commands_referenced": 3,
        "workflow_transitions_referenced": 3,
    }
    reports = {
        item["story_id"]: item for item in result["validation_report"]["story_reports"]
    }
    assert reports["builder.prototype_cycle.ru.happy_path"]["valid"] is True
    assert reports["builder.prototype_cycle.ru.happy_path"]["final_state"] == "automation_ready"
    assert reports["builder.no_match.en.repair"]["valid"] is True
    assert reports["builder.no_match.en.repair"]["final_state"] == "ready"


def test_builder_conversational_static_report_covers_declared_control_surface() -> None:
    result = compile_package(ROOT, kind="skill")
    coverage = result["static_report"]["coverage"]

    assert coverage["output_kinds_covered_by_stories"] == ["accepted", "repair", "result"]
    assert coverage["repair_policies_missing_story_coverage"] == []
    assert coverage["locales_covered_by_stories"] == ["en", "ru"]
    assert coverage["channels_covered_by_stories"] == ["text", "web"]
