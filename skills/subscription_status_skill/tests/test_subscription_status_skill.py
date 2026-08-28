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
                    "metering": "codex_usage_stream",
                    "source": "builder_llm_job",
                    "accuracy": "reported",
                    "last_model": "gpt-5",
                },
            },
        },
    )

    payload = module.get_status(webspace_id="desktop-dev")

    assert payload["current"]["value"] == "builder"
    assert payload["buttons"][0]["id"] == "details"
    assert payload["resources"]["items"][0]["resource"] == "llm.requests"
    assert payload["usage_history"]["items"][0]["resource"] == "llm.requests"
    codex = next(row for row in payload["resources"]["items"] if row["resource"] == "codex.api.tokens")
    assert codex["metering"] == "codex_usage_stream"
    assert codex["accuracy"] == "reported"
    assert "Codex 30d: 100" in payload["current"]["description"]
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


def test_get_status_refreshes_root_when_entitlement_is_missing(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    calls: list[str] = []
    statuses = [
        {
            "generated_at": "2026-08-28T10:00:00Z",
            "subscription_state": "unassigned",
            "plan_id": "none",
            "entitlement_state": "disabled_observed",
            "disabled_resource_count": 11,
            "disabled_resources": [],
            "usage": {},
            "entitlement_snapshot": {"loaded": False},
        },
        {
            "generated_at": "2026-08-28T10:00:01Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "limited_observed",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {"llm.requests": {"used_24h": 4}},
            "entitlement_snapshot": {"loaded": True},
        },
    ]
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "refresh_entitlement_snapshot_from_root",
        lambda: calls.append("refresh") or {"ok": True},
    )
    monkeypatch.setattr(module, "current_subnet_economic_status", lambda: statuses.pop(0))

    payload = module.get_status(webspace_id="desktop")

    assert calls == ["refresh"]
    assert payload["current"]["value"] == "builder"
    assert payload["refresh"]["ok"] is True
    assert projection.values[0][1]["current"]["value"] == "builder"


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


def test_list_resources_returns_table_items(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-28T08:35:00Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "limited_observed",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {"llm.requests": {"used_24h": 3, "used_30d": 9}},
        },
    )

    payload = module.list_resources(webspace_id="desktop")

    assert payload["ok"] is True
    assert payload["items"][0]["resource"] == "llm.requests"
    assert payload["items"][0]["used_24h"] == 3
    assert projection.values[0][0] == "subscription_status.snapshot"


def test_list_usage_history_returns_observed_usage_rows(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-28T08:35:00Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "limited_observed",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {
                "codex.api.tokens": {
                    "used_24h": 500,
                    "used_30d": 1000,
                    "source": "builder_llm_job",
                    "accuracy": "estimated",
                    "last_model": "gpt-5",
                }
            },
        },
    )

    payload = module.list_usage_history(webspace_id="desktop")

    assert payload["ok"] is True
    assert payload["items"][0]["resource"] == "codex.api.tokens"
    assert payload["items"][0]["used_30d"] == 1000
    assert payload["items"][0]["accuracy"] == "estimated"
    assert payload["items"][0]["last_model"] == "gpt-5"


def test_request_plan_change_records_local_request(monkeypatch, tmp_path) -> None:
    module = _load_module()
    projection = _Projection()
    request_path = tmp_path / "plan_change_request.json"
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(module, "_plan_change_request_path", lambda: request_path)
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-28T11:15:00Z",
            "subnet_id": "sn_6f5a69bf",
            "zone_id": "eu",
            "subscription_state": "active",
            "plan_id": "personal",
            "entitlement_state": "enabled",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {},
        },
    )

    payload = module.request_plan_change("builder", note="need Codex quota", webspace_id="desktop")

    assert payload["ok"] is True
    assert payload["plan_change"]["desired_plan_id"] == "builder"
    assert request_path.exists()
    assert projection.values[0][1]["plan_change"]["value"] == "builder"


def test_root_management_event_refreshes_root_entitlement(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    calls: list[str] = []
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "refresh_entitlement_snapshot_from_root",
        lambda: calls.append("refresh") or {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-28T09:00:00Z",
            "subscription_state": "active",
            "plan_id": "personal",
            "entitlement_state": "enabled",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {},
        },
    )

    module.on_runtime_refresh(type("Evt", (), {"type": "root.mgmnt.snapshot.changed", "payload": {}})())

    assert calls == ["refresh"]
    assert projection.values[0][1]["current"]["value"] == "personal"
