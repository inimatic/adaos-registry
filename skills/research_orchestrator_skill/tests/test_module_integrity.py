from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import yaml

from adaos.services.skill.validation import SkillValidationService


def test_skill_manifest_and_entrypoint_are_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "skill.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "research_orchestrator_skill"
    assert set(manifest["capabilities"]) == {
        "storage.relational",
        "builder.project_sources",
        "skills.invoke",
    }
    assert "accept_prototype" in manifest["exports"]["tools"]
    assert "get_formulation_run" in manifest["exports"]["tools"]
    assert manifest["conversation"]["dialog_channel"]["default_tool"] == "research_orchestrator_skill.chat"

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("research_orchestrator_skill.handlers.main", root / "handlers" / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.chat)
    assert callable(module.get_formulation_run)

    report = SkillValidationService(None).validate_path(root, install_mode=True)  # type: ignore[arg-type]
    assert report.ok is True, [f"{item.code}: {item.message}" for item in report.issues]


def test_acceptance_method_returns_through_atomic_idempotency_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    module = ast.parse(
        (root / "research" / "orchestrator.py").read_text(encoding="utf-8")
    )
    orchestrator = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "ResearchOrchestrator"
    )
    method = next(
        node
        for node in orchestrator.body
        if isinstance(node, ast.FunctionDef) and node.name == "accept"
    )
    final = method.body[-1]
    assert isinstance(final, ast.Return)
    assert isinstance(final.value, ast.Call)
    assert isinstance(final.value.func, ast.Attribute)
    assert final.value.func.attr == "once"
