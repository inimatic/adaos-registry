from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def lang_res() -> dict[str, str]:
    return {
        "prep.summary_header": "Preparation Summary",
        "prep.collected_resources": "Collected Resources",
        "prep.tested_hypotheses": "Tested Hypotheses",
    }


def run_prep(skill_path: Path) -> dict[str, Any]:
    """Create an empty, domain-neutral preparation result for a new skill."""

    result: dict[str, Any] = {
        "status": "ok",
        "timestamp": datetime.now(UTC).isoformat(),
        "resources": {},
        "tested_hypotheses": [],
    }
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "prep_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (skill_path / "prep_result_prompt.md").write_text(
        "# Preparation Summary\n\n"
        "## Collected Resources\n\n"
        "No domain-specific resources are required by the default template.\n\n"
        "## Tested Hypotheses\n\n"
        "No domain-specific checks are required by the default template.\n",
        encoding="utf-8",
    )
    return result
