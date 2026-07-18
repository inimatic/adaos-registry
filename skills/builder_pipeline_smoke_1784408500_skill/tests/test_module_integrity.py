from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from adaos.services.skill.validation import SkillValidationService


def test_skill_entrypoint_imports() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((skill_root / "skill.yaml").read_text(encoding="utf-8")) or {}
    entry = manifest.get("entry") or manifest.get("entrypoint") or "handlers/main.py"
    entry_path = skill_root / str(entry)

    spec = importlib.util.spec_from_file_location("skill_under_test.handlers.main", entry_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert entry_path.is_file()


def test_manifest_exports_only_pipeline_smoke() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((skill_root / "skill.yaml").read_text(encoding="utf-8")) or {}

    assert manifest["default_tool"] == "pipeline_smoke"
    assert manifest["exports"]["tools"] == ["pipeline_smoke"]


def test_skill_passes_validation() -> None:
    skill_root = Path(__file__).resolve().parents[1]

    report = SkillValidationService(None).validate_path(skill_root, install_mode=True)  # type: ignore[arg-type]

    assert report.ok is True


def test_pipeline_smoke_returns_followup_marker() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    entry_path = skill_root / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("skill_under_test.handlers.main", entry_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.pipeline_smoke() == {"ok": True, "marker": "followup"}


def test_pipeline_smoke_manifest_contract() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((skill_root / "skill.yaml").read_text(encoding="utf-8")) or {}
    tool_spec = next(tool for tool in manifest["tools"] if tool["name"] == "pipeline_smoke")

    assert tool_spec["entry"] == "handlers.main:pipeline_smoke"
    assert tool_spec["input_schema"]["additionalProperties"] is False
    assert tool_spec["output_schema"]["required"] == ["ok", "marker"]
    assert tool_spec["output_schema"]["properties"]["ok"]["const"] is True
    assert tool_spec["output_schema"]["properties"]["marker"]["const"] == "followup"
    assert tool_spec["output_schema"]["additionalProperties"] is False
