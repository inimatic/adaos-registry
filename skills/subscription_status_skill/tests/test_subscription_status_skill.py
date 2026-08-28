from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("test_subscription_status_handlers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Projection:
    def __init__(self) -> None:
        self.values = []

    def set(self, slot, value, **kwargs):
        self.values.append((slot, value, kwargs))


def test_status_projection_exposes_quota_rows(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-27T13:00:00Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "enabled",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {
                "llm.requests": {
                    "used_24h": 2,
                    "used_30d": 5,
                    "quota_limit": 20000,
                    "quota_remaining": 19995,
                    "quota_period": "month",
                    "quota_unit": "requests",
                },
                "codex.api.tokens": {
                    "used_24h": 100,
                    "used_30d": 100,
                    "quota_limit": 20000000,
                    "quota_remaining": 19999900,
                    "quota_period": "month",
                    "quota_unit": "tokens",
                },
            },
        },
    )

    payload = module.get_status(webspace_id="desktop-dev")

    assert payload["current"]["value"] == "builder"
    assert payload["resources"]["items"][0]["resource"] == "llm.requests"
    assert any(row["resource"] == "codex.api.tokens" for row in payload["resources"]["items"])
    assert projection.values[0][0] == "subscription_status.snapshot"
    assert projection.values[0][2]["webspace_id"] == "desktop-dev"


def test_refresh_status_pulls_root_entitlement(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    calls: list[str] = []
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "refresh_entitlement_snapshot_from_root",
        lambda: calls.append("refresh") or {"ok": True, "plan_id": "builder"},
    )
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-27T15:00:00Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "enabled",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {"llm.requests": {"used_24h": 1}},
        },
    )

    payload = module.refresh_status(webspace_id="desktop")

    assert calls == ["refresh"]
    assert payload["refresh"]["ok"] is True
    assert payload["current"]["value"] == "builder"
    assert projection.values[0][0] == "subscription_status.snapshot"


def test_active_subscription_with_plan_disabled_resources_is_warning(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-27T16:00:00Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "limited_observed",
            "disabled_resource_count": 1,
            "disabled_resources": [{"resource": "media.indexing", "reason_code": "resource_not_in_plan"}],
            "usage": {"llm.requests": {"used_24h": 1}},
        },
    )

    payload = module.get_status(webspace_id="desktop")

    assert payload["current"]["color"] == "warning"
