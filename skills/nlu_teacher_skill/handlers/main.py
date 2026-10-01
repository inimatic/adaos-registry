"""NLU Teacher uses core-owned host actions and has no private runtime tools."""

from __future__ import annotations


def dispose(_params: dict | None = None) -> dict[str, bool]:
    """Provide a bounded lifecycle hook for the presentation-only package."""

    return {"ok": True}
