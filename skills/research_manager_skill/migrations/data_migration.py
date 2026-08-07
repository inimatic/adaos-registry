"""Stage owner data and apply versioned relational migrations before activation."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def migrate(payload: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(payload.get("source_data_root") or "")).resolve()
    target = Path(str(payload.get("target_data_root") or "")).resolve()
    if source.exists() and source != target:
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            destination = target / item.name
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            elif not destination.exists():
                shutil.copy2(item, destination)
    from research.repository import ResearchRepository

    repository = ResearchRepository()
    return {
        "ok": True,
        "staged": True,
        "binding_id": repository._db.binding.binding_id,
        "provider_id": repository._db.binding.provider_id,
        "health": dict(repository._db.health()),
    }
