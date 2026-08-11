from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


def _load_browsers_skill_module():
    if "adaos.sdk.data.ctx" not in sys.modules:
        fake_ctx = types.ModuleType("adaos.sdk.data.ctx")

        class _FakeSubnet:
            def set(self, slot, value, *, webspace_id=None):
                return None

            async def set_async(self, slot, value, *, webspace_id=None):
                return None

        fake_ctx.subnet = _FakeSubnet()
        fake_ctx.current_user = object()
        fake_ctx.selected_user = object()
        sys.modules["adaos.sdk.data.ctx"] = fake_ctx

    if "adaos.services.workspaces.index" not in sys.modules:
        fake_index = types.ModuleType("adaos.services.workspaces.index")
        fake_index.list_workspaces = lambda: []
        sys.modules["adaos.services.workspaces.index"] = fake_index
        if "adaos.services.workspaces" not in sys.modules:
            fake_pkg = types.ModuleType("adaos.services.workspaces")
            fake_pkg.index = fake_index
            sys.modules["adaos.services.workspaces"] = fake_pkg

    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_browsers_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    from adaos.sdk.data.projections import clear_projection_demand

    clear_projection_demand()
    return module


def _remember_browser_projection_demand(mod, webspace_id: str = "desktop") -> None:
    for slot in (
        "browsers.summary",
        "browsers.devices",
        "browsers.clients",
        "browsers.current_summary",
        "browsers.current_name",
    ):
        mod._PROJECTION_RUNTIME.remember_projection(
            slot,
            webspace_id=webspace_id,
            subscription_id=f"test:{webspace_id}:{slot}",
        )


def test_browsers_skill_detach_link_refreshes_snapshot_without_nameerror(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()
    _remember_browser_projection_demand(mod)
    mod._SELECTED_BROWSER_BY_WS["desktop"] = "missing-browser"

    browser_entry = {
        "id": "browser-1",
        "display_name": "Living room browser",
        "hostname": "tv-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    published: list[tuple[str, str | None, object]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        published.append((slot, webspace_id, value))

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(
        mod.workspace_index,
        "list_workspaces",
        lambda: [
            SimpleNamespace(workspace_id="desktop"),
            SimpleNamespace(workspace_id="default"),
        ],
    )
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")
    monkeypatch.setattr(
        mod.sdk_device_access,
        "detach_device",
        lambda device_ref: {"ok": True, "device_ref": str(device_ref or "").strip(), "entry": {"revoked": True}},
    )

    result = mod.detach_link(node_id="member-1", webspace_id="desktop")

    assert result["ok"] is True
    assert result["device_ref"] == "member:member-1"
    assert mod._SELECTED_BROWSER_BY_WS["desktop"] == "browser-1"
    assert any(slot == "browsers.current_name" and webspace_id == "desktop" for slot, webspace_id, _value in published)
    assert not any(slot == "browsers.current_name" and webspace_id == "default" for slot, webspace_id, _value in published)


def test_browsers_skill_projection_refresh_skips_unchanged_yjs_writes(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()
    _remember_browser_projection_demand(mod)

    browser_entry = {
        "id": "browser-1",
        "display_name": "Main browser",
        "hostname": "main-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    writes: list[tuple[str, str | None, object]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        writes.append((slot, webspace_id, value))

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    asyncio.run(mod._publish_snapshot("desktop"))
    first_write_count = len(writes)
    asyncio.run(mod._publish_snapshot("desktop"))

    assert first_write_count == 5
    assert len(writes) == first_write_count


def test_browsers_skill_browser_tiles_include_online_flag(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    tiles = mod._browser_tiles([
        {
            "id": "browser-1",
            "display_name": "Dev Browser",
            "device_display_name": "Мой телефон",
            "access_class": "device",
            "online": True,
        },
        {
            "id": "browser-2",
            "display_name": "Old Browser",
            "access_class": "device",
            "online": False,
        },
    ])

    assert tiles[0]["title"] == "Мой телефон"
    assert tiles[0]["device_ref"] == "browser:browser-1"
    assert tiles[0]["browser_device_id"] == "browser-1"
    assert tiles[0]["device_display_name"] == "Мой телефон"
    assert tiles[0]["endpoint_display_name"] == "Dev Browser"
    assert "Endpoint Dev Browser" in tiles[0]["subtitle"]
    assert "Device name: Мой телефон" in tiles[0]["content"]
    assert "Endpoint name: Dev Browser" in tiles[0]["content"]
    assert tiles[0]["online"] is True
    assert tiles[0]["status"] == "online"
    assert tiles[1]["online"] is False
    assert tiles[1]["status"] == "offline"


def test_browsers_skill_summary_labels_client_rows_as_endpoints(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._SNAPSHOT_CACHE.clear()

    entries = [
        {
            "id": "dev-phone",
            "display_name": "Chrome",
            "device_display_name": "Phone",
            "access_class": "device",
            "online": True,
        },
        {
            "id": "dev-phone::webrtc",
            "display_name": "Chrome",
            "device_display_name": "Phone",
            "access_class": "client",
            "online": True,
            "parent_browser_device_id": "dev-phone",
        },
    ]

    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(entry) for entry in entries])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: next((dict(entry) for entry in entries if entry["id"] == device_id), None),
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    payload, _webspace_id = mod._build_snapshot("desktop")

    assert payload["summary"]["subtitle"] == "1 devices | 1 endpoints"
    assert payload["devices"][0]["title"] == "Phone"
    assert payload["clients"][0]["title"] == "Chrome"


def test_browsers_skill_surfaces_client_build_version(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")
    entry = {
        "id": "browser-1",
        "display_name": "Dev Browser",
        "access_class": "client",
        "online": True,
        "client_build_version": "0.0.67+08ad430",
    }

    tiles = mod._browser_tiles([dict(entry)])
    monkeypatch.setattr(mod.sdk_access_links, "get_browser_link", lambda device_id: dict(entry))

    summary, _name, _device_name = mod._current_browser_payload("browser-1")

    assert tiles[0]["client_build_version"] == "0.0.67+08ad430"
    assert "client 0.0.67+08ad430" in tiles[0]["subtitle"]
    assert "Client version: 0.0.67+08ad430" in tiles[0]["content"]
    assert {"title": "Client version", "description": "0.0.67+08ad430"} in summary


def test_browsers_skill_explicit_refresh_recomputes_without_rewriting_identical_yjs(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()
    _remember_browser_projection_demand(mod)

    browser_entry = {
        "id": "browser-1",
        "display_name": "Main browser",
        "hostname": "main-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    writes: list[tuple[str, str | None, object]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        writes.append((slot, webspace_id, value))

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    assert mod.refresh_snapshot("desktop")["delivery"] == "projection"
    assert mod.refresh_snapshot("desktop")["delivery"] == "projection"

    assert len(writes) == 5
    assert mod._PROJECTION_RUNTIME.diagnostics_snapshot()["skipped_unchanged_total"] == 5


def test_browsers_skill_projection_refresh_does_not_eager_publish_streams(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()

    browser_entry = {
        "id": "browser-1",
        "display_name": "Main browser",
        "hostname": "main-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    streams: list[tuple[str, object, dict[str, object] | None]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        return None

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(mod, "stream_publish", lambda receiver, data, _meta=None: streams.append((receiver, data, _meta)))
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    asyncio.run(mod._publish_snapshot("desktop"))
    assert streams == []

    mod._publish_stream_snapshot("browsers.devices", "desktop")

    assert [item[0] for item in streams] == ["browsers.devices"]
    assert streams[0][2]["webspace_id"] == "desktop"


def test_browsers_skill_snapshot_request_uses_fingerprint_guard(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()

    browser_entry = {
        "id": "browser-1",
        "display_name": "Main browser",
        "hostname": "main-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    streams: list[tuple[str, object, dict[str, object] | None]] = []

    monkeypatch.setattr(mod, "stream_publish", lambda receiver, data, _meta=None: streams.append((receiver, data, _meta)))
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    event = {"receiver": "browsers.summary", "webspace_id": "desktop"}

    mod.on_webio_stream_snapshot_requested(event)
    mod.on_webio_stream_snapshot_requested(event)

    assert [item[0] for item in streams] == ["browsers.summary"]
    assert streams[0][2]["webspace_id"] == "desktop"


def test_browsers_skill_refresh_event_schedules_without_inline_snapshot(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    scheduled: list[dict[str, object]] = []

    def _unexpected_build(_target_ws=None):
        raise AssertionError("background refresh event must not build snapshot inline")

    monkeypatch.setattr(mod, "_build_snapshot", _unexpected_build)
    monkeypatch.setattr(
        mod,
        "_schedule_publish_snapshot",
        lambda target_ws=None, *, fanout=False, force=False: scheduled.append(
            {"target_ws": target_ws, "fanout": fanout, "force": force}
        ),
    )

    async def _run() -> None:
        mod._on_refresh({"type": "browser.session.changed", "webspace_id": "dev1"})

    mod._on_refresh({"type": "browser.session.changed", "webspace_id": "dev1"})
    asyncio.run(_run())

    assert scheduled == [
        {"target_ws": "dev1", "fanout": False, "force": False},
        {"target_ws": "dev1", "fanout": False, "force": False},
    ]


def test_browsers_skill_yjs_projection_requires_active_demand(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()

    async def _unexpected_set_async(slot, value, *, webspace_id=None):
        raise AssertionError("inactive browsers projections must not write Yjs")

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _unexpected_set_async)
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "get_browser_link", lambda _device_id: None)
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    asyncio.run(mod._publish_snapshot("desktop"))

    diagnostics = mod._PROJECTION_RUNTIME.diagnostics_snapshot()
    assert diagnostics["pressure_blocked_total"] == 6
    assert diagnostics["last_result"]["reason"] == "no_active_projection_demand"


def test_browsers_skill_stream_payload_filters_online_only_by_default(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()

    entries = [
        {
            "id": "browser-online",
            "display_name": "Online browser",
            "access_class": "device",
            "online": True,
        },
        {
            "id": "browser-offline",
            "display_name": "Offline browser",
            "access_class": "device",
            "online": False,
        },
    ]
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(item) for item in entries])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: next((dict(item) for item in entries if item["id"] == device_id), None),
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    default_rows = mod._build_stream_payload("browsers.devices", "desktop")
    all_rows = mod._build_stream_payload("browsers.devices", "desktop", params={"online_only": False})

    assert [item["id"] for item in default_rows] == ["browser-online"]
    assert {item["id"] for item in all_rows} == {"browser-online", "browser-offline"}


def test_browsers_skill_selected_browser_summary_uses_selected_live_entry(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()

    entries = [
        {
            "id": "browser-offline",
            "display_name": "Offline browser",
            "access_class": "device",
            "online": False,
        },
        {
            "id": "browser-online::tab-1",
            "display_name": "Online browser",
            "access_class": "client",
            "lifetime_mode": "fixed",
            "online": True,
            "last_webspace_id": "desktop",
            "parent_browser_device_id": "browser-online",
        },
    ]
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(item) for item in entries])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: next((dict(item) for item in entries if item["id"] == device_id), None),
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda entry: "Fixed" if entry.get("access_class") == "client" else "Permanent")

    result = mod.select_browser("browser-online::tab-1", webspace_id="desktop")
    summary = mod._build_stream_payload("browsers.current_summary", "desktop")
    current_name = mod._build_stream_payload("browsers.current_name", "desktop")

    assert result["selected_device_id"] == "browser-online::tab-1"
    assert {"title": "Endpoint", "description": "Online browser"} in summary
    assert {"title": "Status", "description": "online"} in summary
    assert current_name == {"value": "Online browser", "device_id": "browser-online::tab-1"}


def test_browsers_skill_current_stream_prefers_explicit_device_parameter(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()

    entries = [
        {"id": "browser-stale", "display_name": "Browser on iOS", "access_class": "device", "online": False},
        {"id": "browser-current", "display_name": "Chrome on Windows", "access_class": "device", "online": True},
    ]
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(item) for item in entries])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: next((dict(item) for item in entries if item["id"] == device_id), None),
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")
    mod._SELECTED_BROWSER_BY_WS["desktop"] = "browser-stale"

    summary = mod._build_stream_payload(
        "browsers.current_summary",
        "desktop",
        params={"device_id": "browser-current"},
    )

    assert {"title": "Endpoint", "description": "Chrome on Windows"} in summary
    assert {"title": "Status", "description": "online"} in summary
    assert mod._SELECTED_BROWSER_BY_WS["desktop"] == "browser-stale"


def test_browsers_skill_rename_browser_device_name_refreshes_without_renaming_endpoint(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    seen: list[tuple[str, str]] = []

    def _fake_rename(device_ref: str, name: str):
        seen.append((device_ref, name))
        return {
            "ok": True,
            "device_ref": device_ref,
            "entry": {
                "display_name": "Chrome",
                "device_display_name": name,
            },
        }

    refreshes: list[str | None] = []
    monkeypatch.setattr(mod.sdk_device_access, "rename_browser_device_name", _fake_rename)
    monkeypatch.setattr(mod, "_refresh_snapshot_sync", lambda webspace_id=None: refreshes.append(webspace_id))

    result = mod.rename_browser_device_name(
        "Мой телефон",
        browser_device_id="dev-browser",
        webspace_id="desktop",
    )

    assert result["ok"] is True
    assert result["entry"]["display_name"] == "Chrome"
    assert result["entry"]["device_display_name"] == "Мой телефон"
    assert seen == [("browser:dev-browser", "Мой телефон")]
    assert refreshes == ["desktop"]


def test_browsers_skill_refresh_event_handler_does_not_wait_for_projection(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._PENDING_REFRESH_BY_WS.clear()
    submitted: list[object] = []

    class _Future:
        def done(self) -> bool:
            return False

        def add_done_callback(self, callback):
            self.callback = callback

        def result(self):
            raise AssertionError("event-loop refresh must not wait for projection result")

    class _Executor:
        def submit(self, fn):
            submitted.append(fn)
            for cell in fn.__closure__ or ():
                value = cell.cell_contents
                if inspect.iscoroutine(value):
                    value.close()
            return _Future()

    monkeypatch.setattr(mod, "_PROJECTION_EXECUTOR", _Executor())
    monkeypatch.setattr(
        mod,
        "_build_snapshot",
        lambda target_ws=None: (
            {
                "summary": {},
                "devices": [],
                "clients": [],
                "current_summary": [],
                "current_name": {"value": ""},
            },
            str(target_ws or "desktop"),
        ),
    )

    async def _invoke() -> None:
        mod._on_refresh(SimpleNamespace(payload={"webspace_id": "desktop"}))

    asyncio.run(_invoke())

    assert submitted
    assert "desktop" in mod._PENDING_REFRESH_BY_WS


def test_browsers_skill_runtime_dispose_clears_pending_state_and_executor() -> None:
    mod = _load_browsers_skill_module()

    shutdown_calls: list[tuple[bool, bool]] = []

    class _Future:
        def cancel(self) -> bool:
            return True

    class _Executor:
        def shutdown(self, *, wait=False, cancel_futures=False):
            shutdown_calls.append((wait, cancel_futures))

    mod._PENDING_REFRESH_BY_WS["desktop"] = _Future()
    mod._SELECTED_BROWSER_BY_WS["desktop"] = "browser-1"
    mod._PROJECTION_EXECUTOR = _Executor()

    result = mod.browsers_runtime_dispose(reason="test")

    assert result["ok"] is True
    assert result["pending_total"] == 1
    assert result["cancelled_total"] == 1
    assert result["selected_total"] == 1
    assert mod._PENDING_REFRESH_BY_WS == {}
    assert mod._SELECTED_BROWSER_BY_WS == {}
    assert mod._PROJECTION_EXECUTOR is None
    assert shutdown_calls == [(False, True)]


def test_browsers_skill_get_link_settings_uses_sdk_device_access(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    expected = {
        "device_ref": "member:member-2",
        "title": "Kitchen tablet",
        "detach": {"enabled": True, "confirm_message": 'Detach device "Kitchen tablet"?'},
    }
    seen: list[str] = []

    def _fake_get_device_settings(device_ref: str):
        seen.append(str(device_ref or "").strip())
        return dict(expected)

    monkeypatch.setattr(mod.sdk_device_access, "get_device_settings", _fake_get_device_settings)

    result = mod.get_link_settings(node_id="member-2")

    assert result == expected
    assert seen == ["member:member-2"]


def test_browsers_skill_get_device_settings_accepts_generic_device_ref(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    expected = {
        "device_ref": "member:member-4",
        "title": "Workshop display",
    }
    seen: list[str] = []

    def _fake_get_device_settings(device_ref: str):
        seen.append(str(device_ref or "").strip())
        return dict(expected)

    monkeypatch.setattr(mod.sdk_device_access, "get_device_settings", _fake_get_device_settings)

    result = mod.get_device_settings(device_ref="member:member-4")

    assert result == expected
    assert seen == ["member:member-4"]


def test_browsers_skill_rename_device_uses_generic_device_ref(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    seen: list[tuple[str, str]] = []

    monkeypatch.setattr(mod, "_refresh_snapshot_sync", lambda webspace_id=None: {"ok": True, "webspace_id": webspace_id})

    def _fake_rename(device_ref: str, display_name: str):
        seen.append((str(device_ref or "").strip(), str(display_name or "").strip()))
        return {"ok": True, "device_ref": device_ref}

    monkeypatch.setattr(mod.sdk_device_access, "rename_device", _fake_rename)

    result = mod.rename_device(device_ref="member:member-4", name="Workshop display", webspace_id="desktop")

    assert result == {"ok": True, "device_ref": "member:member-4"}
    assert seen == [("member:member-4", "Workshop display")]


def test_browsers_skill_current_payload_exposes_device_name_suggestions(monkeypatch) -> None:
    mod = _load_browsers_skill_module()

    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: {
            "id": "dev-phone",
            "access_class": "device",
            "device_display_name": "My phone",
            "display_name": "Chrome",
            "online": True,
            "lifetime_mode": "permanent",
        },
    )
    monkeypatch.setattr(
        mod.sdk_device_access,
        "list_registered_device_names",
        lambda: [{"value": "My phone", "label": "My phone", "subtitle": "1/1 online | browser"}],
    )

    summary, endpoint_name, device_name = mod._current_browser_payload("dev-phone")

    assert summary[1]["title"] == "Endpoint"
    assert endpoint_name["value"] == "Chrome"
    assert device_name["value"] == "My phone"
    assert device_name["suggestions"][0]["value"] == "My phone"


def test_browsers_skill_identify_device_uses_sdk_device_access(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    seen: list[tuple[str, str | None]] = []

    def _fake_identify(device_ref: str, webspace_id: str | None = None):
        seen.append((str(device_ref or "").strip(), webspace_id))
        return {"ok": True, "device_ref": device_ref, "request_id": "identify-1"}

    monkeypatch.setattr(mod.sdk_device_access, "identify_device", _fake_identify)

    result = mod.identify_device(device_ref="browser:dev-phone", webspace_id="desktop")

    assert result == {"ok": True, "device_ref": "browser:dev-phone", "request_id": "identify-1"}
    assert seen == [("browser:dev-phone", "desktop")]


def test_browsers_skill_set_browser_media_control_uses_sdk_device_access(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    seen: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_refresh_snapshot_sync", lambda webspace_id=None: {"ok": True, "webspace_id": webspace_id})

    def _fake_set(device_ref: str, **kwargs):
        seen.append({"device_ref": str(device_ref or "").strip(), **kwargs})
        return {"ok": True, "device_ref": device_ref}

    monkeypatch.setattr(mod.sdk_device_access, "set_browser_media_control", _fake_set)

    result = mod.set_browser_media_control(
        device_ref="browser:dev-phone",
        volume=0,
        muted=False,
        audio_output_device_id="speaker-1",
        webspace_id="desktop",
    )

    assert result == {"ok": True, "device_ref": "browser:dev-phone"}
    assert seen == [
        {
            "device_ref": "browser:dev-phone",
            "audio_input_device_id": None,
            "audio_input_label": None,
            "audio_output_device_id": "speaker-1",
            "audio_output_label": None,
            "volume": 0,
            "muted": False,
            "media_audio_input_device_id": None,
            "media_audio_input_label": None,
            "media_audio_output_device_id": None,
            "media_audio_output_label": None,
            "media_audio_output_volume": None,
            "media_audio_output_muted": None,
            "webspace_id": "desktop",
        }
    ]


def test_browsers_skill_adopt_link_uses_sdk_device_access(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    seen: list[tuple[str, str | None, str]] = []

    monkeypatch.setattr(mod, "_refresh_snapshot_sync", lambda webspace_id=None: {"ok": True, "webspace_id": webspace_id})

    def _fake_adopt(device_ref: str, display_name: str | None = None, preset: str = "permanent"):
        seen.append((str(device_ref or "").strip(), display_name, str(preset or "").strip()))
        return {"ok": True, "device_ref": device_ref}

    monkeypatch.setattr(mod.sdk_device_access, "adopt_device", _fake_adopt)

    result = mod.adopt_link(node_id="member-3", name="Workshop display", preset="7d", webspace_id="desktop")

    assert result == {"ok": True, "device_ref": "member:member-3"}
    assert seen == [("member:member-3", "Workshop display", "7d")]
