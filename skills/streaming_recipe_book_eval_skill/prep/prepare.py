from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def lang_res() -> dict[str, str]:
    return {"prep.summary_header": "Preparation Summary"}


def run_prep(skill_path: Path) -> dict:
    data_dir = skill_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resources": {"storage": "skill-local data/recipes.json", "network_required": False},
        "tested_hypotheses": [{"name": "Local JSON persistence available", "result": data_dir.is_dir(), "critical": True}],
    }
    (skill_path / "prep_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (skill_path / "prep_result_prompt.md").write_text("# Preparation Summary\n\n- Local-only recipe storage; no credentials or network access.\n", encoding="utf-8")
    return result
