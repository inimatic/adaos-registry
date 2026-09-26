from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def test_compact_cbs_provider_compiles_to_package_neutral_contracts():
    from adaos.services.artifact_pipeline.cbs_authoring import (
        BINDING_OUTPUT_PATH,
        CAPABILITY_OUTPUT_PATH,
        compile_cbs_provider_files,
    )

    root = Path(__file__).resolve().parents[1]
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".runtime" not in path.parts
    }
    compiled = compile_cbs_provider_files(files, kind="skill")

    assert compiled is not None
    assert compiled.capability.capability_ref == "capability:subscriptions.status.inspect"
    assert compiled.binding.binding_definition_ref == (
        "binding-definition:subscriptions.status.inspect.adaos-root-local"
    )
    assert set(compiled.generated_files) == {
        CAPABILITY_OUTPUT_PATH,
        BINDING_OUTPUT_PATH,
    }


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


def test_webui_reads_subscription_projection_without_calling_write_tools() -> None:
    skill_dir = Path(__file__).resolve().parents[1]
    webui = json.loads((skill_dir / "webui.json").read_text(encoding="utf-8"))

    data_sources: list[dict] = []

    def collect(value) -> None:
        if isinstance(value, dict):
            source = value.get("dataSource")
            if isinstance(source, dict):
                data_sources.append(source)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(webui)

    assert data_sources
    assert all(source.get("kind") == "y" for source in data_sources)
    assert all(source.get("scope") == "shared" for source in data_sources)
    assert {source.get("path") for source in data_sources} == {
        "data/subscription_status",
        "data/subscription_status/raw",
        "data/subscription_status/resources",
        "data/subscription_status/usage_history",
        "data/subscription_status/codex_models",
        "data/subscription_status/refresh",
    }


def test_details_click_opens_exactly_one_subscription_modal() -> None:
    skill_dir = Path(__file__).resolve().parents[1]
    webui = json.loads((skill_dir / "webui.json").read_text(encoding="utf-8"))
    widget = next(item for item in webui["widgets"] if item["id"] == "subscription_status_widget")

    modal_actions = [
        action
        for action in widget["actions"]
        if action.get("params", {}).get("modalId") == "subscription_status_modal"
    ]

    assert len(modal_actions) == 1
    assert modal_actions[0]["on"] == "click:details"


def test_subscription_widget_refreshes_root_status_on_mount() -> None:
    skill_dir = Path(__file__).resolve().parents[1]
    webui = json.loads((skill_dir / "webui.json").read_text(encoding="utf-8"))
    widget = next(item for item in webui["widgets"] if item["id"] == "subscription_status_widget")

    mount_action = next(action for action in widget["actions"] if action["on"] == "mount")

    assert mount_action["type"] == "callSkill"
    assert mount_action["target"] == "subscription_status_skill.refresh_status"
    assert mount_action["params"]["webspace_id"] == "$client.webspaceId"


def test_refresh_action_preserves_the_snapshot_written_by_refresh_status() -> None:
    skill_dir = Path(__file__).resolve().parents[1]
    webui = json.loads((skill_dir / "webui.json").read_text(encoding="utf-8"))

    modal = webui["registry"]["modals"]["subscription_status_modal"]["schema"]
    command_bar = next(widget for widget in modal["widgets"] if widget["id"] == "subscription-status-actions")
    refresh_action = next(action for action in command_bar["actions"] if action["on"] == "click:refresh")

    assert refresh_action["type"] == "callSkill"
    assert refresh_action["target"] == "subscription_status_skill.refresh_status"
    assert refresh_action["params"]["webspace_id"] == "$client.webspaceId"
    assert "invalidates" not in refresh_action


def test_subscription_modal_refreshes_after_user_identity_is_ready() -> None:
    skill_dir = Path(__file__).resolve().parents[1]
    webui = json.loads((skill_dir / "webui.json").read_text(encoding="utf-8"))

    modal = webui["registry"]["modals"]["subscription_status_modal"]["schema"]
    command_bar = next(widget for widget in modal["widgets"] if widget["id"] == "subscription-status-actions")
    mount_action = next(action for action in command_bar["actions"] if action["on"] == "mount")

    assert mount_action == {
        "on": "mount",
        "type": "callSkill",
        "target": "subscription_status_skill.refresh_status",
        "params": {"webspace_id": "$client.webspaceId"},
    }


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
                    "quota_metric": "billable_tokens",
                    "optimization_metric": "fresh_plus_output_tokens",
                    "usage_breakdown": {
                        "window_24h": {
                            "fresh_plus_output_tokens": 25,
                            "cached_input_tokens": 80,
                            "output_tokens": 5,
                            "runs": 1,
                            "zero_model_tasks": 1,
                        },
                        "window_30d": {"fresh_plus_output_tokens": 40},
                    },
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
    assert codex["fresh_plus_output_24h"] == 25
    assert codex["cached_input_24h"] == 80
    assert codex["zero_model_tasks_24h"] == 1
    assert "Usage in the last 24h: LLM 2/20000, left 19995; Codex quota 100" in payload["current"]["description"]
    assert "fresh + output 25; cached input 80" in payload["current"]["description"]
    assert "Codex 30d: 100" in payload["current"]["description"]
    assert projection.values[0][0] == "subscription_status.snapshot"
    assert projection.values[0][2]["webspace_id"] == "desktop-dev"


def test_status_projection_normalizes_legacy_codex_metering(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(
        module,
        "current_subnet_economic_status",
        lambda: {
            "generated_at": "2026-08-28T15:00:00Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "limited_observed",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {
                "codex.api.tokens": {
                    "used_30d": 10,
                    "quota_limit": 20000000,
                    "quota_remaining": 19999990,
                    "metering": "manual_adjustment_pending_codex_stream",
                }
            },
        },
    )

    payload = module.get_status(webspace_id="desktop")

    codex = next(row for row in payload["resources"]["items"] if row["resource"] == "codex.api.tokens")
    assert codex["metering"] == "codex_usage_stream"


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
    assert len(projection.values) == 2
    assert projection.values[0][0] == "subscription_status.snapshot"
    assert projection.values[0][1]["refresh"]["status"] == "refreshing"
    assert projection.values[1][1]["refresh"]["status"] == "refreshed"
    assert projection.values[1][1]["refresh"]["updated_at"] == "2026-08-27T15:00:00Z"


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


def test_get_status_refreshes_stale_entitlement_snapshot(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    calls: list[str] = []
    statuses = [
        {
            "generated_at": "2026-08-28T15:00:00Z",
            "subscription_state": "active",
            "plan_id": "personal",
            "entitlement_state": "enabled",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {},
            "entitlement_snapshot": {"loaded": True, "updated_at": "2026-08-28T14:00:00Z"},
        },
        {
            "generated_at": "2026-08-28T15:00:01Z",
            "subscription_state": "active",
            "plan_id": "builder",
            "entitlement_state": "limited_observed",
            "disabled_resource_count": 0,
            "disabled_resources": [],
            "usage": {},
            "entitlement_snapshot": {"loaded": True, "updated_at": "2026-08-28T15:00:01Z"},
        },
    ]
    monkeypatch.setattr(module, "_LAST_AUTO_ROOT_REFRESH_AT", 0.0)
    monkeypatch.setattr(module, "time", type("Clock", (), {"monotonic": staticmethod(lambda: 1000.0), "time": staticmethod(lambda: 1787929200.0)}))
    monkeypatch.setattr(module, "ctx_current_user", projection)
    monkeypatch.setattr(module, "current_subnet_economic_status", lambda: statuses.pop(0))
    monkeypatch.setattr(
        module,
        "refresh_entitlement_snapshot_from_root",
        lambda: calls.append("refresh") or {"ok": True},
    )

    payload = module.get_status(webspace_id="desktop")

    assert calls == ["refresh"]
    assert payload["current"]["value"] == "builder"


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


def test_codex_models_display_unknown_cost_without_fabricating_zero(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "_read_plan_change_request", lambda: {})
    payload = module._projection_payload({"usage": {"codex.api.tokens": {
        "usage_breakdown": {"window_24h": {"by_model": [
            {"model": "model-a", "runs": 1, "cost": {
                "status": "estimated", "currency": "USD", "estimated_usd": 0.00123}},
            {"model": None, "runs": 1},
        ]}}}}})
    rows = payload["codex_models"]["items"]
    assert rows[0]["estimated_usd"] == "0.001230"
    assert rows[1]["estimated_usd"] == ""
    assert rows[1]["cost_status"] == "unavailable"
    assert rows[1]["model"] == "Unknown"
