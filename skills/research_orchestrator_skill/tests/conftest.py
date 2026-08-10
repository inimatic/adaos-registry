from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from adaos.services.testing.bootstrap import bootstrap_test_ctx


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


@pytest.fixture(autouse=True)
def _skill_test_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base_dir = tmp_path / ".adaos"
    slot_dir = base_dir / "workspace" / "skills" / ".runtime" / "research_orchestrator_skill" / "v0.1" / "slots" / "A"
    slot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_ROOT / "skill.yaml", slot_dir / "skill.yaml")
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
    handle = bootstrap_test_ctx(skill_name="research_orchestrator_skill", skill_slot_dir=slot_dir, secrets={})
    try:
        yield handle.ctx
    finally:
        handle.teardown()
