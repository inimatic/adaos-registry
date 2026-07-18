from __future__ import annotations

from typing import Any

from adaos.sdk.core.decorators import tool


@tool(summary="Run the Builder pipeline smoke check.", side_effects="none")
def pipeline_smoke() -> dict[str, Any]:
    """Return the stable marker used to verify the Builder tool pipeline."""
    return {"ok": True, "marker": "followup"}
