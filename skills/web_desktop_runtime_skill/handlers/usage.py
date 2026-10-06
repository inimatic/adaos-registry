"""Bounded presentation of the public subscription usage projection."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from adaos.sdk import access, subscriptions


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 2**53 - 1 or not math.isfinite(value):
        return None
    return value


def get_subscription_usage(
    webspace_id: str | None = None, refresh: bool = False,
) -> dict[str, Any]:
    access.require("workspace.read")
    try:
        snapshot = subscriptions.get_codex_usage_snapshot(
            webspace_id=webspace_id, refresh=refresh, timeout=8.0,
        )
    except Exception as exc:
        snapshot = {"status": "unavailable", "reason": type(exc).__name__}
    if not isinstance(snapshot, Mapping):
        snapshot = {"status": "unavailable"}
    status = snapshot.get("status")
    if not isinstance(status, str) or status not in {"ready", "stale", "unavailable"}:
        status = "unavailable"
    available = status in {"ready", "stale"}
    windows = []
    for identity, label, field in (
        ("week", "Last 7 days", "used_7d_tokens"),
        ("month", "Last 30 days", "used_30d_tokens"),
        ("codex", "Last 24 hours", "used_tokens"),
    ):
        used = _number(snapshot.get(field)) if available else None
        windows.append((identity, label, used))
    scale = max((used for _identity, _label, used in windows if used is not None), default=None)
    metrics = []
    for identity, label, used in windows:
        percent = min(100.0, 100.0 * (used / scale)) if used is not None and scale else (0.0 if used == 0 else None)
        metrics.append({
            "id": identity, "label": label, "value": percent,
            "display": f"{used:,} tokens" if used is not None else "Unavailable",
            "used_tokens": used,
            "description": "No comparable usage window" if percent is None else "Relative to the largest reported window",
        })
    updated_at = snapshot.get("updated_at")
    updated_at = updated_at[:128] if isinstance(updated_at, str) else None
    reason = snapshot.get("reason")
    reason = reason[:160] if isinstance(reason, str) and reason.strip() else None
    description = "Rolling reported AI token usage; arcs compare time windows, not cost or quota."
    if status == "stale":
        description = "Stale usage; last reported values."
    elif status == "unavailable":
        description = "Usage unavailable. Retry when the subscription service is available."
        if reason:
            description += f" Reason: {reason}."
    if updated_at:
        description += f" Updated {updated_at}."
    return {
        "ok": available, "status": status, "reason": reason, "updated_at": updated_at,
        "usage_arc": {
            "value": metrics[1]["display"], "label": "Last 30 days",
            "subtitle": "Last 7 / 30 days", "description": description,
            "metrics": metrics,
        },
    }
