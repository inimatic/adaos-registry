"""Migrate research control-plane data without retaining TLP primary data."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any


def migrate(payload: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(payload.get("source_data_root") or "")).resolve()
    target = Path(str(payload.get("target_data_root") or "")).resolve()
    copied: list[str] = []
    excluded: list[str] = []
    if source.exists() and source != target:
        target.mkdir(parents=True, exist_ok=True)
        # DB state and immutable historical attempt evidence remain owned by
        # the research control plane. The former STL-10 payload is adopted by
        # tlp_experiment_skill through its own lifecycle migration and must not
        # be duplicated into every future research-manager bucket.
        for relative in (Path("db"), Path("internal")):
            item = source / relative
            if item.is_dir():
                shutil.copytree(item, target / relative, dirs_exist_ok=True)
                copied.append(relative.as_posix())
        files = source / "files"
        if files.is_dir():
            for item in files.iterdir():
                relative = Path("files") / item.name
                if item.name == "datasets":
                    excluded.append(relative.as_posix())
                    continue
                destination = target / relative
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                elif not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
                copied.append(relative.as_posix())
    skill_root = Path(__file__).resolve().parents[1]
    if str(skill_root) not in sys.path:
        sys.path.insert(0, str(skill_root))

    from research.repository import ResearchRepository

    repository = ResearchRepository()
    return {
        "ok": True,
        "staged": True,
        "copied": copied,
        "excluded": excluded,
        "retention": "retain_until_previous_runtime_retired",
        "binding_id": repository._db.binding.binding_id,
        "provider_id": repository._db.binding.provider_id,
        "health": dict(repository._db.health()),
    }
