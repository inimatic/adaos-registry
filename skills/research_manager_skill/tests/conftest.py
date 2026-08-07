from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from adaos.services.testing.bootstrap import bootstrap_test_ctx


@pytest.fixture(autouse=True)
def _skill_test_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide the same SDK context for direct and AdaOS install-time pytest."""

    skill_root = Path(__file__).resolve().parents[1]
    skill_name = skill_root.name
    base_dir = tmp_path / ".adaos"
    slot_dir = (
        base_dir
        / "workspace"
        / "skills"
        / ".runtime"
        / skill_name
        / "v0.1"
        / "slots"
        / "A"
    )
    slot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_root / "skill.yaml", slot_dir / "skill.yaml")
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
    handle = bootstrap_test_ctx(
        skill_name=skill_name,
        skill_slot_dir=slot_dir,
        secrets={},
    )
    try:
        yield handle.ctx
    finally:
        handle.teardown()
