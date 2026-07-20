from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from adaos.services.skill.validation import SkillValidationService


def validate_with_legacy_route_schema_compat(skill_root: Path, tmp_path: Path):
    """Validate current routes, projecting only fields unknown to legacy AdaOS schemas."""
    report = SkillValidationService(None).validate_path(skill_root, install_mode=True)  # type: ignore[arg-type]
    if report.ok:
        return report

    issues = [(issue.code, issue.message) for issue in report.issues]
    legacy_error = (
        len(issues) == 1
        and issues[0][0] == "schema.invalid"
        and "Additional properties are not allowed" in issues[0][1]
        and "'read_policy', 'tool' were unexpected" in issues[0][1]
    )
    if not legacy_error:
        return report

    projected_root = tmp_path / skill_root.name
    shutil.copytree(skill_root, projected_root)
    manifest_path = projected_root / "skill.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    for route in manifest.get("data_routes", []):
        route.pop("tool", None)
        route.pop("read_policy", None)
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return SkillValidationService(None).validate_path(projected_root, install_mode=True)  # type: ignore[arg-type]
