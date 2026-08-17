from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from adaos.sdk.data.projections import clear_projection_demand

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg


def _load_infrastate_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_infrastate_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_projection_demand_registry():
    clear_projection_demand()
    yield
    clear_projection_demand()


def _enable_projection_demand(mod, *slot_names: str, webspace_id: str = "default") -> None:
    clear_projection_demand()
    mod._PROJECTION_RUNTIME.reset()
    for slot_name in slot_names:
        mod._PROJECTION_RUNTIME.remember_projection(slot_name, webspace_id=webspace_id, subscription_id="test")


def _run_stream_snapshots_inline(mod, monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "_schedule_stream_receiver_snapshot",
        lambda receiver, webspace_id, *, reason, node_id=None: mod._publish_registered_stream_receiver_snapshot(
            receiver,
            webspace_id,
            reason=reason,
            node_id=node_id,
        ),
    )


def test_infrastate_summary_projection_stays_current_without_browser_demand():
    mod = _load_infrastate_module()

    writes: list[tuple[str, object, str | None]] = []

    class _ProjectionContext:
        async def set_async(self, slot: str, value: object, *, webspace_id: str | None = None) -> None:
            writes.append((slot, value, webspace_id))

    summary_slot = mod._PROJECTION_SLOT_BY_NAME["infrastate.summary"]
    actions_slot = mod._PROJECTION_SLOT_BY_NAME["infrastate.actions"]
    mod._PROJECTION_RUNTIME.reset()
    mod._PROJECTION_RUNTIME.bind_ctx_subnet(_ProjectionContext())

    summary_result = asyncio.run(
        mod._PROJECTION_RUNTIME.set_if_changed(
            summary_slot,
            {"value": "succeeded", "subtitle": "slot B"},
            webspace_id="desktop",
        )
    )
    actions_result = asyncio.run(
        mod._PROJECTION_RUNTIME.set_if_changed(
            actions_slot,
            [],
            webspace_id="desktop",
        )
    )

    assert summary_slot.demand == "always"
    assert summary_result.written is True
    assert writes == [("infrastate.summary", {"value": "succeeded", "subtitle": "slot B"}, "desktop")]
    assert actions_result.reason == "no_active_projection_demand"


def test_infrastate_local_version_skips_sparse_placeholder(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    placeholder = tmp_path / "scenarios" / "sparse_scene"
    placeholder.mkdir(parents=True)
    (placeholder / ".gitignore").write_text("*/impl/\n", encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "build_registry_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("strict parser must not run")),
    )

    assert mod._read_local_artifact_version(tmp_path, "scenarios", "sparse_scene") is None


def test_infrastate_yjs_tabs_do_not_self_reference_sync_runtime():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"

    reliability = {
        "runtime": {
            "sync_runtime": {
                "webspaces": {
                    "default": {
                        "log_mode": "snapshot_plus_diff",
                        "update_log_entries": 1,
                        "replay_window_entries": 1,
                    }
                }
            }
        }
    }

    selected = mod._selected_yjs_webspace_id({}, reliability)
    items = mod._yjs_webspace_tabs(_Conf(), {}, reliability, {"kind": "local"})

    assert selected == "default"
    assert items
    assert items[0]["id"] == "default"


def test_infrastate_node_label_skips_webspace_like_noise():
    mod = _load_infrastate_module()

    label = mod._node_label(["default", "desktop", {"WEBSPACE_ID": "DEFAULT"}, "TE1"], fallback="hub")

    assert label == "TE1"


def test_infrastate_node_tabs_keep_offline_member_selected():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"
        node_names = ["Hub"]

    reliability = {
        "runtime": {
            "hub_member_connection_state": {
                "known_members": [
                    {
                        "node_id": "member-1",
                        "node_names": ["TE1"],
                        "connected": False,
                        "state": "offline",
                        "observed_via": "subnet_directory",
                    }
                ]
            }
        }
    }

    tabs, selected = mod._node_tabs(_Conf(), {"selected_node_id": "member-1"}, reliability)
    local_tab = next(item for item in tabs if item["id"] == "hub-1")
    member_tab = next(item for item in tabs if item["id"] == "member-1")

    assert any(item["id"] == "member-1" for item in tabs)
    assert selected["node_id"] == "member-1"
    assert selected["kind"] == "member"
    assert selected["connected"] is False
    assert local_tab["node_status"] == "online"
    assert member_tab["node_status"] == "offline"


def test_infrastate_compact_summary_bounds_description_to_yjs_budget():
    mod = _load_infrastate_module()

    description = "state=" + ("ready|" * 1000)
    compact = mod._compact_summary_for_yjs({"description": description})

    assert compact["description"] != description
    assert compact["description"].startswith("state=ready|")
    assert "truncated; full diagnostics" in compact["description"]
    assert len(compact["description"].encode("utf-8")) <= 1200
    assert len(mod._stable_json_bytes(compact)) < 4096


def test_infrastate_compact_summary_keeps_runtime_identity():
    mod = _load_infrastate_module()

    compact = mod._compact_summary_for_yjs(
        {
            "value": "succeeded",
            "subtitle": "slot B | 0.1.272 | 392083c",
            "target_version": "392083c8afffdcb862de277c58e5a914a4cf5970",
            "runtime_git_short_commit": "392083c",
            "runtime_git_commit": "392083c8afffdcb862de277c58e5a914a4cf5970",
            "runtime_git_branch": "HEAD",
            "runtime_git_subject": "chore: bump adaos version to 0.1.272",
        }
    )

    assert compact["target_version"] == "392083c8afffdcb862de277c58e5a914a4cf5970"
    assert compact["runtime_git_short_commit"] == "392083c"
    assert compact["runtime_git_commit"] == "392083c8afffdcb862de277c58e5a914a4cf5970"
    assert compact["runtime_git_subject"] == "chore: bump adaos version to 0.1.272"


def test_infrastate_compact_snapshot_excludes_yjs_controls():
    mod = _load_infrastate_module()

    compact = mod._compact_snapshot_for_yjs(
        {
            "summary": {"description": "ok"},
            "core_actions": [{"id": "refresh", "title": "Refresh"}],
            "yjs_actions": [{"id": "yjs_reset", "title": "Yjs reset"}],
            "yjs_webspaces": [{"id": "default", "label": "default *"}],
        }
    )

    assert "core_actions" in compact
    assert "yjs_actions" not in compact
    assert "yjs_webspaces" not in compact


def test_infrastate_compact_snapshot_projects_yjs_balancer():
    mod = _load_infrastate_module()

    balancer = {
        "schema": "adaos.yjs_balancer.v1",
        "webspace_id": "desktop",
        "state": "watch",
        "reason": "active_connection_limit_near",
        "health": {"available": True, "server_ready": True},
        "usage": {
            "active_connections": 5,
            "active_connection_limit": 6,
            "active_client_sessions": [{"dev_id": "dev-1", "session_count": 2}],
        },
        "limits": {"max_active_per_webspace": 6},
        "guard": {"recent_attempts_10s": 1},
        "observed": {
            "hot_clients": [{"device_id": "dev-1", "attempt_15s": 1}],
            "active_by_webspace": [{"webspace_id": "desktop", "active_connections": 5}],
            "server": {"ready": True, "room_total": 1},
        },
        "raw_diag": {"should_not": "leak"},
    }

    compact = mod._compact_snapshot_for_yjs({"yjs_balancer": balancer})
    sections = mod._projection_sections_from_snapshot({"yjs_balancer": balancer})

    assert mod._PROJECTION_SLOT_PATHS["infrastate.yjs_balancer"] == "data/infrastate/yjs_balancer"
    assert mod._PROJECTION_SECTION_TO_SLOT["yjs_balancer"] == "infrastate.yjs_balancer"
    assert compact["yjs_balancer"]["state"] == "watch"
    assert compact["yjs_balancer"]["usage"]["active_connections"] == 5
    assert "raw_diag" not in compact["yjs_balancer"]
    assert sections["infrastate.yjs_balancer"]["guard"]["recent_attempts_10s"] == 1


def test_infrastate_update_actions_use_member_label():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    reliability = {
        "runtime": {
            "hub_member_connection_state": {
                "known_members": [
                    {
                        "node_id": "member-1",
                        "node_label": "Edge One",
                    }
                ]
            }
        }
    }

    items = mod._update_actions(_Conf(), {"selected_node_id": "member-1"}, reliability)

    assert items
    assert items[0]["title"] == "Update (Edge One)"


def test_infrastate_inventory_update_actions_include_bulk_controls():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    items = mod._update_actions(_Conf(), {"selected_node_id": "hub-1"}, {})

    assert [item["id"] for item in items] == [
        "adaos_update",
        "inventory_activate",
        "inventory_validate",
        "inventory_test",
        "marketplace",
    ]
    assert [item["label"] for item in items] == ["Update", "Activate", "Validate", "Test", "Marketplace"]
    assert [item["title"] for item in items] == ["Update", "Activate", "Validate", "Test", "Marketplace"]


def test_infrastate_inventory_update_actions_show_bulk_progress(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    items = mod._update_actions(
        _Conf(),
        {
            "selected_node_id": "hub-1",
            "inventory_bulk_action": "inventory_activate",
            "inventory_bulk_action_running": True,
        },
        {},
    )

    by_id = {item["id"]: item for item in items}
    assert by_id["inventory_activate"]["label"] == "Activating..."
    assert by_id["inventory_activate"]["disabled"] is True
    assert by_id["inventory_validate"]["disabled"] is True

    monkeypatch.setattr(mod.time, "time", lambda: 100.0)
    items = mod._update_actions(
        _Conf(),
        {
            "selected_node_id": "hub-1",
            "inventory_bulk_action": "inventory_activate",
            "inventory_bulk_action_running": False,
            "inventory_bulk_action_finished_at": 95.0,
            "inventory_bulk_action_failed": False,
        },
        {},
    )
    by_id = {item["id"]: item for item in items}
    assert by_id["inventory_activate"]["label"] == "Activated"
    assert by_id["inventory_activate"]["disabled"] is False


def test_infrastate_compact_action_list_preserves_disabled_state():
    mod = _load_infrastate_module()

    compact = mod._compact_action_list_for_yjs(
        [{"id": "inventory_activate", "label": "Activating...", "title": "Activating...", "disabled": True}]
    )

    assert compact == [
        {"id": "inventory_activate", "label": "Activating...", "title": "Activating...", "disabled": True}
    ]


def test_infrastate_inventory_bulk_action_dispatch_does_not_require_row_name(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    ui_updates: list[dict[str, object]] = []
    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "hub-1"})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setattr(
        mod,
        "_inventory_activate_local",
        lambda *, webspace_id=None: {"ok": True, "action": "inventory_activate", "webspace_id": webspace_id},
    )

    result = mod._perform_action("inventory_activate", _Conf(), {"webspace_id": "desktop"})

    assert result == {"ok": True, "action": "inventory_activate", "webspace_id": "desktop"}
    assert ui_updates[-1]["last_action"] == "inventory_activate"


def test_infrastate_set_node_names_prefers_selected_member_over_injected_local_node(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    pushed: list[tuple[str, list[str]]] = []
    ui_updates: list[dict[str, object]] = []

    class _Manager:
        async def set_member_node_names(self, node_id: str, node_names: list[str]) -> None:
            pushed.append((node_id, list(node_names)))

    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "member-1"})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.subnet.link_manager",
        types.SimpleNamespace(get_hub_link_manager=lambda: _Manager()),
    )

    result = mod._perform_action(
        "set_node_names",
        _Conf(),
        {"node_id": "hub-1", "value": "Edge One"},
    )

    assert result["ok"] is True
    assert result["scope"] == "remote-member"
    assert result["node_id"] == "member-1"
    assert pushed == [("member-1", ["Edge One"])]
    assert ui_updates[-1]["selected_node_id"] == "member-1"


def test_infrastate_remote_node_scoped_start_update_routes_to_member(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    requests: list[tuple[str, dict[str, object]]] = []
    ui_updates: list[dict[str, object]] = []

    class _Manager:
        async def request_member_update(self, node_id: str, **kwargs: object) -> dict[str, object]:
            requests.append((node_id, dict(kwargs)))
            return {
                "ok": True,
                "accepted": True,
                "node_id": node_id,
                "action": kwargs.get("action"),
            }

    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setattr(
        mod,
        "read_core_update_status",
        lambda: {"state": "idle", "target_rev": "rev-123", "target_version": "0.1.0+rev123"},
    )
    monkeypatch.setattr(
        mod,
        "_post_local_admin",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local update must not be called")),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.subnet.link_manager",
        types.SimpleNamespace(get_hub_link_manager=lambda: _Manager()),
    )

    result = mod._perform_action(
        "start_update",
        _Conf(),
        {"target_node_id": "member-1", "node_id": "member-1"},
    )

    assert result["ok"] is True
    assert result["accepted"] is True
    assert requests == [
        (
            "member-1",
            {
                "action": "update",
                "target_rev": "rev-123",
                "target_version": "0.1.0+rev123",
                "countdown_sec": 60.0,
                "drain_timeout_sec": 10.0,
                "signal_delay_sec": 0.25,
                "reason": "infrastate.member_start_update",
            },
        )
    ]
    assert ui_updates[-1]["selected_node_id"] == "member-1"
    assert ui_updates[-1]["last_action"] == "member_start_update"


def test_infrastate_set_node_names_uses_sdk_layer_for_local_update(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    ui_updates: list[dict[str, object]] = []
    sdk_calls: list[tuple[Any]] = []
    node_config_calls: list[str] = []

    def _sdk_set_node_names(value: Any) -> dict[str, object]:
        names = [item.strip() for item in str(value).split(",") if item.strip()] if isinstance(value, str) else list(value)
        sdk_calls.append((value, names))
        return {"node_id": "hub-1", "node_names": names}

    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setattr(mod._sdk_node, "set_node_names", _sdk_set_node_names)
    monkeypatch.setattr(
        mod._node_config,
        "save_config",
        lambda *args, **kwargs: node_config_calls.append("save_config"),
    )

    def _forbid_direct_node_names_set(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("node_config.set_node_names should not be called")

    monkeypatch.setattr(
        mod._node_config,
        "set_node_names",
        _forbid_direct_node_names_set,
    )

    result = mod._perform_action(
        "set_node_names",
        _Conf(),
        {"node_id": "hub-1", "value": "Hub Node"},
    )

    assert result["ok"] is True
    assert result["scope"] == "local"
    assert result["node_id"] == "hub-1"
    assert result["node_names"] == ["Hub Node"]
    assert not node_config_calls
    assert sdk_calls == [(["Hub Node"], ["Hub Node"])]
    assert ui_updates[-1]["last_action"] == "set_node_names"


def test_infrastate_marketplace_action_is_a_safe_noop(monkeypatch):
    mod = _load_infrastate_module()
    ui_updates: list[dict[str, object]] = []

    class _Conf:
        node_id = "hub-1"

    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "member-1"})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))

    result = mod._perform_action("marketplace", _Conf(), {})

    assert result == {"ok": True, "action": "marketplace", "selected_node_id": "member-1"}
    assert ui_updates[-1]["last_action"] == "marketplace"


def test_infrastate_marketplace_items_include_selected_node_target(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [{"kind": kind[:-1], "id": f"{kind[:-1]}_one", "name": f"{kind[:-1]}_one", "version": "1.0.0"}],
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {})

    items = mod._marketplace_items(webspace_id="default", selected_node_id="member-1", local_node_id="hub-1")

    assert items["skills"][0]["node_id"] == "member-1"
    assert items["skills"][0]["target_node_id"] == "member-1"


def test_infrastate_marketplace_stream_defaults_to_selected_node(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="hub-1"))
    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "member-1"})
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [{"kind": kind[:-1], "id": f"{kind[:-1]}_one", "name": f"{kind[:-1]}_one", "version": "1.0.0"}],
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {})

    rows = mod._build_stream_payload_for_receiver(mod._marketplace_skills_receiver(), "homepoint")

    assert rows[0]["node_id"] == "member-1"
    assert rows[0]["target_node_id"] == "member-1"


def test_infrastate_marketplace_stream_honors_explicit_target_node(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="hub-1"))
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [{"kind": kind[:-1], "id": f"{kind[:-1]}_one", "name": f"{kind[:-1]}_one", "version": "1.0.0"}],
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {})

    rows = mod._build_stream_payload_for_receiver(
        mod._marketplace_skills_receiver(),
        "homepoint",
        selected_node_id="member-1",
    )

    assert rows[0]["node_id"] == "member-1"
    assert rows[0]["target_node_id"] == "member-1"


def test_infrastate_marketplace_uses_remote_installed_set(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="hub-1"))
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: {})
    monkeypatch.setattr(mod, "_lightweight_member_reliability", lambda conf, lifecycle=None: {"members": []})
    monkeypatch.setattr(mod, "_selected_member_entry", lambda reliability, node_id: {"node_id": node_id})
    monkeypatch.setattr(
        mod,
        "_remote_capacity_inventory_items",
        lambda member, kind: [{"name": "weather_skill"}] if kind == "skills" else [],
    )
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [
            {"kind": "skill", "id": "weather_skill", "name": "weather_skill", "version": "1.0.0"},
            {"kind": "skill", "id": "calendar_skill", "name": "calendar_skill", "version": "1.0.0"},
        ] if kind == "skills" else [],
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {})

    rows = mod._marketplace_items(
        webspace_id="desktop",
        selected_node_id="member-1",
        local_node_id="hub-1",
    )["skills"]

    assert [row["id"] for row in rows] == ["calendar_skill"]
    assert rows[0]["target_node_id"] == "member-1"


def test_infrastate_direct_marketplace_tool_returns_items(monkeypatch):
    mod = _load_infrastate_module()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="hub-1"))

    def _marketplace_items(**kwargs):
        calls.append(dict(kwargs))
        return {
            "skills": [{"id": "weather_skill"}],
            "scenarios": [{"id": "web_desktop"}],
        }

    monkeypatch.setattr(
        mod,
        "_marketplace_items",
        _marketplace_items,
    )

    result = mod.get_marketplace(kind="skills", webspace_id="desktop", target_node_id="hub-1")

    assert result["ok"] is True
    assert result["kind"] == "skills"
    assert result["count"] == 1
    assert result["items"] == [{"id": "weather_skill"}]
    assert calls == [
        {
            "webspace_id": "desktop",
            "selected_node_id": "hub-1",
            "local_node_id": "hub-1",
            "requested_kinds": {"skills"},
        }
    ]


def test_infrastate_scenario_marketplace_does_not_build_skill_inventory(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(
        mod,
        "_skills_items",
        lambda: (_ for _ in ()).throw(AssertionError("scenario-only request must not build skill inventory")),
    )
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (
            [{"kind": "scenario", "id": "media_center", "name": "media_center", "version": "0.4.0"}]
            if kind == "scenarios"
            else (_ for _ in ()).throw(AssertionError("scenario-only request must not load skill catalog"))
        ),
    )
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {})

    items = mod._marketplace_items(webspace_id="desktop", requested_kinds={"scenarios"})

    assert items["skills"] == []
    assert [item["id"] for item in items["scenarios"]] == ["media_center"]


def test_infrastate_direct_inventory_tool_returns_items(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="hub-1"))
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "_inventory_items_from", lambda _builder: [{"name": "web_desktop"}])
    monkeypatch.setattr(mod, "_inventory_drift_only_enabled", lambda *_args, **_kwargs: False)

    result = mod.get_inventory(kind="scenarios", webspace_id="desktop", target_node_id="hub-1")

    assert result["ok"] is True
    assert result["kind"] == "scenarios"
    assert result["count"] == 1
    assert result["items"] == [{"name": "web_desktop"}]


def test_infrastate_compact_snapshot_keeps_semantic_state_plane_contracts():
    mod = _load_infrastate_module()

    compact = mod._compact_snapshot_for_client(
        {
            "summary": {"label": "Infra"},
            "reliability": {
                "ok": True,
                "node": {"node_id": "hub-1"},
                "runtime": {
                    "assessment": {"state": "ready"},
                    "channel_overview": {"hub_root": {"effective_status": "ready"}},
                    "readiness_tree": {"route": {"status": "ready"}},
                    "connectivity": {
                        "required_upstream_link": {"kind": "hub_root", "transport_state": "ready"},
                    },
                    "state_sync": {
                        "webspace_id": "desktop",
                        "semantic_state": "stale",
                        "freshness_state": "aging",
                    },
                    "yjs_pressure": {
                        "owner": "_by_owner/skill_infrastate_skill",
                        "policy_state": "throttle",
                    },
                },
            },
        }
    )

    runtime = compact["reliability"]["runtime"]
    assert runtime["connectivity"]["required_upstream_link"]["kind"] == "hub_root"
    assert runtime["state_sync"]["semantic_state"] == "stale"
    assert runtime["yjs_pressure"]["policy_state"] == "throttle"


def test_infrastate_reliability_compaction_strips_heavy_runtime_payloads():
    mod = _load_infrastate_module()

    compact = mod._compact_reliability_for_infrastate(
        {
            "ok": True,
            "runtime": {
                "sync_runtime": {
                    "selected_webspace_id": "desktop",
                    "assessment": {"state": "pressure"},
                    "transport": {"room_total": 1},
                    "load_mark": {
                        "assessment": {"state": "critical"},
                        "selected_webspace": {"recent_bytes_total": 42},
                        "webspaces": {"desktop": {"owners": {"huge": "tree"}}},
                    },
                    "webspaces": {
                        "desktop": {
                            "webspace_id": "desktop",
                            "update_log_entries": 7,
                            "oversized": {"x": list(range(20))},
                        },
                        "other": {"update_log_entries": 99},
                    },
                    "selected_webspace": {
                        "webspace_id": "desktop",
                        "rebuild": {
                            "status": "ready",
                            "materialization": {
                                "ready": True,
                                "registry": {"large": "omitted"},
                                "catalog_counts": {"apps": 3},
                            },
                        },
                    },
                },
                "hub_member_connection_state": {
                    "role": "hub",
                    "known_members": [
                        {
                            "node_id": "member-1",
                            "connected": True,
                            "node_snapshot": {
                                "build": {"version": "1"},
                                "desktop_catalog": {
                                    "apps": [{"id": "a"}],
                                    "registry": {"modals": {"m": {"very": "large"}}},
                                },
                            },
                        }
                    ],
                },
            },
        }
    )

    sync = compact["runtime"]["sync_runtime"]
    assert sync["webspaces"] == {"desktop": {"webspace_id": "desktop", "update_log_entries": 7}}
    assert "webspaces" not in sync["load_mark"]
    assert sync["selected_webspace"]["rebuild"]["materialization"] == {
        "ready": True,
        "catalog_counts": {"apps": 3},
    }
    member_snapshot = compact["runtime"]["hub_member_connection_state"]["known_members"][0]["node_snapshot"]
    assert member_snapshot["desktop_catalog"] == {"apps_total": 1, "modal_total": 1, "captured_at": None}


def test_infrastate_reliability_summary_note_prefers_semantic_state_plane_contracts():
    mod = _load_infrastate_module()

    note = mod._reliability_summary_note(
        {
            "runtime": {
                "connectivity": {
                    "required_upstream_link": {
                        "kind": "hub_root",
                        "transport_state": "ready",
                        "transition_state": "waiting_restart",
                    },
                    "browser_control_route": {
                        "kind": "browser_control_route",
                        "transport_state": "degraded",
                        "transition_state": "reconnecting",
                    },
                },
                "state_sync": {
                    "semantic_state": "stale",
                    "freshness_state": "stale",
                    "first_sync_state": "timeout",
                    "replay": {"cursor": "3/32", "mode": "snapshot_plus_diff"},
                },
                "yjs_pressure": {
                    "policy_state": "throttle",
                    "observed_state": "critical",
                    "owner": "_by_owner/skill_infrastate_skill",
                },
            }
        },
        {},
    )

    assert "hub-root=ready/waiting_restart" in note
    assert "hub-root-browser=degraded/reconnecting" in note
    assert "upstream=hub_root:ready/waiting_restart" in note
    assert "state_sync=stale/stale" in note
    assert "first_sync=timeout" in note
    assert "yjs_pressure=throttle:critical" in note
    assert "yjs_owner=_by_owner/skill_infrastate_skill" in note


def test_infrastate_adaos_update_local_uses_shared_webspace_refresh(monkeypatch):
    mod = _load_infrastate_module()

    class _Repo:
        def get(self, name: str):
            return SimpleNamespace(version="1.2.3")

    class _ScenarioRepo:
        def get(self, name: str):
            return SimpleNamespace(version="4.5.6")

    class _Ctx:
        sql = object()
        git = object()
        paths = object()
        bus = None
        caps = object()
        settings = object()
        skills_repo = _Repo()
        scenarios_repo = _ScenarioRepo()

    class _SkillManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    refresh_calls: list[tuple[str, str]] = []
    scenario_capacity_calls: list[tuple[str, str, bool]] = []
    rebuild_calls: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(mod, "SkillManager", _SkillManager)
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: object())
    monkeypatch.setattr(
        mod,
        "sync_workspace_sparse_to_registry",
        lambda ctx: {"ok": True, "skills": ["weather_skill"], "scenarios": ["web_desktop"]},
    )
    monkeypatch.setattr(
        mod,
        "reconcile_workspace_db_to_materialized",
        lambda ctx: {"ok": True, "skills": ["weather_skill"], "scenarios": ["web_desktop"]},
    )
    monkeypatch.setattr(
        mod,
        "refresh_skill_runtime",
        lambda mgr, name, **kwargs: refresh_calls.append((name, str(kwargs.get("webspace_id") or ""))) or {
            "runtime_updated": True,
            "runtime_migrated": False,
        },
    )
    monkeypatch.setattr(
        mod,
        "install_scenario_in_capacity",
        lambda name, version, *, active=True, dev=False, base_dir=None: scenario_capacity_calls.append((name, version, active)),
    )
    monkeypatch.setattr(
        mod,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: rebuild_calls.append(dict(kwargs)) or {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )

    result = mod._adaos_update_local(dry_run=False)

    assert result["ok"] is True
    assert result["registry_reconciled"] is True
    assert result["runtime_updated"] == ["weather_skill"]
    assert result["scenario_capacity_updated"] == ["web_desktop"]
    expected_webspace_id = mod.default_webspace_id()
    assert refresh_calls == [("weather_skill", expected_webspace_id)]
    assert scenario_capacity_calls == [("web_desktop", "4.5.6", True)]
    assert rebuild_calls == [
        {
            "webspace_id": expected_webspace_id,
            "action": "infrastate_adaos_update_sync",
            "source_of_truth": "scenario_projection",
        }
    ]
    assert result["webspace_refresh"]["ok"] is True


def test_infrastate_remote_adaos_update_requests_member_snapshot_after_success(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    ui_updates: list[dict[str, object]] = []
    calls: list[tuple[str, str]] = []

    class _Manager:
        async def rpc_tools_call(self, node_id: str, *, tool: str, arguments: dict[str, object] | None, timeout: float | None, dev: bool):
            calls.append(("rpc", node_id))
            assert tool == "infrastate_skill:adaos_update"
            return {"ok": True, "updated": True}

        async def request_member_snapshot(self, node_id: str, reason: str) -> dict[str, object]:
            calls.append(("snapshot", f"{node_id}:{reason}"))
            return {"ok": True, "accepted": True}

    def _no_loop():
        raise RuntimeError("no running loop")

    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "member-1"})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setattr(mod.asyncio, "get_running_loop", _no_loop)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.subnet.link_manager",
        types.SimpleNamespace(get_hub_link_manager=lambda: _Manager()),
    )

    result = mod._perform_action("adaos_update", _Conf(), {})

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["snapshot_requested"] is True
    assert calls == [
        ("rpc", "member-1"),
        ("snapshot", "member-1:infrastate.adaos_update"),
    ]
    assert ui_updates[-1]["selected_node_id"] == "member-1"


def test_infrastate_schedule_snapshot_refresh_starts_thread_without_running_loop(monkeypatch):
    mod = _load_infrastate_module()

    calls: list[str] = []

    def _no_loop():
        raise RuntimeError("no running loop")

    monkeypatch.setattr(mod.asyncio, "get_running_loop", _no_loop)
    monkeypatch.setattr(mod, "_write_ui_state", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_push_infrastate_skill_context", lambda: False)
    monkeypatch.setattr(mod, "clear_current_skill", lambda: None)
    monkeypatch.setattr(mod, "_run_background_refresh_thread", lambda: calls.append("thread"))
    mod._background_refresh_task = None
    mod._background_refresh_thread = None
    mod._background_refresh_pending = False

    mod._schedule_snapshot_refresh(webspace_id="desktop", reason="test.no_loop")

    thread = mod._background_refresh_thread
    assert thread is not None
    thread.join(timeout=2.0)
    assert calls == ["thread"]
    assert mod._background_refresh_pending is True
    assert mod._background_refresh_webspace_id == "desktop"
    assert mod._background_refresh_reason == "test.no_loop"


def test_infrastate_schedule_snapshot_refresh_does_not_touch_memory_on_event_loop(monkeypatch):
    mod = _load_infrastate_module()

    def _forbidden_ui_write(**_kwargs):
        raise AssertionError("skill memory I/O ran while scheduling an event refresh")

    monkeypatch.setattr(mod, "_write_ui_state", _forbidden_ui_write)
    mod._background_refresh_task = None
    mod._background_refresh_thread = None
    mod._background_refresh_pending = False

    async def _run() -> None:
        mod._schedule_snapshot_refresh(webspace_id="desktop", reason="skills.activated")
        task = mod._background_refresh_task
        assert task is not None
        assert mod._projection_diag["last_refresh_scheduled_reason"] == "skills.activated"
        assert mod._projection_diag["last_refresh_scheduled_webspace_id"] == "desktop"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_infrastate_background_refresh_memory_io_does_not_block_event_loop(monkeypatch):
    mod = _load_infrastate_module()
    io_thread_ids: list[int] = []

    def _slow_ui_write(**_kwargs):
        io_thread_ids.append(threading.get_ident())
        time.sleep(0.2)

    async def _refresh(*, webspace_id=None, allow_cache=True):
        return {"ok": True, "webspace_id": webspace_id, "allow_cache": allow_cache}

    monkeypatch.setattr(mod, "_BACKGROUND_REFRESH_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(mod, "_write_ui_state", _slow_ui_write)
    monkeypatch.setattr(mod, "_refresh_snapshot_async", _refresh)
    mod._background_refresh_pending = True
    mod._background_refresh_reason = "test.non_blocking_io"
    mod._background_refresh_webspace_id = "desktop"
    mod._background_refresh_task = None

    async def _run() -> tuple[float, int]:
        loop_thread_id = threading.get_ident()
        started = time.monotonic()
        task = asyncio.create_task(mod._background_refresh_worker())
        await asyncio.sleep(0.02)
        tick_elapsed = time.monotonic() - started
        await task
        return tick_elapsed, loop_thread_id

    tick_elapsed, loop_thread_id = asyncio.run(_run())

    assert tick_elapsed < 0.12
    assert io_thread_ids
    assert all(thread_id != loop_thread_id for thread_id in io_thread_ids)


def test_infrastate_forget_subnet_clears_directory_and_requests_member_refresh(monkeypatch):
    mod = _load_infrastate_module()

    class _Directory:
        def __init__(self) -> None:
            self.cleared = False

        def list_known_nodes(self) -> list[dict[str, str]]:
            return [
                {"node_id": "member-1"},
                {"node_id": "member-2"},
            ]

        def clear_all(self) -> None:
            self.cleared = True

    class _Manager:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str]] = []

        def snapshot(self) -> dict[str, object]:
            return {"members": [{"node_id": "member-1"}]}

        async def request_member_snapshot(self, node_id: str, reason: str) -> None:
            self.requests.append((node_id, reason))

    directory = _Directory()
    manager = _Manager()

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub", subnet_id="sn-test"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.registry.subnet_directory",
        types.SimpleNamespace(get_directory=lambda: directory),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.subnet.link_manager",
        types.SimpleNamespace(get_hub_link_manager=lambda: manager),
    )

    result = mod._forget_subnet_local()

    assert directory.cleared is True
    assert result["ok"] is True
    assert result["forgotten_total"] == 2
    assert result["forgotten_node_ids"] == ["member-1", "member-2"]
    assert result["refresh_requested"] == 1
    assert manager.requests == [("member-1", "infrastate.forget_subnet")]


def test_infrastate_get_snapshot_ignores_legacy_full_snapshot_crashes(monkeypatch):
    mod = _load_infrastate_module()

    def _boom(*, webspace_id=None):
        raise UnboundLocalError("cannot access local variable 'sync_runtime' where it is not associated with a value")

    monkeypatch.setattr(mod, "_snapshot", _boom)
    monkeypatch.setattr(mod, "read_core_update_status", lambda: {"state": "idle"})
    monkeypatch.setattr(mod, "read_core_update_last_result", lambda: {})
    monkeypatch.setattr(mod, "slot_status", lambda: {})
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready"})
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub", node_id="hub-1"))
    monkeypatch.setattr(mod, "_should_project_snapshot_result", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(mod, "_highlight_summary_changes", lambda summary, **_kwargs: summary)
    monkeypatch.setattr(mod, "_event_state", lambda: [])

    snapshot = mod.get_snapshot(webspace_id="default", project=True)

    assert snapshot["ok"] is True
    assert snapshot["full_snapshot_removed"] is True
    assert snapshot["projection"] == "lightweight_control"
    assert snapshot["summary"]["value"] == "idle"


def test_infrastate_snapshot_tolerates_section_failures(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "_ensure_skill_data_projections", lambda: None)
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub", node_id="hub-1"))
    monkeypatch.setattr(mod, "read_core_update_status", lambda: {"state": "idle"})
    monkeypatch.setattr(mod, "read_core_update_last_result", lambda: {})
    monkeypatch.setattr(mod, "slot_status", lambda: {})
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready"})
    monkeypatch.setattr(mod, "_build_meta", lambda: {})
    monkeypatch.setattr(mod, "_effective_runtime_projection", lambda status, last_result, slots_payload, build: (slots_payload, build))
    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_reliability_snapshot", lambda conf, lifecycle: {"runtime": {}})
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_yjs_webspace_tabs", lambda conf, ui_state, reliability, selected_node: [])
    monkeypatch.setattr(mod, "_selected_node_editor", lambda conf, selected_node: {})
    monkeypatch.setattr(
        mod,
        "_selected_node_projection",
        lambda *args, **kwargs: {"status": {"state": "idle"}, "last_result": {}, "slots_payload": {}, "lifecycle": {}, "build": {}, "selected_member": {}},
    )
    monkeypatch.setattr(mod, "_transport_diag_snapshot", lambda: {})
    monkeypatch.setattr(mod, "_read_json", lambda path: {})
    monkeypatch.setattr(mod, "_effective_update_log_report", lambda report, last_result: {})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": [], "active": []})
    monkeypatch.setattr(mod, "_summary", lambda *args, **kwargs: {"label": "Infra State", "value": "ready"})
    monkeypatch.setattr(mod, "_action_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_core_action_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_yjs_action_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_update_actions", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_build_items", lambda build: (_ for _ in ()).throw(FileNotFoundError("missing build file")))
    monkeypatch.setattr(mod, "_step_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_realtime_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_slot_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_skills_items", lambda: (_ for _ in ()).throw(FileNotFoundError("missing workspace registry")))
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_marketplace_items", lambda webspace_id=None: (_ for _ in ()).throw(FileNotFoundError("missing marketplace source")))
    monkeypatch.setattr(mod, "_status_log_items", lambda report: [])
    monkeypatch.setattr(mod, "_event_state", lambda: [])

    snapshot = mod._snapshot()

    assert snapshot["summary"]["value"] == "ready"
    assert snapshot["build"] == []
    assert snapshot["skills"] == []
    assert snapshot["marketplace"] == {"skills": [], "scenarios": []}


def test_infrastate_snapshot_tolerates_bootstrap_file_not_found(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "_ensure_skill_data_projections", lambda: None)
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub", node_id="hub-1", node_names=["hub"]))
    monkeypatch.setattr(mod, "read_core_update_status", lambda: (_ for _ in ()).throw(FileNotFoundError("missing core status")))
    monkeypatch.setattr(mod, "read_core_update_last_result", lambda: (_ for _ in ()).throw(FileNotFoundError("missing core result")))
    monkeypatch.setattr(mod, "slot_status", lambda: (_ for _ in ()).throw(FileNotFoundError("missing slots")))
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: (_ for _ in ()).throw(FileNotFoundError("missing lifecycle")))
    monkeypatch.setattr(mod, "_build_meta", lambda: (_ for _ in ()).throw(FileNotFoundError("missing build meta")))
    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_reliability_snapshot", lambda conf, lifecycle: (_ for _ in ()).throw(FileNotFoundError("missing reliability")))
    monkeypatch.setattr(mod, "_transport_diag_snapshot", lambda: (_ for _ in ()).throw(FileNotFoundError("missing transport diag")))
    monkeypatch.setattr(mod, "_read_json", lambda path: (_ for _ in ()).throw(FileNotFoundError("missing report")))
    monkeypatch.setattr(mod, "_build_items", lambda build: [])
    monkeypatch.setattr(mod, "_step_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_realtime_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_slot_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_marketplace_items", lambda webspace_id=None: {"skills": [], "scenarios": []})
    monkeypatch.setattr(mod, "_status_log_items", lambda report: [])
    monkeypatch.setattr(mod, "_event_state", lambda: [])

    snapshot = mod._snapshot()

    assert snapshot.get("fallback") is not True
    assert snapshot["summary"]["label"] in {"Infra State", "Core update"}
    assert snapshot["skills"] == []
    assert snapshot["scenarios"] == []
    assert snapshot["marketplace"] == {"skills": [], "scenarios": []}


def test_infrastate_supervisor_transition_note_covers_root_promotion_and_restart():
    mod = _load_infrastate_module()

    pending = mod._supervisor_transition_note(
        {
            "state": "validated",
            "phase": "root_promotion_pending",
            "message": "validated slot is running; root promotion is pending",
        }
    )
    promoted = mod._supervisor_transition_note(
        {
            "state": "succeeded",
            "phase": "root_promoted",
            "message": "root bootstrap files promoted from validated slot; restart adaos.service to activate",
        }
    )

    assert pending["status"] == "warn"
    assert "root promotion" in pending["description"]
    assert promoted["status"] == "warn"
    assert "restart adaos.service" in promoted["description"]


def test_infrastate_supervisor_transition_note_covers_planned_and_subsequent_update():
    mod = _load_infrastate_module()

    planned = mod._supervisor_transition_note(
        {
            "state": "planned",
            "phase": "scheduled",
            "message": "core update deferred until minimum update interval elapses",
            "planned_reason": "minimum_update_period",
            "scheduled_for": time.time() + 300.0,
            "subsequent_transition": True,
        }
    )

    assert planned["status"] == "warn"
    assert "minimum update interval" in planned["description"]
    assert "subsequent transition queued" in planned["description"]


def test_infrastate_highlight_changed_summary_text_marks_only_changed_segments():
    mod = _load_infrastate_module()

    rendered = mod._highlight_changed_summary_text(
        "countdown completed | pending_acks=2 | protocol=degraded | action: cancel_update",
        "countdown completed | pending_acks=1 | protocol=degraded | action: start_update",
    )

    assert "countdown completed" in rendered
    assert "𝐩𝐞𝐧𝐝𝐢𝐧𝐠_𝐚𝐜𝐤𝐬=𝟐" in rendered
    assert "protocol=degraded" in rendered
    assert "𝐚𝐜𝐭𝐢𝐨𝐧: 𝐜𝐚𝐧𝐜𝐞𝐥_𝐮𝐩𝐝𝐚𝐭𝐞" in rendered


def test_infrastate_summary_highlights_against_previous_render(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")

    common_kwargs = dict(
        last_result={},
        slots_payload={"active_slot": "A"},
        lifecycle={},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={"runtime": {}},
        transport_diag={},
        selected_member=None,
    )

    first = mod._summary(
        status={"state": "countdown", "message": "countdown completed", "phase": "countdown"},
        **common_kwargs,
    )
    second = mod._summary(
        status={"state": "restarting", "message": "countdown completed | pending_acks=2", "phase": "shutdown"},
        **common_kwargs,
    )

    assert first["value"] == "countdown"
    assert second["value"] == "𝐫𝐞𝐬𝐭𝐚𝐫𝐭𝐢𝐧𝐠"
    assert "countdown completed" in second["description"]
    assert "𝐩𝐞𝐧𝐝𝐢𝐧𝐠_𝐚𝐜𝐤𝐬=𝟐" in second["description"]

def test_infrastate_idle_summary_does_not_surface_historical_failure(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")

    summary = mod._summary(
        status={"state": "idle", "message": "autostart runner boot"},
        last_result={
            "state": "failed",
            "phase": "prepare",
            "message": "core update slot preparation failed: very large traceback",
        },
        slots_payload={"active_slot": "A"},
        lifecycle={},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={"runtime": {}},
        transport_diag={},
        selected_member=None,
    )

    assert summary["value"] == "idle"
    assert summary["description"] == "autostart runner boot"
    assert "last=failed" not in summary["description"]
    assert "traceback" not in summary["description"]


def test_infrastate_summary_exposes_semantic_state_plane_contracts(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")

    summary = mod._summary(
        status={"state": "ready", "message": "ok", "phase": "validate"},
        last_result={},
        slots_payload={"active_slot": "A"},
        lifecycle={},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={
            "runtime": {
                "connectivity": {
                    "required_upstream_link": {"kind": "hub_root", "transport_state": "ready"},
                },
                "state_sync": {
                    "webspace_id": "desktop",
                    "semantic_state": "stale",
                },
                "yjs_pressure": {
                    "owner": "_by_owner/skill_infrastate_skill",
                    "policy_state": "block",
                },
            }
        },
        transport_diag={},
        selected_member=None,
    )

    assert summary["semantic_connectivity"]["required_upstream_link"]["kind"] == "hub_root"
    assert summary["semantic_state_sync"]["semantic_state"] == "stale"
    assert summary["semantic_yjs_pressure"]["policy_state"] == "block"


def test_infrastate_summary_uses_dev_slot_for_local_root_runtime(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")
    monkeypatch.setattr(mod, "_repo_root", lambda: Path("D:/git/adaos"))
    monkeypatch.setattr(mod.os, "getenv", lambda key, default=None: "dev" if key == "ENV_TYPE" else "")

    summary = mod._summary(
        status={"state": "ready", "message": "ok", "phase": "validate"},
        last_result={},
        slots_payload={"active_slot": "A"},
        lifecycle={},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={
            "runtime_mode": "dev",
            "runtime_build_version": "0.1.0+1.77fab7d",
            "runtime_base_version": "0.1.218",
            "runtime_git_short_commit": "77fab7d",
        },
        ui_state={},
        reliability={"runtime": {}},
        transport_diag={},
        selected_member=None,
    )

    assert summary["subtitle"] == "dev | 0.1.218 | 77fab7d"


def test_infrastate_summary_marks_disconnected_remote_node_offline(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(
        mod,
        "_node_tabs",
        lambda conf, ui_state, reliability: (
            [],
            {
                "kind": "member",
                "node_id": "member-1",
                "label": "Edge One",
                "node_compact_label": "E1",
            },
        ),
    )
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")
    monkeypatch.setattr(mod, "_remote_control_payload", lambda snapshot, member: {})

    summary = mod._summary(
        status={"state": "countdown", "message": "countdown active", "phase": "countdown"},
        last_result={},
        slots_payload={"active_slot": "A"},
        lifecycle={"node_state": "offline"},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={"runtime": {}},
        transport_diag={},
        selected_member={
            "connected": False,
            "state": "offline",
            "snapshot_state": "stale",
            "last_message_ago_s": 42,
            "last_seen_ago_s": 60,
        },
    )

    assert summary["value"] == "Offline"


def test_infrastate_summary_buttons_offer_defer_during_countdown():
    mod = _load_infrastate_module()

    buttons = mod._summary_buttons(
        {
            "state": "countdown",
            "phase": "countdown",
            "scheduled_for": time.time() + 60.0,
        }
    )

    button_ids = [str(item.get("id") or "") for item in buttons]
    assert "defer_update_5m" in button_ids
    assert "defer_update_15m" in button_ids


def test_infrastate_supervisor_transition_note_covers_planned_and_subsequent(monkeypatch):
    mod = _load_infrastate_module()
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)

    planned = mod._supervisor_transition_note(
        {
            "state": "planned",
            "phase": "scheduled",
            "message": "core update is scheduled",
            "planned_reason": "minimum_update_period",
            "scheduled_for": 400.0,
            "subsequent_transition": True,
        }
    )

    assert planned["status"] == "warn"
    assert "minimum update interval" in planned["description"]
    assert "subsequent transition queued" in planned["description"]


def test_infrastate_summary_buttons_include_defer_actions_for_planned(monkeypatch):
    mod = _load_infrastate_module()
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)

    buttons = mod._summary_buttons(
        {
            "state": "planned",
            "scheduled_for": 400.0,
        }
    )

    ids = [item["id"] for item in buttons]
    assert "defer_update_5m" in ids
    assert "defer_update_15m" in ids
    assert "cancel_update" in ids


def test_infrastate_step_items_include_supervisor_transition():
    mod = _load_infrastate_module()

    items = mod._step_items(
        {
            "state": "validated",
            "phase": "root_promotion_pending",
            "message": "validated slot is running; root promotion is pending",
            "target_rev": "rev2026",
        },
        {"active_slot": "A", "previous_slot": "B"},
        {"node_state": "ready", "reason": "runtime nominal"},
        {"version": "0.1.0+40.deadbee", "runtime_git_short_commit": "deadbee", "runtime_git_branch": "rev2026"},
    )

    supervisor_item = next(item for item in items if item["id"] == "supervisor_transition")
    assert supervisor_item["status"] == "warn"
    assert "root promotion" in supervisor_item["description"]


def test_infrastate_scenario_items_reconcile_sql_registry_to_local_runtime(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    alpha_dir = workspace / "scenarios" / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / "scenario.yaml").write_text("id: alpha\nversion: '1.2.3'\n", encoding="utf-8")
    beta_dir = workspace / "scenarios" / "beta"
    beta_dir.mkdir(parents=True, exist_ok=True)
    (beta_dir / "scenario.yaml").write_text("id: beta\nversion: '2.0.0'\n", encoding="utf-8")

    class _ScenarioRecord:
        def __init__(self, name: str, active_version: str, last_updated: float | None = None):
            self.name = name
            self.active_version = active_version
            self.last_updated = last_updated

    class _ScenarioRegistry:
        def __init__(self):
            self.rows = {
                "alpha": _ScenarioRecord("alpha", "1.0.0", 1.0),
                "gamma": _ScenarioRecord("gamma", "3.0.0", 2.0),
            }

        def list(self):
            return [self.rows[name] for name in sorted(self.rows)]

        def register(self, name: str, *, active_version: str | None = None, **_kwargs):
            self.rows[name] = _ScenarioRecord(name, active_version or "", 10.0)
            return self.rows[name]

        def unregister(self, name: str):
            self.rows.pop(name, None)

    scenario_registry = _ScenarioRegistry()
    reconcile_calls: list[Path] = []

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            git=object(),
        ),
    )
    monkeypatch.setattr(
        mod,
        "reconcile_workspace_db_to_materialized",
        lambda ctx: (
            reconcile_calls.append(Path(ctx.paths.workspace_dir())),
            scenario_registry.register("alpha", active_version="1.2.3"),
            scenario_registry.register("beta", active_version="2.0.0"),
            scenario_registry.unregister("gamma"),
            {"ok": True, "scenarios": ["alpha", "beta"], "scenarios_removed": ["gamma"]},
        )[-1],
    )
    monkeypatch.setattr(
        mod,
        "SqliteScenarioRegistry",
        lambda sql: scenario_registry,
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(
        mod,
        "get_local_capacity",
        lambda: {"scenarios": [{"name": "delta", "version": "4.0.0", "active": True, "updated_at": 4.0}]},
    )

    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", False)

    all_items = mod._scenario_items(include_all=True)
    assert reconcile_calls == [workspace]
    assert [(item["name"], item["version"]) for item in all_items] == [
        ("alpha", "1.2.3"),
        ("beta", "2.0.0"),
        ("delta", "4.0.0"),
    ]
    assert all_items[0]["workspace_source_version"] == "1.2.3"
    assert all_items[0]["active_version"] == "1.2.3"
    assert [(item["name"], item["workspace_display"]) for item in all_items] == [
        ("alpha", "1.2.3"),
        ("beta", "2.0.0"),
        ("delta", "missing"),
    ]
    assert all_items[2]["runtime_display"] == "4.0.0"
    assert all_items[2]["workspace_source_missing"] is True

    default_items = mod._scenario_items()
    assert reconcile_calls == [workspace]
    assert [(item["name"], item["version"]) for item in default_items] == [
        ("alpha", "1.2.3"),
        ("beta", "2.0.0"),
        ("delta", "4.0.0"),
    ]

    drift_items = mod._filter_inventory_drift(default_items, drift_only=True)
    assert [item["name"] for item in drift_items] == []


def test_infrastate_scenario_reconcile_skips_full_scan_when_names_match(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    scenario_dir = workspace / "scenarios" / "alpha"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "scenario.yaml").write_text("id: alpha\nversion: '1.0.0'\n", encoding="utf-8")
    row = SimpleNamespace(name="alpha", installed=True)

    monkeypatch.setattr(
        mod,
        "reconcile_workspace_db_to_materialized",
        lambda _ctx: (_ for _ in ()).throw(AssertionError("matching registry must avoid a full scan")),
    )

    reconciled = mod._reconcile_scenario_registry_if_due(SimpleNamespace(), workspace, [row])

    assert reconciled is False


def test_infrastate_scenario_items_surface_dependency_failures_for_active_scenarios(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    for name in ("alpha", "beta"):
        scenario_dir = workspace / "scenarios" / name
        scenario_dir.mkdir(parents=True, exist_ok=True)
        (scenario_dir / "scenario.yaml").write_text(f"id: {name}\nversion: '1.0.0'\n", encoding="utf-8")

    class _ScenarioRecord:
        def __init__(self, name: str, active_version: str):
            self.name = name
            self.active_version = active_version
            self.last_updated = 1.0

    dependency_bootstrap = {
        "ok": False,
        "failed": ["bad_skill"],
        "items": [{"name": "bad_skill", "ok": False, "error": "prepare failed"}],
        "error": "RuntimeError: prepare failed",
    }
    operations = {
        "by_id": {
            "op-alpha": {
                "operation_id": "op-alpha",
                "kind": "scenario.update",
                "target_kind": "scenario",
                "target_id": "alpha",
                "status": "failed",
                "error": {
                    "type": "ScenarioDependencyLifecycleError",
                    "message": "required scenario dependencies failed: bad_skill",
                    "dependency_bootstrap": dependency_bootstrap,
                },
            },
            "op-beta": {
                "operation_id": "op-beta",
                "kind": "scenario.update",
                "target_kind": "scenario",
                "target_id": "beta",
                "status": "failed",
                "error": {
                    "type": "ScenarioDependencyLifecycleError",
                    "dependency_bootstrap": dependency_bootstrap,
                },
            },
        }
    }

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(sql=object(), paths=SimpleNamespace(workspace_dir=lambda: workspace), git=object()),
    )
    monkeypatch.setattr(
        mod,
        "SqliteScenarioRegistry",
        lambda sql: SimpleNamespace(
            list=lambda: [
                _ScenarioRecord("alpha", "1.0.0"),
                _ScenarioRecord("beta", ""),
            ]
        ),
    )
    monkeypatch.setattr(mod, "get_local_capacity", lambda: {"scenarios": []})
    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", False)

    items = {item["name"]: item for item in mod._scenario_items(include_all=True, operations=operations)}

    assert items["alpha"]["dependency_lifecycle_failed"] is True
    assert items["alpha"]["dependency_failure_operation_id"] == "op-alpha"
    assert items["alpha"]["dependency_failure_failed"] == ["bad_skill"]
    assert items["alpha"]["status"] == "dependency_lifecycle_failed"
    assert items["alpha"]["status_icon"] == "warning-outline"
    assert "bad_skill" in items["alpha"]["status_tooltip"]
    assert items["beta"]["dependency_lifecycle_failed"] is True
    assert items["beta"]["dependency_failure_operation_id"] == "op-beta"
    assert items["beta"]["status"] == "dependency_lifecycle_failed"


def test_infrastate_inventory_stream_honors_drift_only_toggle(monkeypatch):
    mod = _load_infrastate_module()
    rows = [
        {"name": "aligned", "has_drift": False},
        {"name": "behind", "has_drift": True},
    ]
    monkeypatch.setattr(mod, "_skills_items", lambda *, include_all=True: list(rows))
    monkeypatch.setattr(mod, "_ui_state", lambda: {"inventory_drift_only": False})

    all_rows = mod._build_stream_payload_for_receiver(mod._skills_receiver())
    assert [item["name"] for item in all_rows] == ["aligned", "behind"]

    monkeypatch.setattr(mod, "_ui_state", lambda: {"inventory_drift_only": True})
    drift_rows = mod._build_stream_payload_for_receiver(mod._skills_receiver())
    assert [item["name"] for item in drift_rows] == ["behind"]


def test_infrastate_inventory_stream_honors_explicit_selected_node(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="hub-1"))
    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "hub-1"})
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: {})
    monkeypatch.setattr(mod, "_lightweight_member_reliability", lambda conf, lifecycle=None: {"members": []})
    monkeypatch.setattr(mod, "_selected_member_entry", lambda reliability, node_id: {"node_id": node_id})
    monkeypatch.setattr(
        mod,
        "_remote_capacity_inventory_items",
        lambda member, kind: [{"name": f"{member['node_id']}-{kind}"}],
    )

    rows = mod._build_stream_payload_for_receiver(
        mod._skills_receiver(),
        "desktop",
        selected_node_id="member-1",
    )

    assert rows == [{"name": "member-1-skills"}]


def test_infrastate_catalog_record_exposes_source_and_commit(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    mod._registry_catalog_cache.clear()
    mod._registry_catalog_meta_cache.clear()

    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(mod, "_allow_marketplace_remote_fetch", lambda: False)
    monkeypatch.setattr(mod, "_allow_marketplace_git_ref_lookup", lambda: True)
    monkeypatch.setattr(
        mod,
        "_registry_payload_from_git_ref",
        lambda workspace_root: {"skills": [{"id": "demo_skill", "version": "1.2.3"}]},
    )
    monkeypatch.setattr(mod, "_registry_ref_config", lambda: ("origin", "main"))
    monkeypatch.setattr(mod, "_registry_git_ref_commit", lambda workspace_root, *, remote, branch: "abc123")
    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )

    record = mod._read_catalog_record(kind_plural="skills", artifact_id="demo_skill")

    assert record == {
        "version": "1.2.3",
        "catalog_source": "git_ref:origin/main",
        "catalog_commit": "abc123",
        "catalog_state": "available",
    }


def test_infrastate_catalog_record_matches_scenario_by_name_when_manifest_id_differs(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    mod._registry_catalog_cache.clear()
    mod._registry_catalog_meta_cache.clear()

    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(mod, "_allow_marketplace_remote_fetch", lambda: False)
    monkeypatch.setattr(mod, "_allow_marketplace_git_ref_lookup", lambda: True)
    monkeypatch.setattr(
        mod,
        "_registry_payload_from_git_ref",
        lambda workspace_root: {
            "scenarios": [
                {
                    "id": "new_face_vision_scenario",
                    "name": "new_face_vision",
                    "manifest_id": "new_face_vision_scenario",
                    "path": "scenarios/new_face_vision",
                    "install": {"name": "new_face_vision", "id": "new_face_vision_scenario"},
                    "version": "0.2.15",
                }
            ]
        },
    )
    monkeypatch.setattr(mod, "_registry_ref_config", lambda: ("origin", "main"))
    monkeypatch.setattr(mod, "_registry_git_ref_commit", lambda workspace_root, *, remote, branch: "abc123")
    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )

    record = mod._read_catalog_record(kind_plural="scenarios", artifact_id="new_face_vision")

    assert record == {
        "version": "0.2.15",
        "catalog_source": "git_ref:origin/main",
        "catalog_commit": "abc123",
        "catalog_state": "available",
    }


def test_infrastate_catalog_record_reports_no_git(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    mod._registry_catalog_cache.clear()
    mod._registry_catalog_meta_cache.clear()

    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(mod, "_allow_marketplace_remote_fetch", lambda: False)
    monkeypatch.setattr(mod, "_allow_marketplace_git_ref_lookup", lambda: True)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod, "_marketplace_catalog_entries", lambda kind: [])
    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )

    record = mod._read_catalog_record(kind_plural="skills", artifact_id="demo_skill")

    assert record["version"] == ""
    assert record["catalog_source"] == "no_git"
    assert record["catalog_state"] == "no_git"


def test_infrastate_version_status_marks_catalog_unknown_and_no_git():
    mod = _load_infrastate_module()

    unknown = mod._version_status(
        artifact_kind="skill",
        catalog_version="",
        catalog_source="git_ref:origin/main",
        catalog_state="unknown",
        workspace_source_version="1.0.0",
        active_version="1.0.0",
        active=True,
    )
    no_git = mod._version_status(
        artifact_kind="skill",
        catalog_version="",
        catalog_source="no_git",
        catalog_state="no_git",
        workspace_source_version="1.0.0",
        active_version="1.0.0",
        active=True,
    )

    assert unknown["status"] == "catalog_unknown"
    assert unknown["has_drift"] is True
    assert no_git["status"] == "git_unavailable"
    assert no_git["has_drift"] is True


def test_infrastate_skill_items_use_registry_and_workspace_versions(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: infrastate_skill\nversion: '0.19.0'\n", encoding="utf-8")
    extra_dir = workspace / "skills" / "extra_skill"
    extra_dir.mkdir(parents=True, exist_ok=True)
    (extra_dir / "skill.yaml").write_text("id: extra_skill\nversion: '9.9.9'\n", encoding="utf-8")

    class _SkillRecord:
        def __init__(self, name: str, active_version: str, installed: bool = True):
            self.name = name
            self.active_version = active_version
            self.installed = installed

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            bus=None,
            caps=object(),
            settings=object(),
            skills_repo=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: [_SkillRecord("infrastate_skill", "0.18.0")]))
    monkeypatch.setattr(
        mod,
        "SkillManager",
        lambda **kwargs: SimpleNamespace(
            runtime_status=lambda name: {"active_slot": "A", "version": "0.19.0", "runtime_bucket": "v0.19"}
        ),
    )
    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(
        mod,
        "_registry_json_catalog_entries",
        lambda kind, workspace_root: [{"id": "infrastate_skill", "name": "infrastate_skill", "version": "0.20.0"}]
        if kind == "skills"
        else [],
    )
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [{"id": "infrastate_skill", "name": "infrastate_skill", "version": "0.20.0"}] if kind == "skills" else [],
    )

    items = mod._skills_items()

    assert len(items) == 1
    item = items[0]
    assert item["name"] == "infrastate_skill"
    assert item["version"] == "0.19.0"
    assert item["active_version"] == "0.19.0"
    assert item["workspace_source_version"] == "0.19.0"
    assert item["catalog_version"] == "0.20.0"
    assert item["catalog_source"] == "registry_json"
    assert item["catalog_commit"] == ""
    assert item["catalog_state"] == "available"
    assert item["runtime_bucket"] == "v0.19"
    assert item["version_display"] == "0.19.0* (0.20.0)"
    assert item["slot"] == "A"
    assert item["remote_version"] == "0.20.0"
    assert item["update_available"] is True
    assert item["registry_mismatch"] is True
    assert item["has_drift"] is True
    assert item["can_activate"] is False
    assert item["status"] == "behind_catalog"
    assert item["status_icon"] == "cloud-download-outline"
    assert "behind catalog" in item["status_tooltip"]


def test_infrastate_skill_items_compare_remote_against_active_runtime_version(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: infrastate_skill\nversion: '0.20.0'\n", encoding="utf-8")

    class _SkillRecord:
        name = "infrastate_skill"
        active_version = "0.19.0"
        installed = True

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            bus=None,
            caps=object(),
            settings=object(),
            skills_repo=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: [_SkillRecord()]))
    monkeypatch.setattr(
        mod,
        "SkillManager",
        lambda **kwargs: SimpleNamespace(
            runtime_status=lambda name: {"active_slot": "A", "version": "0.19.0", "runtime_bucket": "v0.19"}
        ),
    )
    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(
        mod,
        "_registry_json_catalog_entries",
        lambda kind, workspace_root: [{"id": "infrastate_skill", "name": "infrastate_skill", "version": "0.20.0"}]
        if kind == "skills"
        else [],
    )

    items = mod._skills_items()

    assert items[0]["version"] == "0.19.0"
    assert items[0]["remote_version"] == "0.20.0"
    assert items[0]["active_version"] == "0.19.0"
    assert items[0]["workspace_source_version"] == "0.20.0"
    assert items[0]["catalog_version"] == "0.20.0"
    assert items[0]["catalog_source"] == "registry_json"
    assert items[0]["catalog_state"] == "available"
    assert items[0]["runtime_bucket"] == "v0.19"
    assert items[0]["version_display"] == "0.19.0* (0.20.0)"
    assert items[0]["registry_mismatch"] is True
    assert items[0]["has_drift"] is True
    assert items[0]["can_activate"] is True


def test_infrastate_skill_items_skip_remote_version_probe_when_disabled(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: infrastate_skill\nversion: '0.19.0'\n", encoding="utf-8")

    class _SkillRecord:
        def __init__(self, name: str, active_version: str):
            self.name = name
            self.active_version = active_version

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            bus=None,
            caps=object(),
            settings=object(),
            skills_repo=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: [_SkillRecord("infrastate_skill", "0.18.0")]))
    monkeypatch.setattr(mod, "SkillManager", lambda **kwargs: SimpleNamespace(runtime_status=lambda name: {"active_slot": "A"}))
    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", False)
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (_ for _ in ()).throw(AssertionError("remote registry probe should be skipped when disabled")),
    )

    items = mod._skills_items()

    assert items[0]["remote_version"] == ""
    assert items[0]["update_available"] is False
    assert items[0]["registry_mismatch"] is False
    assert items[0]["version_display"] == "0.18.0"
    assert items[0]["workspace_source_version"] == "0.19.0"
    assert items[0]["has_drift"] is True
    assert items[0]["status"] == "workspace_runtime_differs"


def test_infrastate_marketplace_catalog_skips_remote_url_fetch_on_member(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="member"))
    monkeypatch.setattr(mod, "list_workspace_registry_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        mod,
        "rebuild_workspace_registry",
        lambda workspace_root: {
            "skills": [
                {"kind": "skill", "id": "local_skill", "name": "local_skill", "version": "0.1.0"},
            ]
        },
    )
    called = {"url": 0, "git": 0}

    def _fail_url():
        called["url"] += 1
        raise AssertionError("member snapshot path should not hit marketplace URL fetch")

    def _fail_git(*args, **kwargs):
        called["git"] += 1
        raise AssertionError("member snapshot path should not hit git ref registry lookup")

    monkeypatch.setattr(mod, "_registry_payload_from_url", _fail_url)
    monkeypatch.setattr(mod.subprocess, "run", _fail_git)

    items = mod._marketplace_catalog_entries("skills")

    assert [item["name"] for item in items] == ["local_skill"]
    assert called == {"url": 0, "git": 0}


def test_infrastate_adaos_update_uses_union_sparse_sync_and_installed_skill_names(monkeypatch):
    mod = _load_infrastate_module()
    runtime_updates: list[str] = []
    scenario_capacity_updates: list[tuple[str, str]] = []
    rebuild_calls: list[dict[str, object]] = []

    ctx = SimpleNamespace(
        sql=object(),
        git=object(),
        paths=SimpleNamespace(workspace_dir=lambda: Path(".")),
        bus=None,
        caps=object(),
        settings=object(),
        skills_repo=object(),
        scenarios_repo=object(),
    )

    monkeypatch.setattr(mod, "get_ctx", lambda: ctx)
    monkeypatch.setattr(
        mod,
        "sync_workspace_sparse_to_registry",
        lambda current_ctx: {"ok": True, "skills": ["installed_skill"], "scenarios": ["scene_one"], "fallback_used": {}},
    )
    monkeypatch.setattr(
        mod,
        "reconcile_workspace_db_to_materialized",
        lambda current_ctx: {"ok": True, "skills": ["installed_skill"], "scenarios": ["scene_one"]},
    )
    monkeypatch.setattr(
        mod,
        "SkillManager",
        lambda **kwargs: SimpleNamespace(runtime_update=lambda name, space="workspace": runtime_updates.append(name) or {"ok": True}),
    )
    monkeypatch.setattr(
        mod,
        "refresh_skill_runtime",
        lambda mgr, name, **kwargs: runtime_updates.append(name) or {
            "runtime_updated": True,
            "runtime_migrated": False,
        },
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: object())
    monkeypatch.setattr(
        mod,
        "install_scenario_in_capacity",
        lambda name, version, *, active=True, dev=False, base_dir=None: scenario_capacity_updates.append((name, version)),
    )
    monkeypatch.setattr(
        mod,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: rebuild_calls.append(dict(kwargs)) or {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )

    result = mod._adaos_update_local()

    assert result["ok"] is True
    assert result["skills_synced"] is True
    assert result["scenarios_synced"] is True
    assert result["registry_reconciled"] is True
    assert result["skills"] == ["installed_skill"]
    assert result["scenarios"] == ["scene_one"]
    assert result["scenario_capacity_updated"] == ["scene_one"]
    assert runtime_updates == ["installed_skill"]
    assert scenario_capacity_updates == [("scene_one", "unknown")]
    assert result["webspace_refresh"]["ok"] is True
    assert rebuild_calls == [
        {
            "webspace_id": mod.default_webspace_id(),
            "action": "infrastate_adaos_update_sync",
            "source_of_truth": "scenario_projection",
        }
    ]


def test_infrastate_marketplace_filters_installed_and_marks_running_operations(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (
            [
                {"kind": "skill", "id": "installed_skill", "name": "installed_skill", "version": "1.0.0"},
                {"kind": "skill", "id": "queued_skill", "name": "queued_skill", "version": "1.2.0"},
            ]
            if kind == "skills"
            else [{"kind": "scenario", "id": "new_scene", "name": "new_scene", "version": "0.5.0"}]
        ),
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [{"name": "installed_skill", "version": "1.0.0", "slot": "A"}])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(
        mod,
        "get_operation_manager",
        lambda: SimpleNamespace(
            snapshot=lambda webspace_id=None: {
                "active_items": [
                    {
                        "target_kind": "skill",
                        "target_id": "queued_skill",
                        "status": "running",
                        "current_step": "skill.install",
                    }
                ]
            }
        ),
    )

    items = mod._marketplace_items(webspace_id="default")

    assert [item["id"] for item in items["skills"]] == ["queued_skill"]
    assert items["skills"][0]["install_disabled"] is True
    assert items["skills"][0]["operation_status"] == "running"
    assert [item["id"] for item in items["scenarios"]] == ["new_scene"]


def test_infrastate_marketplace_catalog_prefers_remote_registry_and_local_scan(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    scenario_dir = workspace / "scenarios" / "infrascope"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "scenario.yaml").write_text(
        "\n".join(
            [
                "id: infrascope",
                "name: Infrascope",
                "version: '0.2.0'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(mod, "_registry_payload_from_url", lambda: None)
    monkeypatch.setattr(mod, "list_workspace_registry_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"scenarios":[{"kind":"scenario","id":"remote_scene","name":"remote_scene","version":"1.0.0"}]}',
            stderr="",
        ),
    )

    items = mod._marketplace_catalog_entries("scenarios")

    assert [item["name"] for item in items] == ["infrascope", "remote_scene"]


def test_infrastate_marketplace_catalog_skips_scan_when_workspace_registry_exists(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="member"))
    monkeypatch.setattr(
        mod,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [
            {"kind": "scenario", "id": "media_center", "name": "media_center", "version": "0.4.0"}
        ],
    )
    monkeypatch.setattr(
        mod,
        "rebuild_workspace_registry",
        lambda _workspace_root: (_ for _ in ()).throw(AssertionError("existing registry must avoid a full scan")),
    )

    items = mod._marketplace_catalog_entries("scenarios")

    assert [item["name"] for item in items] == ["media_center"]


def test_infrastate_marketplace_catalog_uses_ttl_cache(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    calls = {"git": 0, "scan": 0}

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(mod, "_registry_payload_from_url", lambda: None)
    monkeypatch.setattr(mod, "list_workspace_registry_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_MARKETPLACE_CACHE_TTL_S", 30.0)
    mod._marketplace_catalog_cache.clear()

    def _fake_git_run(*args, **kwargs):
        calls["git"] += 1
        return SimpleNamespace(
            returncode=0,
            stdout='{"skills":[{"kind":"skill","id":"remote_skill","name":"remote_skill","version":"1.0.0"}]}',
            stderr="",
        )

    def _fake_rebuild(workspace_root):
        calls["scan"] += 1
        return {"skills": [{"kind": "skill", "id": "local_skill", "name": "local_skill", "version": "0.1.0"}]}

    monkeypatch.setattr(mod.subprocess, "run", _fake_git_run)
    monkeypatch.setattr(mod, "rebuild_workspace_registry", _fake_rebuild)

    first = mod._marketplace_catalog_entries("skills")
    second = mod._marketplace_catalog_entries("skills")

    assert [item["name"] for item in first] == ["local_skill", "remote_skill"]
    assert [item["name"] for item in second] == ["local_skill", "remote_skill"]
    assert calls == {"git": 2, "scan": 1}


def test_infrastate_registry_payload_cache_is_shared_across_artifact_kinds(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    calls = {"remote": 0}

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(mod, "_MARKETPLACE_CACHE_TTL_S", 30.0)

    def _remote_payload():
        calls["remote"] += 1
        return {
            "skills": [{"id": "weather_skill"}],
            "scenarios": [{"id": "media_center"}],
        }

    monkeypatch.setattr(mod, "_registry_payload_from_url", _remote_payload)

    skills = mod._registry_json_catalog_entries("skills", workspace)
    scenarios = mod._registry_json_catalog_entries("scenarios", workspace)

    assert [item["id"] for item in skills] == ["weather_skill"]
    assert [item["id"] for item in scenarios] == ["media_center"]
    assert calls == {"remote": 1}


def test_infrastate_realtime_items_include_semantic_state_plane_cards():
    mod = _load_infrastate_module()

    items = mod._realtime_items(
        {
            "runtime": {
                "connectivity": {
                    "required_upstream_link": {
                        "kind": "hub_root",
                        "transport_state": "ready",
                        "transition_state": "waiting_restart",
                        "served_by": "supervisor",
                    },
                    "browser_control_route": {
                        "transport_state": "degraded",
                        "transition_state": "reconnecting",
                        "blockers": ["route.flapping"],
                    },
                },
                "state_sync": {
                    "webspace_id": "desktop",
                    "transport_state": "attached",
                    "first_sync_state": "timeout",
                    "semantic_state": "stale",
                    "freshness_state": "stale",
                    "fallback_mode": "hard_degraded_recovery",
                    "replay": {"cursor": "3/32", "mode": "snapshot_plus_diff"},
                },
                "yjs_pressure": {
                    "owner": "_by_owner/skill_infrastate_skill",
                    "policy_state": "throttle",
                    "observed_state": "critical",
                    "recent_bytes": 167296,
                    "recent_writes": 2,
                    "peak_bps": 167296.0,
                    "peak_wps": 2.0,
                    "reason": "write_amplification",
                    "throttled_total": 4,
                    "blocked_total": 1,
                    "last_policy_state": "block",
                    "last_reason": "write_amplification_blocked",
                },
            }
        },
        {},
    )

    by_id = {str(item.get("id") or ""): item for item in items}
    assert "semantic_connectivity" in by_id
    assert "semantic_state_sync" in by_id
    assert "semantic_yjs_pressure" in by_id
    assert "upstream=hub_root:ready/waiting_restart" in str(by_id["semantic_connectivity"]["description"])
    assert "semantic=stale" in str(by_id["semantic_state_sync"]["description"])
    assert "policy=throttle" in str(by_id["semantic_yjs_pressure"]["description"])
    assert "throttled=4" in str(by_id["semantic_yjs_pressure"]["description"])
    assert "blocked=1" in str(by_id["semantic_yjs_pressure"]["description"])
    assert "last=block:write_amplification_blocked" in str(by_id["semantic_yjs_pressure"]["subtitle"])


def test_infrastate_project_async_skips_snapshot_with_only_timestamp_changes(monkeypatch):
    mod = _load_infrastate_module()
    applied: list[tuple[str | None, str, object]] = []
    _enable_projection_demand(mod, "infrastate.summary")
    mod._projection_fingerprints.clear()
    mod._projection_diag.update({"apply_total": 0, "skip_total": 0, "cache_hit_total": 0})

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        applied.append((webspace_id, slot, value))

    monkeypatch.setattr(mod._WEBSPACE_PROJECTION_CTX, "set_async", _fake_set_async)
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(mod, "_publish_snapshot_streams", lambda snapshot, webspace_id=None: None)

    first = {
        "summary": {"value": "ready", "updated_at": 10.0},
        "projection_diag": {"apply_total": 0},
        "last_refresh_ts": 10.0,
        "events": [],
    }
    second = {
        "summary": {"value": "ready", "updated_at": 11.0},
        "projection_diag": {"apply_total": 999},
        "last_refresh_ts": 11.0,
        "events": [],
    }

    asyncio.run(mod._project_async(first, webspace_id="default"))
    asyncio.run(mod._project_async(second, webspace_id="default"))

    assert applied == [("default", "infrastate.summary", {"value": "ready"})]
    assert mod._projection_diag["apply_total"] == 1
    assert mod._projection_diag["skip_total"] == 1


def test_infrastate_project_async_uses_throttled_interval_when_yjs_policy_requires(monkeypatch):
    mod = _load_infrastate_module()
    applied: list[tuple[str | None, str, object]] = []
    _enable_projection_demand(mod, "infrastate.summary")
    mod._projection_fingerprints.clear()
    mod._projection_last_applied_at.clear()
    mod._projection_diag.update(
        {
            "apply_total": 0,
            "skip_total": 0,
            "cache_hit_total": 0,
            "rate_limited_total": 0,
            "last_policy_state": "",
            "last_policy_owner": "",
            "last_policy_observed_state": "",
            "last_policy_webspace_id": "",
            "last_policy_throttled_roots": [],
        }
    )

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        applied.append((webspace_id, slot, value))

    monkeypatch.setattr(mod._WEBSPACE_PROJECTION_CTX, "set_async", _fake_set_async)
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(mod, "_publish_snapshot_streams", lambda snapshot, webspace_id=None: None)
    monkeypatch.setattr(
        mod,
        "_projection_pressure_policy",
        lambda webspace_id=None: {"policy_state": "throttle", "observed_state": "critical", "throttled_roots": ["data"]},
    )
    monkeypatch.setattr(mod, "_MIN_YJS_PROJECTION_INTERVAL_S", 0.1)
    monkeypatch.setattr(mod, "_THROTTLED_YJS_PROJECTION_INTERVAL_S", 2.0)

    first = {"summary": {"value": "ready-1"}}
    second = {"summary": {"value": "ready-2"}}

    asyncio.run(mod._project_async(first, webspace_id="default"))
    asyncio.run(mod._project_async(second, webspace_id="default"))

    assert applied == [("default", "infrastate.summary", {"value": "ready-1"})]
    assert mod._projection_diag["apply_total"] == 1
    assert mod._projection_diag["rate_limited_total"] == 1


def test_infrastate_project_async_blocks_primary_yjs_projection_but_keeps_streams(monkeypatch):
    mod = _load_infrastate_module()
    applied: list[tuple[str | None, str]] = []
    published: list[str] = []
    _enable_projection_demand(mod, "infrastate.summary")
    mod._projection_fingerprints.clear()
    mod._projection_last_applied_at.clear()
    mod._projection_diag.update(
        {
            "apply_total": 0,
            "skip_total": 0,
            "cache_hit_total": 0,
            "rate_limited_total": 0,
            "blocked_total": 0,
            "last_policy_state": "",
            "last_policy_owner": "",
            "last_policy_observed_state": "",
            "last_policy_webspace_id": "",
            "last_policy_throttled_roots": [],
            "last_policy_blocked_roots": [],
            "last_policy_reason": "",
        }
    )

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        applied.append((webspace_id, str(value.get("summary", {}).get("value") or "")))

    monkeypatch.setattr(mod._WEBSPACE_PROJECTION_CTX, "set_async", _fake_set_async)
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(
        mod,
        "_publish_snapshot_streams",
        lambda snapshot, webspace_id=None: published.append(str(snapshot.get("summary", {}).get("value") or "")),
    )
    monkeypatch.setattr(
        mod,
        "_projection_pressure_policy",
        lambda webspace_id=None: {
            "policy_state": "block",
            "observed_state": "critical",
            "blocked_roots": ["data"],
            "reason": "write_amplification_blocked",
        },
    )

    asyncio.run(mod._project_async({"summary": {"value": "ready"}}, webspace_id="default"))

    assert applied == []
    assert published == ["ready"]
    assert mod._projection_diag["blocked_total"] == 1


def test_infrastate_get_snapshot_project_false_does_not_project_control(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "_lightweight_projection_sections",
        lambda **_kwargs: {
            "infrastate.summary": {"value": "degraded"},
            "infrastate.projection_diag": {},
        },
    )
    monkeypatch.setattr(
        mod,
        "_project_sections_async",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("project should not run")),
    )

    result = mod.get_snapshot(webspace_id="desktop", project=False)

    assert result["summary"]["value"] == "degraded"


def test_infrastate_get_snapshot_allows_compact_projection_under_yjs_throttle(monkeypatch):
    mod = _load_infrastate_module()
    projected: list[str | None] = []
    mod._projection_diag.update(
        {
            "tool_project_admitted_under_pressure_total": 0,
            "tool_project_suppressed_total": 0,
            "last_tool_project_suppressed_reason": "",
        }
    )

    monkeypatch.setattr(
        mod,
        "_lightweight_projection_sections",
        lambda **_kwargs: {
            "infrastate.summary": {"value": "succeeded", "subtitle": "slot B | abc123"},
            "infrastate.actions": [{"id": "refresh", "title": "Refresh"}],
        },
    )
    monkeypatch.setattr(
        mod,
        "_projection_pressure_policy",
        lambda webspace_id=None: {
            "policy_state": "throttle",
            "observed_state": "critical",
            "throttled_roots": ["data"],
            "reason": "write_amplification",
        },
    )

    async def _project_sections(sections, webspace_id=None, reason=""):
        projected.append(webspace_id)

    monkeypatch.setattr(mod, "_project_sections_async", _project_sections)

    result = mod.get_snapshot(webspace_id="desktop", project=True)

    assert projected == ["desktop"]
    assert result["summary"]["value"] == "succeeded"
    assert "logs" not in result
    assert mod._projection_diag["tool_project_admitted_under_pressure_total"] == 1
    assert mod._projection_diag["tool_project_suppressed_total"] == 0


def test_infrastate_get_snapshot_suppresses_compact_projection_when_yjs_blocked(monkeypatch):
    mod = _load_infrastate_module()
    mod._projection_diag.update(
        {
            "tool_project_admitted_under_pressure_total": 0,
            "tool_project_suppressed_total": 0,
            "last_tool_project_suppressed_reason": "",
            "last_tool_project_suppressed_policy_state": "",
            "last_tool_project_suppressed_observed_state": "",
        }
    )

    monkeypatch.setattr(
        mod,
        "_lightweight_projection_sections",
        lambda **_kwargs: {"infrastate.summary": {"value": "succeeded"}},
    )
    monkeypatch.setattr(
        mod,
        "_projection_pressure_policy",
        lambda webspace_id=None: {
            "policy_state": "block",
            "observed_state": "critical",
            "blocked_roots": ["data"],
            "reason": "quarantined",
        },
    )
    monkeypatch.setattr(
        mod,
        "_project_sections_async",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked YJS projection should not run")),
    )

    result = mod.get_snapshot(webspace_id="desktop", project=True)

    assert result["summary"]["value"] == "succeeded"
    assert mod._projection_diag["tool_project_suppressed_total"] == 1
    assert mod._projection_diag["last_tool_project_suppressed_reason"] == "tool.get_snapshot.lightweight"
    assert mod._projection_diag["last_tool_project_suppressed_policy_state"] == "block"
    assert mod._projection_diag["tool_project_admitted_under_pressure_total"] == 0


def test_infrastate_project_async_excludes_stream_sections_from_yjs(monkeypatch):
    mod = _load_infrastate_module()
    projected: list[tuple[str, object]] = []
    published: list[tuple[str, object, str | None]] = []
    _enable_projection_demand(mod, "infrastate.summary", "infrastate.operations.active")
    mod._projection_fingerprints.clear()
    mod._projection_diag.update({"apply_total": 0, "skip_total": 0, "cache_hit_total": 0})

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        projected.append((slot, value))

    monkeypatch.setattr(mod._WEBSPACE_PROJECTION_CTX, "set_async", _fake_set_async)
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(
        mod,
        "_publish_stream_payload",
        lambda *, receiver, data, webspace_id=None, force=False, node_id=None: published.append((receiver, data, webspace_id)),
    )

    snapshot = {
        "summary": {"value": "ready"},
        "operations": {"items": [{"id": "op-1"}], "active": [{"id": "op-1"}]},
        "logs": [{"id": "log-1"}],
        "events": [{"id": "evt-1"}],
        "yjs_runtime": {"load_mark": {"selected_webspace": {"items": [{"root": "data"}]}}},
    }

    asyncio.run(mod._project_async(snapshot, webspace_id="default"))

    assert projected == [
        ("infrastate.summary", {"value": "ready"}),
        ("infrastate.operations.active", [{"id": "op-1"}]),
    ]
    assert published == []

    mod._remember_stream_receiver("default", mod._operations_receiver())
    asyncio.run(mod._project_async(snapshot, webspace_id="default"))

    assert published == [
        ("infrastate.operations.active", [{"id": "op-1"}], "default"),
    ]


def test_infrastate_webspace_reload_invalidates_projection_and_stream_state(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[dict[str, object]] = []

    mod._projection_fingerprints.clear()
    mod._projection_last_applied_at.clear()
    mod._stream_fingerprints.clear()
    mod._stream_last_published_at.clear()
    mod._projection_fingerprints["desktop"] = "fp-1"
    mod._projection_last_applied_at["desktop"] = 123.0
    stream_key = mod._stream_cache_key("desktop", "infrastate.logs.recent")
    mod._stream_fingerprints[stream_key] = "stream-fp"
    mod._stream_last_published_at[stream_key] = 456.0

    monkeypatch.setattr(mod, "_schedule_snapshot_refresh", lambda **kwargs: scheduled.append(dict(kwargs)))

    evt = SimpleNamespace(type="desktop.webspace.reloaded", payload={"webspace_id": "desktop"})
    mod.on_webspace_reload(evt)

    assert "desktop" not in mod._projection_fingerprints
    assert "desktop" not in mod._projection_last_applied_at
    assert stream_key not in mod._stream_fingerprints
    assert stream_key not in mod._stream_last_published_at
    assert scheduled == [{"webspace_id": "desktop", "reason": "desktop.webspace.reloaded"}]


def test_infrastate_runtime_cache_invalidation_does_not_wait_for_busy_snapshot_lock(monkeypatch):
    mod = _load_infrastate_module()
    cache_key = mod._snapshot_cache_key("desktop")
    old_snapshot = {"summary": {"value": "old"}}
    mod._snapshot_cache[cache_key] = (time.monotonic(), old_snapshot)
    mod._snapshot_cache_projection_fingerprints[cache_key] = "fp-old"

    lock = mod._snapshot_cache_lock_for(cache_key)
    assert lock.acquire(blocking=False)
    try:
        mod._invalidate_runtime_caches(webspace_id="desktop")
    finally:
        lock.release()

    assert mod._snapshot_cache[cache_key][1] == old_snapshot
    assert mod._snapshot_cache_invalidated_after(cache_key, 0.0)

    monkeypatch.setattr(mod, "_snapshot_cache_ttl_for_pressure", lambda _webspace_id: 60.0)
    monkeypatch.setattr(mod, "_snapshot_or_fallback", lambda *, webspace_id=None: {"summary": {"value": "new"}})
    monkeypatch.setattr(mod, "_snapshot_projection_fingerprint", lambda _snapshot: "fp-new")

    snapshot = mod._snapshot_or_fallback_cached(webspace_id="desktop", allow_cache=True)

    assert snapshot["summary"]["value"] == "new"
    assert mod._snapshot_cache[cache_key][1]["summary"]["value"] == "new"
    assert mod._snapshot_cache_projection_fingerprints[cache_key] == "fp-new"


def test_infrastate_snapshot_cache_metadata_is_bounded(monkeypatch):
    mod = _load_infrastate_module()
    monkeypatch.setattr(mod, "_SNAPSHOT_CACHE_MAX_ITEMS", 2)
    mod._snapshot_cache.clear()
    mod._snapshot_cache_invalidated_at.clear()
    mod._snapshot_cache_versions.clear()
    mod._snapshot_cache_entry_versions.clear()
    mod._snapshot_cache_projection_fingerprints.clear()

    for index in range(5):
        mod._mark_snapshot_cache_invalidated(f"desktop-{index}")

    retained = set(mod._snapshot_cache_invalidated_at) | set(mod._snapshot_cache_versions)
    assert len(retained) == 2
    assert "desktop-4" in retained


def test_infrastate_generic_cache_pruning_keeps_recent_entries():
    mod = _load_infrastate_module()
    cache = {"old": 1, "middle": 2, "recent": 3}

    mod._prune_oldest_cache_entries(cache, max_items=2)

    assert cache == {"middle": 2, "recent": 3}


def test_infrastate_snapshot_cache_skips_store_when_invalidated_during_build(monkeypatch):
    mod = _load_infrastate_module()
    cache_key = mod._snapshot_cache_key("desktop")

    monkeypatch.setattr(mod, "_snapshot_cache_ttl_for_pressure", lambda _webspace_id: 60.0)
    monkeypatch.setattr(mod, "_snapshot_projection_fingerprint", lambda _snapshot: "fp-new")

    def _snapshot_during_invalidation(*, webspace_id=None):
        mod._invalidate_runtime_caches(webspace_id=webspace_id)
        return {"summary": {"value": "during-invalidation"}}

    monkeypatch.setattr(mod, "_snapshot_or_fallback", _snapshot_during_invalidation)

    snapshot = mod._snapshot_or_fallback_cached(webspace_id="desktop", allow_cache=True)

    assert snapshot["summary"]["value"] == "during-invalidation"
    assert cache_key not in mod._snapshot_cache
    assert cache_key not in mod._snapshot_cache_projection_fingerprints


def test_infrastate_get_snapshot_force_refresh_does_not_use_snapshot_cache(monkeypatch):
    mod = _load_infrastate_module()
    calls: list[dict[str, object]] = []

    def _cached_snapshot(**kwargs):
        calls.append(dict(kwargs))
        return {"summary": {"label": "Infra State", "value": "fresh"}}

    monkeypatch.setattr(mod, "_snapshot_or_fallback_cached", _cached_snapshot)
    monkeypatch.setattr(
        mod,
        "_lightweight_projection_sections",
        lambda **_kwargs: {"infrastate.summary": {"label": "Infra State", "value": "fresh"}},
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="local"))

    snapshot = mod.get_snapshot(webspace_id="desktop", force_refresh=True)

    assert snapshot["summary"]["value"] == "fresh"
    assert calls == []


def test_infrastate_inventory_action_invalidates_cache_without_full_stream_refresh(monkeypatch):
    mod = _load_infrastate_module()
    cache_key = mod._snapshot_cache_key("desktop")
    mod._snapshot_cache[cache_key] = (time.monotonic(), {"summary": {"value": "old"}})
    mod._snapshot_cache_entry_versions[cache_key] = 0
    mod._snapshot_cache_projection_fingerprints[cache_key] = "fp-old"
    mod._marketplace_catalog_cache[("desktop", "skills")] = (time.monotonic(), [])
    mod._registry_catalog_cache[("desktop", "skills")] = (time.monotonic(), [])
    mod._registry_catalog_meta_cache[("desktop", "skills")] = (time.monotonic(), {"catalog_state": "available"})

    stream_refreshes: list[tuple[str, str | None, str]] = []
    snapshot_refreshes: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="local", role="hub"))
    monkeypatch.setattr(mod, "_perform_action", lambda action_id, conf, payload: {"ok": True})
    monkeypatch.setattr(
        mod,
        "_schedule_stream_receiver_snapshot",
        lambda receiver, webspace_id, *, reason: stream_refreshes.append((receiver, webspace_id, reason)),
    )
    monkeypatch.setattr(mod, "_schedule_snapshot_refresh", lambda **kwargs: snapshot_refreshes.append(dict(kwargs)))

    mod._remember_stream_receiver("desktop", mod._skills_receiver())
    mod._remember_stream_receiver("desktop", mod._marketplace_skills_receiver())

    mod.on_action(SimpleNamespace(payload={"id": "skill_activate", "webspace_id": "desktop", "name": "browsers_skill"}))

    assert cache_key not in mod._snapshot_cache
    assert cache_key not in mod._snapshot_cache_entry_versions
    assert cache_key not in mod._snapshot_cache_projection_fingerprints
    assert mod._marketplace_catalog_cache == {}
    assert mod._registry_catalog_cache == {}
    assert mod._registry_catalog_meta_cache == {}
    assert stream_refreshes == []
    assert snapshot_refreshes == [{"webspace_id": "desktop", "reason": "infrastate.action:skill_activate"}]


def test_infrastate_broad_action_still_refreshes_inventory_streams(monkeypatch):
    mod = _load_infrastate_module()
    stream_refreshes: list[tuple[str, str | None, str]] = []

    monkeypatch.setattr(
        mod,
        "_schedule_stream_receiver_snapshot",
        lambda receiver, webspace_id, *, reason: stream_refreshes.append((receiver, webspace_id, reason)),
    )

    mod._remember_stream_receiver("desktop", mod._skills_receiver())
    mod._remember_stream_receiver("desktop", mod._scenarios_receiver())
    mod._remember_stream_receiver("desktop", mod._marketplace_skills_receiver())
    mod._remember_stream_receiver("desktop", mod._marketplace_scenarios_receiver())

    mod._schedule_action_inventory_streams("marketplace_install", webspace_id="desktop")

    assert stream_refreshes == [
        (mod._skills_receiver(), "desktop", "infrastate.action:marketplace_install"),
        (mod._scenarios_receiver(), "desktop", "infrastate.action:marketplace_install"),
        (mod._marketplace_skills_receiver(), "desktop", "infrastate.action:marketplace_install"),
        (mod._marketplace_scenarios_receiver(), "desktop", "infrastate.action:marketplace_install"),
    ]


def test_infrastate_inventory_action_publishes_row_patches(monkeypatch):
    mod = _load_infrastate_module()
    published: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="local", role="hub"))
    monkeypatch.setattr(mod, "_invalidate_after_action", lambda action_id, *, webspace_id: None)
    monkeypatch.setattr(mod, "_schedule_action_inventory_streams", lambda action_id, *, webspace_id: None)
    monkeypatch.setattr(mod, "_schedule_snapshot_refresh", lambda **kwargs: None)
    monkeypatch.setattr(mod, "_stream_receiver_is_active", lambda webspace_id, receiver: receiver == mod._skills_receiver())
    monkeypatch.setattr(
        mod,
        "_inventory_item_for_kind",
        lambda kind, name: {
            "name": name,
            "workspace_display": "0.1.0",
            "runtime_display": "0.1.0 A",
        },
    )
    monkeypatch.setattr(mod, "_publish_stream_payload", lambda **kwargs: published.append(dict(kwargs)))
    monkeypatch.setattr(
        mod,
        "_perform_action",
        lambda action_id, conf, payload: {
            "ok": True,
            "action": action_id,
            "name": payload["name"],
        },
    )

    mod.on_action(
        SimpleNamespace(
            payload={
                "id": "skill_hard_pull",
                "name": "demo_skill",
                "request_id": "req-1",
                "webspace_id": "desktop",
            }
        )
    )

    assert len(published) == 2
    pending_patch = published[0]["data"]
    final_patch = published[1]["data"]
    assert published[0]["receiver"] == mod._skills_receiver()
    assert pending_patch["schema"] == "adaos.stream.patch.v1"
    assert pending_patch["path"] == "/items"
    assert pending_patch["key"] == "demo_skill"
    assert pending_patch["item"]["pending"] is True
    assert pending_patch["item"]["last_request_id"] == "req-1"
    assert final_patch["item"]["pending"] is False
    assert final_patch["item"]["last_request_id"] == "req-1"


def test_infrastate_scenario_hard_pull_submits_update_operation(monkeypatch):
    mod = _load_infrastate_module()
    submitted: list[dict[str, object]] = []
    ui_writes: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "read_core_update_status", lambda: {})
    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_writes.append(dict(kwargs)))
    monkeypatch.setattr(
        mod,
        "submit_update_operation",
        lambda **kwargs: submitted.append(dict(kwargs))
        or {"operation_id": "op-1", "target_kind": "scenario", "target_id": "new_face_vision", "action": "update"},
    )

    result = mod._perform_action(
        "scenario_hard_pull",
        SimpleNamespace(node_id="local", role="hub"),
        {"name": "new_face_vision", "webspace_id": "desktop"},
    )

    assert submitted == [
        {
            "target_kind": "scenario",
            "target_id": "new_face_vision",
            "webspace_id": "desktop",
            "initiator": {
                "kind": "ui",
                "id": "infrastate",
                "node_id": "local",
                "target_node_id": "local",
            },
        }
    ]
    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["operation_id"] == "op-1"
    assert ui_writes[-1]["last_action"] == "scenario_hard_pull"


def test_infrastate_stream_snapshot_request_publishes_requested_receiver(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []
    _run_stream_snapshots_inline(mod, monkeypatch)

    monkeypatch.setattr(mod, "_lightweight_control_context", lambda **_kwargs: {"display_last_result": {}})
    monkeypatch.setattr(mod, "_status_log_items", lambda _report: [{"id": "log-1"}])
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.logs.recent",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        ("infrastate.logs.recent", [{"id": "log-1"}], "default"),
    ]


def test_infrastate_stream_snapshot_request_schedules_registered_receiver(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[tuple[str, str | None, str]] = []

    monkeypatch.setattr(
        mod,
        "_schedule_stream_receiver_snapshot",
        lambda receiver, webspace_id, *, reason, node_id=None: scheduled.append((receiver, webspace_id, reason)),
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": mod._scenarios_receiver(),
                "webspace_id": "desktop",
            }
        )
    )

    assert scheduled == [(mod._scenarios_receiver(), "desktop", "snapshot_requested")]


def test_infrastate_stream_subscription_changed_schedules_initial_snapshot(monkeypatch):
    mod = _load_infrastate_module()
    remembered: list[tuple[str | None, str]] = []
    scheduled: list[tuple[str, str | None, str]] = []

    monkeypatch.setattr(
        mod,
        "_remember_stream_receiver",
        lambda webspace_id, receiver, node_id=None: remembered.append((webspace_id, receiver)),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_stream_receiver_snapshot",
        lambda receiver, webspace_id, *, reason, node_id=None: scheduled.append((receiver, webspace_id, reason)),
    )

    mod.on_webio_stream_subscription_changed(
        SimpleNamespace(
            payload={
                "receiver": mod._scenarios_receiver(),
                "webspace_id": "desktop",
                "action": "subscribed",
            }
        )
    )

    assert remembered == [("desktop", mod._scenarios_receiver())]
    assert scheduled == [(mod._scenarios_receiver(), "desktop", "subscription_changed")]


def test_infrastate_stream_subscription_changed_passes_target_node(monkeypatch):
    mod = _load_infrastate_module()
    remembered: list[tuple[str | None, str, str | None]] = []
    scheduled: list[tuple[str, str | None, str, str | None]] = []

    monkeypatch.setattr(
        mod,
        "_remember_stream_receiver",
        lambda webspace_id, receiver, node_id=None: remembered.append((webspace_id, receiver, node_id)),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_stream_receiver_snapshot",
        lambda receiver, webspace_id, *, reason, node_id=None: scheduled.append((receiver, webspace_id, reason, node_id)),
    )

    mod.on_webio_stream_subscription_changed(
        SimpleNamespace(
            payload={
                "receiver": mod._skills_receiver(),
                "webspace_id": "desktop",
                "target_node_id": "member-1",
                "action": "subscribed",
            }
        )
    )

    assert remembered == [("desktop", mod._skills_receiver(), "member-1")]
    assert scheduled == [(mod._skills_receiver(), "desktop", "subscription_changed", "member-1")]


def test_infrastate_stream_subscription_changed_forgets_without_snapshot(monkeypatch):
    mod = _load_infrastate_module()
    forgotten: list[tuple[str | None, str]] = []
    scheduled: list[tuple[str, str | None, str]] = []

    monkeypatch.setattr(
        mod,
        "_forget_stream_receiver",
        lambda webspace_id, receiver, node_id=None: forgotten.append((webspace_id, receiver)),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_stream_receiver_snapshot",
        lambda receiver, webspace_id, *, reason, node_id=None: scheduled.append((receiver, webspace_id, reason)),
    )

    mod.on_webio_stream_subscription_changed(
        SimpleNamespace(
            payload={
                "receiver": mod._skills_receiver(),
                "webspace_id": "desktop",
                "action": "unsubscribed",
            }
        )
    )
    mod.on_webio_stream_subscription_changed(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.details.skills.skill-a",
                "webspace_id": "desktop",
                "action": "subscribed",
            }
        )
    )

    assert forgotten == [
        ("desktop", mod._skills_receiver()),
        ("desktop", "infrastate.details.skills.skill-a"),
    ]
    assert scheduled == []


def test_infrastate_yjs_snapshot_request_schedules_projection_refresh(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda **kwargs: scheduled.append(dict(kwargs)),
    )

    mod.on_webio_yjs_snapshot_requested(
        SimpleNamespace(
            payload={
                "slot": "infrastate.summary",
                "webspace_id": "desktop",
            }
        )
    )

    assert scheduled == [
        {"webspace_id": "desktop", "reason": "webio.yjs.snapshot_requested"},
    ]


def test_infrastate_yjs_subscription_changed_schedules_projection_refresh(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda **kwargs: scheduled.append(dict(kwargs)),
    )

    mod.on_webio_yjs_subscription_changed(
        SimpleNamespace(
            payload={
                "topic": "webio.yjs.desktop.infrastate.projection_diag",
                "webspace_id": "desktop",
                "action": "subscribed",
            }
        )
    )
    mod.on_webio_yjs_subscription_changed(
        SimpleNamespace(
            payload={
                "slot": "infrastate.summary",
                "webspace_id": "desktop",
                "action": "unsubscribed",
            }
        )
    )
    mod.on_webio_yjs_snapshot_requested(
        SimpleNamespace(
            payload={
                "slot": "infrascope.summary",
                "webspace_id": "desktop",
            }
        )
    )

    assert scheduled == [
        {"webspace_id": "desktop", "reason": "webio.yjs.subscription_changed"},
    ]


def test_infrastate_operations_stream_request_uses_direct_sdk_builder(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []
    _run_stream_snapshots_inline(mod, monkeypatch)

    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("operations stream should not build full snapshot")),
    )
    monkeypatch.setattr(
        mod,
        "get_operation_manager",
        lambda: SimpleNamespace(
            snapshot=lambda webspace_id=None: {
                "active_items": [{"id": "op-1", "webspace_id": webspace_id}],
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.operations.active",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        ("infrastate.operations.active", [{"id": "op-1", "webspace_id": "default"}], "default"),
    ]


def test_infrastate_operation_details_include_dependency_bootstrap():
    mod = _load_infrastate_module()
    snapshot = {
        "operations": {
            "by_id": {
                "op-1": {
                    "operation_id": "op-1",
                    "kind": "scenario.update",
                    "target_kind": "scenario",
                    "target_id": "alpha",
                    "status": "failed",
                    "message": "required scenario dependencies failed: bad_skill",
                    "error": {
                        "type": "ScenarioDependencyLifecycleError",
                        "dependency_bootstrap": {
                            "ok": False,
                            "failed": ["bad_skill"],
                            "items": [{"name": "bad_skill", "ok": False, "error": "prepare failed"}],
                        },
                    },
                }
            }
        }
    }

    detail = mod._detail_payload_for_receiver(snapshot, "infrastate.details.operations.op-1")

    assert detail["id"] == "op-1"
    assert detail["status"] == "failed"
    assert "dependency_bootstrap" in detail["content"]
    assert "bad_skill" in detail["content"]


def test_infrastate_stream_snapshot_request_bypasses_noncritical_guardrail(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []
    suppressions: list[dict[str, object]] = []
    _run_stream_snapshots_inline(mod, monkeypatch)

    monkeypatch.setattr(mod, "_lightweight_control_context", lambda **_kwargs: {"display_last_result": {}})
    monkeypatch.setattr(mod, "_status_log_items", lambda _report: [{"id": "log-1"}])
    monkeypatch.setattr(
        mod,
        "_active_noncritical_stream_guardrail",
        lambda webspace_id, receiver: {"active": True, "reason": "yjs_pressure"},
    )
    monkeypatch.setattr(
        mod,
        "_record_noncritical_stream_guardrail_suppression",
        lambda **kwargs: suppressions.append(kwargs),
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None: published.append((receiver, data, (_meta or {}).get("webspace_id"))),
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.logs.recent",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        ("infrastate.logs.recent", [{"id": "log-1"}], "default"),
    ]
    assert suppressions == []


def test_infrastate_cached_snapshot_coalesces_concurrent_stream_requests(monkeypatch):
    mod = _load_infrastate_module()
    calls: list[str | None] = []
    start = threading.Event()

    monkeypatch.setattr(mod, "_SNAPSHOT_CACHE_TTL_S", 30.0)
    mod._snapshot_cache.clear()

    def _slow_snapshot(*, webspace_id=None):
        calls.append(webspace_id)
        time.sleep(0.05)
        return {"summary": {"value": "ready"}, "webspace_id": webspace_id}

    monkeypatch.setattr(mod, "_snapshot_or_fallback", _slow_snapshot)

    results: list[dict[str, object]] = []

    def _worker() -> None:
        start.wait(timeout=5)
        results.append(mod._snapshot_or_fallback_cached(webspace_id="default", allow_cache=True))

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 5
    assert calls == ["default"]


def test_infrastate_stream_snapshot_request_supports_yjs_load_mark(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []
    _run_stream_snapshots_inline(mod, monkeypatch)

    monkeypatch.setattr(
        mod,
        "_direct_yjs_load_mark_rows",
        lambda webspace_id=None: [{"root": "ui", "peak_bps": 12.0, "kind": "root", "id": "ui", "display": "ui"}],
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.yjs.load_mark",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        (
            "infrastate.yjs.load_mark",
            [{"root": "ui", "peak_bps": 12.0, "kind": "root", "id": "ui", "display": "ui"}],
            "default",
        ),
    ]


def test_infrastate_stream_snapshot_request_supports_direct_yjs_load_mark_rows(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []
    _run_stream_snapshots_inline(mod, monkeypatch)

    monkeypatch.setattr(
        mod,
        "_direct_yjs_load_mark_rows",
        lambda webspace_id=None: [{"root": "registry", "avg_bps": 7.0, "kind": "root", "id": "registry", "display": "registry"}],
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.yjs.load_mark",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        (
            "infrastate.yjs.load_mark",
            [{"root": "registry", "avg_bps": 7.0, "kind": "root", "id": "registry", "display": "registry"}],
            "default",
        ),
    ]


def test_infrastate_direct_yjs_load_mark_stream_uses_history_when_memory_is_empty(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "yjs_load_mark_snapshot",
        lambda webspace_id=None: {"selected_webspace": {"items": [], "owner_items": []}},
    )
    monkeypatch.setattr(
        mod,
        "list_yjs_load_mark_history_rows",
        lambda **kwargs: {
            "items": [
                {
                    "kind": "owner",
                    "bucket_id": "_by_owner/skill_infrastate_skill",
                    "display": "_by_owner/skill_infrastate_skill",
                    "peak_bps": 12.0,
                }
            ]
        },
    )
    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("history fallback should avoid full snapshot")),
    )

    rows = mod._build_stream_payload_for_receiver(mod._yjs_load_receiver(), "desktop")

    assert rows == [
        {
            "kind": "owner",
            "bucket_id": "_by_owner/skill_infrastate_skill",
            "display": "_by_owner/skill_infrastate_skill",
            "peak_bps": 12.0,
            "id": "_by_owner/skill_infrastate_skill",
            "owner": "_by_owner/skill_infrastate_skill",
        }
    ]


def test_infrastate_manifest_wakes_on_webio_stream_controls():
    manifest = (Path(__file__).resolve().parents[1] / "skill.yaml").read_text(encoding="utf-8")

    assert "\n  - webio.stream.snapshot.requested\n" in manifest
    assert "\n  - webio.stream.subscription.changed\n" in manifest


def test_infrastate_manifest_admits_boot_and_background_status_events():
    mod = _load_infrastate_module()
    skill_dir = Path(__file__).resolve().parents[1]
    manifest = mod.yaml.safe_load((skill_dir / "skill.yaml").read_text(encoding="utf-8"))
    activation = ((manifest.get("runtime") or {}).get("activation") or {})

    assert activation.get("mode") == "lazy"
    assert activation.get("startup_allowed") is True
    assert activation.get("background_refresh") is True
    assert not activation.get("when")

    from adaos.services.skill.activation import load_skill_activation_policy, subscription_event_admission

    policy = load_skill_activation_policy(skill_dir.parent, "infrastate_skill")
    admitted = subscription_event_admission(
        policy,
        SimpleNamespace(type="sys.ready", payload={"ts": 1.0}),
        "sys.ready",
    )
    assert admitted["allowed"] is True


def test_infrastate_marketplace_action_navigates_to_declared_modal_without_host_roundtrip():
    webui = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    widgets = webui["registry"]["modals"]["infrastate_modal"]["schema"]["widgets"]
    update_actions = next(widget for widget in widgets if widget.get("id") == "infrastate-update-actions")
    actions = update_actions["actions"]

    marketplace = next(action for action in actions if action.get("on") == "click:marketplace")
    assert marketplace.get("type") == "navigate"
    assert marketplace.get("params") == {
        "to": "infrastate_skill.marketplace_modal",
        "surface": "modal",
        "modalId": "marketplace_modal",
    }
    assert not any(action.get("on") == "click" and action.get("target") == "infrastate.action" for action in actions)


def test_infrastate_inventory_toolbar_wires_bulk_action_buttons():
    webui = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    widgets = webui["registry"]["modals"]["infrastate_modal"]["schema"]["widgets"]
    update_actions = next(widget for widget in widgets if widget.get("id") == "infrastate-update-actions")
    action_by_on = {action.get("on"): action for action in update_actions["actions"]}
    defaults = webui["ydoc_defaults"]["data/infrastate"]["update_actions"]

    assert [item["id"] for item in defaults] == [
        "adaos_update",
        "inventory_activate",
        "inventory_validate",
        "inventory_test",
        "marketplace",
    ]
    for action_id in ["adaos_update", "inventory_activate", "inventory_validate", "inventory_test"]:
        action = action_by_on[f"click:{action_id}"]
        assert action.get("type") == "callHost"
        assert action.get("target") == "infrastate.action"
        assert (action.get("params") or {}).get("id") == "$event.id"
    assert action_by_on["click:marketplace"].get("type") == "navigate"
    assert (action_by_on["click:marketplace"].get("params") or {}).get("to") == "infrastate_skill.marketplace_modal"


def test_infrastate_yjs_balancer_tab_uses_y_projection():
    webui = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    widgets = webui["registry"]["modals"]["infrastate_modal"]["schema"]["widgets"]
    by_id = {widget.get("id"): widget for widget in widgets}
    tabs = by_id["infrastate-tabs"]["inputs"]["buttons"]
    tab_ids = [tab.get("id") for tab in tabs]

    assert tab_ids.index("yjs_balancer") > tab_ids.index("diagnostics")
    assert by_id["infrastate-yjs-balancer"]["type"] == "ui.jsonViewer"
    assert by_id["infrastate-yjs-balancer"]["visibleIf"] == "$state.infrastateTab === 'yjs_balancer'"
    assert by_id["infrastate-yjs-balancer"]["dataSource"] == {
        "kind": "y",
        "path": "data/infrastate/yjs_balancer",
    }


def test_infrastate_manifest_includes_handler_projection_slots():
    mod = _load_infrastate_module()
    manifest = mod.yaml.safe_load((Path(__file__).resolve().parents[1] / "skill.yaml").read_text(encoding="utf-8"))
    entries = manifest.get("data_projections") if isinstance(manifest, dict) else []
    manifest_paths = {
        str(entry.get("slot") or ""): str(((entry.get("targets") or [{}])[0] or {}).get("path") or "")
        for entry in list(entries or [])
        if isinstance(entry, dict)
    }
    manifest_scopes = {
        str(entry.get("slot") or ""): str(entry.get("scope") or "")
        for entry in list(entries or [])
        if isinstance(entry, dict)
    }

    for slot, path in mod._PROJECTION_SLOT_PATHS.items():
        assert manifest_paths.get(slot) == path
        assert manifest_scopes.get(slot) == "webspace"


def test_infrastate_inventory_and_marketplace_use_stream_data_sources():
    webui = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    infrastate_widgets = webui["registry"]["modals"]["infrastate_modal"]["schema"]["widgets"]
    marketplace_widgets = webui["registry"]["modals"]["marketplace_modal"]["schema"]["widgets"]
    by_id = {widget.get("id"): widget for widget in [*infrastate_widgets, *marketplace_widgets]}

    assert by_id["infrastate-skills"]["dataSource"] == {
        "kind": "stream",
        "scope": "node",
        "receiver": "infrastate.skills",
    }
    assert webui["webio"]["receivers"]["infrastate.skills"]["scope"] == "node"
    assert by_id["infrastate-scenarios"]["dataSource"] == {
        "kind": "stream",
        "scope": "node",
        "receiver": "infrastate.scenarios",
    }
    assert webui["webio"]["receivers"]["infrastate.scenarios"]["scope"] == "node"
    assert webui["webio"]["receivers"]["infrastate.scenarios"]["route"]["surface"] == "modal:scenario_registry"
    assert by_id["infrastate-scenarios"]["title"] == "Scenarios"
    scenario_columns = by_id["infrastate-scenarios"]["inputs"]["columns"]
    assert any(column.get("key") == "catalog_display" and column.get("label") == "Catalog" for column in scenario_columns)
    assert any(column.get("key") == "workspace_display" and column.get("label") == "Workspace" for column in scenario_columns)
    assert any(column.get("key") == "runtime_display" and column.get("label") == "Runtime" for column in scenario_columns)
    assert not any(
        column.get("label") in {"Local source", "Registry", "Active registry", "Installed"}
        for column in scenario_columns
    )
    assert not any(column.get("key") in {"active_registry_display", "_workspace_actions"} for column in scenario_columns)
    scenario_action_column = next(column for column in scenario_columns if column.get("key") == "_runtime_actions")
    scenario_buttons = scenario_action_column.get("buttons") or []
    assert any(
        button.get("id") == "hard_pull"
        and button.get("icon") == "cloud-download-outline"
        and button.get("whenKey") == "can_hard_pull"
        and "Workspace source" in str(button.get("tooltip") or "")
        for button in scenario_buttons
    )
    scenario_actions = by_id["infrastate-scenarios"].get("actions") or []
    assert any(
        action.get("on") == "click:hard_pull"
        and ((action.get("params") or {}).get("id") == "scenario_hard_pull")
        for action in scenario_actions
    )
    assert by_id["marketplace-skills"]["dataSource"] == {
        "kind": "stream",
        "scope": "node",
        "receiver": "infrastate.marketplace.skills",
    }
    assert webui["webio"]["receivers"]["infrastate.marketplace.skills"]["scope"] == "node"
    assert by_id["marketplace-scenarios"]["dataSource"] == {
        "kind": "stream",
        "scope": "node",
        "receiver": "infrastate.marketplace.scenarios",
    }
    assert webui["webio"]["receivers"]["infrastate.marketplace.scenarios"]["scope"] == "node"


def test_infrastate_skill_inventory_stream_payload_is_compact(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(node_id="local-node"))
    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "local-node"})
    monkeypatch.setattr(mod, "_inventory_drift_only_enabled", lambda: False)
    monkeypatch.setattr(
        mod,
        "_skills_items",
        lambda include_all=True: [
            {
                "name": "infrastate_skill",
                "display_name": "infrastate_skill",
                "status_icon": "checkmark-circle-outline",
                "status_tooltip": "Versions are aligned.",
                "catalog_display": "0.1.0",
                "workspace_display": "0.1.0",
                "runtime_display": "0.1.0 A",
                "can_activate": False,
                "can_hard_pull": False,
                "can_push": True,
                "can_validate": True,
                "can_test": True,
                "can_logs": True,
                "uninstall_disabled": False,
                "catalog_version": "0.1.0",
                "catalog_commit": "abcdef",
                "catalog_source": "workspace",
                "active_version": "0.1.0",
                "workspace_source_version": "0.1.0",
                "runtime_bucket": "v0.1",
                "used_by_scenarios": ["web_desktop"],
                "rollout_quarantine_reason": "large diagnostic reason",
            }
        ],
    )

    rows = mod._build_stream_payload_for_receiver(mod._skills_receiver(), "desktop")

    assert rows == [
        {
            "name": "infrastate_skill",
            "display_name": "infrastate_skill",
            "status_icon": "checkmark-circle-outline",
            "status_tooltip": "Versions are aligned.",
            "catalog_display": "0.1.0",
            "workspace_display": "0.1.0",
            "runtime_display": "0.1.0 A",
            "can_activate": False,
            "can_hard_pull": False,
            "can_push": True,
            "can_validate": True,
            "can_test": True,
            "can_logs": True,
            "uninstall_disabled": False,
        }
    ]


def test_infrastate_inventory_row_patch_is_compact(monkeypatch):
    mod = _load_infrastate_module()
    published: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_stream_receiver_is_active", lambda webspace_id, receiver: True)
    monkeypatch.setattr(
        mod,
        "_inventory_item_for_kind",
        lambda kind, name: {
            "name": name,
            "display_name": name,
            "status_icon": "warning-outline",
            "status_tooltip": "Workspace differs.",
            "catalog_display": "0.1.0",
            "workspace_display": "0.2.0",
            "runtime_display": "0.1.0 A",
            "can_activate": True,
            "can_hard_pull": False,
            "can_push": True,
            "can_validate": True,
            "can_test": True,
            "can_logs": True,
            "uninstall_disabled": False,
            "catalog_commit": "abcdef",
            "used_by_scenarios": ["web_desktop"],
            "rollout_quarantine_reason": "large diagnostic reason",
        },
    )
    monkeypatch.setattr(
        mod,
        "_publish_stream_payload",
        lambda **kwargs: published.append(dict(kwargs)),
    )

    mod._publish_inventory_row_patch(
        kind="skill",
        name="infrastate_skill",
        webspace_id="desktop",
        request_id="req-1",
        pending=True,
        operation_status="accepted",
    )

    item = published[0]["data"]["item"]
    assert item["name"] == "infrastate_skill"
    assert item["last_request_id"] == "req-1"
    assert item["pending"] is True
    assert item["operation_status"] == "accepted"
    assert "catalog_commit" not in item
    assert "used_by_scenarios" not in item
    assert "rollout_quarantine_reason" not in item


def test_infrastate_operation_patch_never_rebuilds_inventory(monkeypatch):
    mod = _load_infrastate_module()
    published: list[dict[str, object]] = []
    mod._inventory_row_cache.clear()

    monkeypatch.setattr(mod, "_stream_receiver_is_active", lambda webspace_id, receiver: True)
    monkeypatch.setattr(
        mod,
        "_inventory_items_from",
        lambda _builder: (_ for _ in ()).throw(AssertionError("operation patch must not rebuild inventory")),
    )
    monkeypatch.setattr(mod, "_publish_stream_payload", lambda **kwargs: published.append(dict(kwargs)))

    mod._publish_inventory_row_patch_for_operation(
        {
            "operation_id": "op-1",
            "target_kind": "scenario",
            "target_id": "web_desktop",
            "status": "running",
            "webspace_id": "desktop",
        }
    )

    item = published[0]["data"]["item"]
    assert item == {
        "name": "web_desktop",
        "pending": True,
        "operation_id": "op-1",
        "operation_status": "running",
        "updated_at": item["updated_at"],
        "last_action_at": item["last_action_at"],
    }


def test_infrastate_inventory_snapshot_populates_row_patch_cache():
    mod = _load_infrastate_module()
    mod._inventory_row_cache.clear()

    rows = mod._compact_inventory_stream_items(
        "skill",
        [{"name": "demo_skill", "workspace_display": "1.2.3", "catalog_commit": "ignored"}],
    )

    assert rows == [{"name": "demo_skill", "workspace_display": "1.2.3"}]
    assert mod._inventory_item_for_kind("skill", "demo_skill") == rows[0]

    mod._compact_inventory_stream_items("skill", [{"name": "replacement_skill"}])
    assert mod._inventory_item_for_kind("skill", "demo_skill") is None
    assert mod._inventory_item_for_kind("skill", "replacement_skill") == {"name": "replacement_skill"}


def test_infrastate_operation_request_cache_is_bounded(monkeypatch):
    mod = _load_infrastate_module()
    mod._inventory_request_by_operation.clear()
    monkeypatch.setattr(mod, "_INVENTORY_OPERATION_REQUEST_MAX_ITEMS", 2)

    for index in range(3):
        mod._remember_inventory_operation_request(
            f"op-{index}",
            kind="skill",
            name=f"skill-{index}",
            request_id=f"request-{index}",
        )

    assert mod._remembered_inventory_operation_request("op-0") == {}
    assert set(mod._inventory_request_by_operation) == {"op-1", "op-2"}


def test_infrastate_runtime_event_invalidates_snapshot_cache(monkeypatch):
    mod = _load_infrastate_module()
    invalidated: list[str | None] = []
    scheduled: list[dict[str, object]] = []
    appended: list[str] = []

    monkeypatch.setattr(
        mod,
        "_invalidate_runtime_caches",
        lambda *, webspace_id=None, marketplace=False: invalidated.append(webspace_id),
    )
    monkeypatch.setattr(mod, "_append_event", lambda event_type, payload: appended.append(event_type))
    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda **kwargs: scheduled.append(dict(kwargs)),
    )

    asyncio.run(
        mod.on_runtime_event(
            SimpleNamespace(
                type="core.update.status",
                payload={
                    "state": "succeeded",
                    "phase": "validate",
                    "webspace_id": "default",
                },
            )
        )
    )

    assert invalidated == ["default"]
    assert appended == ["core.update.status"]
    assert scheduled == [{"webspace_id": "default", "reason": "core.update.status.terminal"}]


def test_infrastate_sys_ready_defers_first_paint_refresh(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_invalidate_runtime_caches", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda **kwargs: scheduled.append(dict(kwargs)),
    )

    asyncio.run(mod.on_runtime_event(SimpleNamespace(type="sys.ready", payload={"ts": 1.0})))

    assert scheduled == [{"webspace_id": None, "reason": "sys.ready"}]


def test_infrastate_sys_ready_uses_sdk_envelope_metadata(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_invalidate_runtime_caches", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda **kwargs: scheduled.append(dict(kwargs)),
    )

    asyncio.run(
        mod.on_runtime_event(
            {
                "ts": 1.0,
                "_meta": {
                    "event_type": "sys.ready",
                    "event_source": "lifecycle",
                },
            }
        )
    )

    assert scheduled == [{"webspace_id": None, "reason": "sys.ready"}]


def test_infrastate_sys_ready_materializes_when_event_history_is_unavailable(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_invalidate_runtime_caches", lambda **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("event history unavailable")),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda **kwargs: scheduled.append(dict(kwargs)),
    )

    asyncio.run(mod.on_runtime_event(SimpleNamespace(type="sys.ready", payload={"ts": 1.0})))

    assert scheduled == [{"webspace_id": None, "reason": "sys.ready"}]


def test_infrastate_marketplace_hides_skills_installed_via_scenario_dependencies(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (
            [{"kind": "skill", "id": "prompt_engineer_skill", "name": "prompt_engineer_skill", "version": "0.5.0"}]
            if kind == "skills"
            else [{"kind": "scenario", "id": "prompt_engineer_scenario", "name": "prompt_engineer_scenario", "version": "0.2.0"}]
        ),
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {"depends": ["prompt_engineer_skill"]} if name == "prompt_engineer_scenario" else {})

    items = mod._marketplace_items(webspace_id="default")

    assert items["skills"] == []
    assert [item["id"] for item in items["scenarios"]] == ["prompt_engineer_scenario"]


def test_infrastate_effective_runtime_projection_prefers_validated_target_slot():
    mod = _load_infrastate_module()

    slots_payload = {
        "active_slot": "A",
        "previous_slot": "B",
        "slots": {
            "A": {"manifest": {"slot": "A", "git_short_commit": "ddeb33f", "git_commit": "ddeb33f-old"}},
            "B": {"manifest": {"slot": "B", "git_short_commit": "stale-b"}},
        },
    }
    build = {
        "runtime_mode": "slot",
        "runtime_version": "old",
        "runtime_git_commit": "ddeb33f-old",
        "runtime_git_short_commit": "ddeb33f",
        "runtime_git_branch": "HEAD",
        "runtime_git_subject": "old subject",
    }
    status = {
        "state": "succeeded",
        "phase": "validate",
        "target_slot": "B",
        "manifest": {
            "slot": "B",
            "target_version": "8dd3543c72f912ef0d7932f4c5754ce4c6700849",
            "git_commit": "8dd3543c72f912ef0d7932f4c5754ce4c6700849",
            "git_short_commit": "8dd3543",
            "git_branch": "HEAD",
            "git_subject": "feat: add skill-aware infra_access publication and Root MCP token lifecycle management",
        },
    }

    effective_slots, effective_build = mod._effective_runtime_projection(status, {}, slots_payload, build)

    assert effective_slots["active_slot"] == "B"
    assert effective_slots["previous_slot"] == "A"
    assert effective_slots["slots"]["B"]["manifest"]["git_short_commit"] == "8dd3543"
    assert effective_build["runtime_git_short_commit"] == "8dd3543"
    assert effective_build["runtime_git_commit"] == "8dd3543c72f912ef0d7932f4c5754ce4c6700849"


def test_infrastate_effective_runtime_projection_ignores_stale_slots_for_dev_runtime():
    mod = _load_infrastate_module()

    slots_payload = {
        "active_slot": "A",
        "previous_slot": "B",
        "slots": {
            "A": {"manifest": {"slot": "A", "build_version": "0.1.0+1.8c698078", "git_short_commit": "8c698078"}},
            "B": {"manifest": {"slot": "B", "build_version": "0.1.217+1.b10da50", "git_short_commit": "b10da50"}},
        },
    }
    build = {
        "runtime_mode": "dev",
        "runtime_build_version": "0.1.0+22.8c698078",
        "runtime_base_version": "0.1.218",
        "runtime_git_short_commit": "8c698078",
        "runtime_git_branch": "rev2026",
    }
    status = {
        "state": "succeeded",
        "phase": "validate",
        "target_slot": "B",
        "manifest": {"slot": "B", "build_version": "0.1.217+1.b10da50"},
    }

    effective_slots, effective_build = mod._effective_runtime_projection(status, {}, slots_payload, build)

    assert effective_slots["active_slot"] == "dev"
    assert effective_slots["slots"] == {}
    assert effective_slots["active_manifest"]["slot"] == "dev"
    assert effective_slots["active_manifest"]["base_version"] == "0.1.218"
    assert effective_build["runtime_mode"] == "dev"


def test_infrastate_skill_runtime_migration_helpers_report_failures():
    mod = _load_infrastate_module()

    report = mod._skill_runtime_migration_report(
        {},
        {
            "manifest": {
                "skill_runtime_migration": {
                    "total": 2,
                    "failed_total": 1,
                    "lifecycle_failed_total": 1,
                    "rollback_total": 1,
                    "skills": [
                        {"skill": "weather_skill", "ok": True},
                        {"skill": "voice_skill", "ok": False, "failure_kind": "lifecycle", "failed_stage": "rehydrate"},
                    ],
                }
            }
        },
    )
    note = mod._skill_runtime_migration_note(report)

    assert report["failed_total"] == 1
    assert "skill_migration=1/2" in note
    assert "voice_skill:lifecycle/rehydrate" in note
    assert "lifecycle_failed=1" in note
    assert "rollback=1" in note


def test_infrastate_skill_runtime_rollback_helpers_report_failures():
    mod = _load_infrastate_module()

    report = mod._skill_runtime_rollback_report(
        {
            "skill_runtime_rollback": {
                "total": 3,
                "failed_total": 1,
                "rollback_total": 2,
                "skipped_total": 1,
                "skills": [
                    {"skill": "weather_skill", "ok": True},
                    {"skill": "voice_skill", "ok": False, "error": "broken rollback"},
                    {"skill": "maps_skill", "ok": True, "skipped": True},
                ],
            }
        },
        {},
    )
    note = mod._skill_runtime_rollback_note(report)

    assert report["failed_total"] == 1
    assert "skill_rollback=2/3" in note
    assert "failed=voice_skill" in note
    assert "skipped=1" in note


def test_infrastate_skill_post_commit_helpers_report_deactivations():
    mod = _load_infrastate_module()

    report = mod._skill_post_commit_checks_report(
        {
            "skill_post_commit_checks": {
                "total": 2,
                "failed_total": 1,
                "lifecycle_failed_total": 1,
                "deactivated_total": 1,
                "skills": [
                    {"skill": "weather_skill", "ok": True},
                    {
                        "skill": "voice_skill",
                        "ok": False,
                        "failure_kind": "lifecycle",
                        "failed_stage": "rehydrate",
                        "deactivated": True,
                        "deactivation": {
                            "committed_core_switch": True,
                            "failure_kind": "lifecycle",
                            "failed_stage": "rehydrate",
                        },
                    },
                ],
            }
        },
        {},
    )
    note = mod._skill_post_commit_checks_note(report)

    assert report["deactivated_total"] == 1
    assert "skill_post_commit=1/2" in note
    assert "voice_skill:lifecycle/rehydrate" in note
    assert "lifecycle_failed=1" in note
    assert "deactivated=1" in note
    assert "quarantine=voice_skill:lifecycle/rehydrate" in note


def test_infrastate_skill_post_commit_helpers_report_existing_quarantine():
    mod = _load_infrastate_module()

    note = mod._skill_post_commit_checks_note(
        {
            "total": 1,
            "failed_total": 0,
            "deactivated_total": 1,
            "skills": [
                {
                    "skill": "voice_skill",
                    "ok": True,
                    "skipped": True,
                    "deactivated": True,
                    "deactivation": {
                        "committed_core_switch": True,
                        "failure_kind": "lifecycle",
                        "failed_stage": "rehydrate",
                    },
                }
            ],
        }
    )

    assert "skill_post_commit=1/1" in note
    assert "quarantine=voice_skill:lifecycle/rehydrate" in note


def test_infrastate_core_update_diagnostics_include_required_local_payloads(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()

    base_dir = tmp_path / ".adaos"
    (base_dir / "state" / "supervisor").mkdir(parents=True, exist_ok=True)
    runtime_path = base_dir / "state" / "supervisor" / "runtime.json"
    runtime_path.write_text('{"runtime_state":"spawned","managed_matches_active_slot":false}', encoding="utf-8")
    slot_dir = base_dir / "state" / "core_slots" / "slots" / "B"
    (slot_dir / "repo" / "src").mkdir(parents=True, exist_ok=True)
    (slot_dir / "venv" / "bin").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mod, "_base_dir", lambda: base_dir)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="journal tail line\nsecond line", stderr="", returncode=0),
    )

    last_result = {
        "state": "failed",
        "phase": "apply",
        "target_slot": "B",
        "message": "core update command failed",
    }
    status = {"state": "idle"}
    slots_payload = {"inactive_slot": "B"}

    items = mod._core_update_diagnostic_items(status, last_result, slots_payload, local_node=True)
    actions = mod._core_update_diagnostic_actions(items)

    by_id = {item["id"]: item for item in items}
    assert "core-update-last-result" in by_id
    assert "supervisor-runtime" in by_id
    assert "target-slot-tree" in by_id
    assert "adaos-service-journal" in by_id
    assert "journal tail line" in by_id["adaos-service-journal"]["content"]
    assert "repo/src" in by_id["target-slot-tree"]["content"]
    assert any(item["id"] == "copy_core_update_diag_bundle" for item in actions)
    assert any(item["id"] == "copy_core_update_diag_commands" for item in actions)


def test_infrastate_core_update_diagnostics_skip_local_files_for_remote_member(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "_base_dir", lambda: Path("/base"))
    items = mod._core_update_diagnostic_items(
        {"state": "idle"},
        {"state": "failed", "target_slot": "B"},
        {"inactive_slot": "B"},
        local_node=False,
    )

    ids = [item["id"] for item in items]
    assert "core-update-diagnostic-commands" in ids
    assert "core-update-last-result" in ids
    assert "supervisor-runtime" not in ids
    assert "target-slot-tree" not in ids


def test_infrastate_post_local_admin_prefers_supervisor_for_update_routes(monkeypatch):
    mod = _load_infrastate_module()

    calls: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _Resp({"ok": True, "_served_by": "supervisor"})

    monkeypatch.setattr(mod.requests, "post", _post)
    monkeypatch.setenv("ADAOS_SUPERVISOR_HOST", "127.0.0.1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PORT", "8776")
    monkeypatch.setattr(mod, "_self_base_url", lambda conf: "http://127.0.0.1:8777")

    payload = mod._post_local_admin(SimpleNamespace(token="dev-token"), "/api/admin/update/start", {"reason": "test"})

    assert payload["_served_by"] == "supervisor"
    assert calls == ["http://127.0.0.1:8776/api/supervisor/update/start"]


def test_infrastate_post_local_admin_falls_back_to_runtime_admin_when_supervisor_is_unavailable(monkeypatch):
    mod = _load_infrastate_module()

    calls: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        if "8776" in url:
            raise RuntimeError("supervisor unavailable")
        return _Resp({"ok": True, "_served_by": "runtime"})

    monkeypatch.setattr(mod.requests, "post", _post)
    monkeypatch.setenv("ADAOS_SUPERVISOR_HOST", "127.0.0.1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PORT", "8776")
    monkeypatch.setattr(mod, "_self_base_url", lambda conf: "http://127.0.0.1:8777")

    payload = mod._post_local_admin(SimpleNamespace(token="dev-token"), "/api/admin/update/cancel", {"reason": "test"})

    assert payload["_served_by"] == "runtime"
    assert calls == [
        "http://127.0.0.1:8776/api/supervisor/update/cancel",
        "http://127.0.0.1:8777/api/admin/update/cancel",
    ]
