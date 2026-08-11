"""Migrate universal research-orchestrator state between runtime buckets."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def _inside(root: Path, value: Path) -> bool:
    return value == root or root in value.parents


def migrate(payload: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(payload.get("source_data_root") or "")).resolve()
    target = Path(str(payload.get("target_data_root") or "")).resolve()
    if source == target:
        return {"ok": True, "staged": True, "copied": [], "excluded": [], "reason": "same_data_root"}
    if source.name != "data" or target.name != "data":
        raise ValueError("research orchestrator migration requires exact runtime data roots")

    copied: list[str] = []
    excluded: list[str] = []
    if source.exists():
        target.mkdir(parents=True, exist_ok=True)
        for relative in (Path("db"), Path("internal")):
            item = (source / relative).resolve()
            destination = (target / relative).resolve()
            if not _inside(source, item) or not _inside(target, destination):
                raise ValueError("data migration path escaped its runtime bucket")
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
                copied.append(relative.as_posix())

        files = (source / "files").resolve()
        if files.is_dir():
            for item in files.iterdir():
                relative = Path("files") / item.name
                if item.name == "uploads":
                    # Uploads are transient transport copies. Accepted source
                    # material is already owned by each direction skill.
                    excluded.append(relative.as_posix())
                    continue
                destination = (target / relative).resolve()
                if not _inside(target, destination):
                    raise ValueError("file migration path escaped target bucket")
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
                copied.append(relative.as_posix())

    return {
        "ok": True,
        "staged": True,
        "copied": copied,
        "excluded": excluded,
        "retention": "retain_until_previous_runtime_retired",
        "source_deleted": False,
    }
