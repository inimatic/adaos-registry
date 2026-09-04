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


def test_lang_resources_are_mapping() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    entry_path = skill_root / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("skill_under_test.handlers.main", entry_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if hasattr(module, "lang_res"):
        assert isinstance(module.lang_res(), dict)


def test_template_declares_conversation_contract() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((skill_root / "skill.yaml").read_text(encoding="utf-8")) or {}

    assert manifest["default_tool"] == "chat"
    assert manifest["supported_locales"] == ["en", "ru"]
    assert set(manifest["exports"]["tools"]) >= {"chat", "ask_for_details", "remember_preference"}
    conversation = manifest["conversation"]
    assert conversation["dialog_channel"]["owner"] == "skill:flowboard_lab_for_safely_prototyping_a_r_17528146_skill"
    assert conversation["dialog_channel"]["default_tool"] == "flowboard_lab_for_safely_prototyping_a_r_17528146_skill.chat"
    assert conversation["forms"][0]["repair"]["max_turns"] == 3
    assert len(conversation["agents"]) >= 2
    assert {item["talk_tool"] for item in conversation["agents"]} == {"chat"}


def test_template_passes_conversation_storage_validation() -> None:
    skill_root = Path(__file__).resolve().parents[1]

    report = SkillValidationService(None).validate_path(skill_root, install_mode=True)  # type: ignore[arg-type]

    conversation_codes = {issue.code for issue in report.issues if issue.code.startswith("conversation.")}
    assert report.ok is True
    assert conversation_codes == set()
