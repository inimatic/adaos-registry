from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


def _load_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_web_desktop_runtime_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_connect_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "connect.py"
    module_name = f"test_web_desktop_connect_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_prepare_connection_is_owned_by_desktop_without_legacy_projection(
    monkeypatch,
) -> None:
    mod = _load_connect_module()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mod.sdk_access,
        "require",
        lambda permission: calls.append(("require", permission)),
    )
    monkeypatch.setattr(
        mod,
        "_resolve_context",
        lambda: {
            "cfg": SimpleNamespace(),
            "hub_id": "sn_test",
            "zone_id": "ru",
            "verify": True,
            "cert_tuple": None,
            "root_base_url": "https://ru.api.inimatic.com",
            "app_base_url": "https://inimatic.com",
        },
    )

    def _prepare(mode, context, *, request_id):
        current = mod._decorate_current(
            mod._base_current(mode),
            context,
            request_id=request_id,
            status="ready",
            busy=False,
        )
        current["code"] = "PAIR-1"
        mod._apply_expiry_fields(current, expires_at=1_900_000_000)
        return current

    monkeypatch.setattr(mod, "_prepare_current", _prepare)

    result = asyncio.run(
        mod.prepare_connection(mode="browser", webspace_id="desktop", refresh=True)
    )

    assert result["ok"] is True
    assert result["current"]["code"] == "PAIR-1"
    assert calls == [("require", "workspace.write")]
    source = (Path(mod.__file__).read_text(encoding="utf-8"))
    assert "data/adaos_connect" not in source
    assert "@subscribe" not in source


def _authorized(monkeypatch, mod, calls: list[tuple[str, object]]) -> None:
    monkeypatch.setattr(
        mod.sdk_access,
        "require",
        lambda permission: calls.append(("require", permission)),
    )


def test_list_applications_projects_installed_launchable_rows(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)

    def _list_applications(**kwargs):
        calls.append(("list", kwargs))
        return [
            {
                "icon": "apps",
                "application": {
                    "application_id": "adaos.applications",
                    "display": {
                        "title": "Applications",
                        "summary": "Install and update apps",
                        "categories": ["system"],
                    },
                    "publisher": {"display_name": "AdaOS", "name": "AdaOS"},
                    "entrypoints": [
                        {
                            "entrypoint_id": "main",
                            "presentation_ref": "scenario:applications",
                        }
                    ],
                },
                "installed": True,
                "update_available": False,
                "effective_release": {"version": "2.4.1", "update_track": "stable"},
            }
        ]

    monkeypatch.setattr(mod.sdk_applications, "list_applications", _list_applications)

    result = mod.list_applications(installed_only=True, launchable_only=True)

    assert calls == [
        ("require", "workspace.read"),
        (
            "list",
            {
                "installed_only": False,
                "catalog_only": False,
                "available_only": False,
                "developed_only": False,
                "include_development": False,
            },
        ),
    ]
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["total"] == 1
    assert result["truncated"] is False
    assert result["items"][0] == {
        "id": "adaos.applications",
        "title": "Applications",
        "summary": "Install and update apps",
        "icon": "apps",
        "publisher": "AdaOS",
        "categories": ["system"],
        "category": "system",
        "installed": True,
        "installed_state": "installed",
        "local_beta_active": False,
        "effective_version": "2.4.1",
        "effective_channel": "stable",
        "channel": "stable",
        "update_state": "current",
        "has_update": False,
        "scenario_id": "applications",
        "launchable": True,
        "pinned": False,
        "home_order": 0,
    }


def test_list_applications_coalesces_immediate_identical_registry_reads(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)

    def _list_applications(**kwargs):
        calls.append(("list", kwargs))
        return []

    monkeypatch.setattr(mod.sdk_applications, "list_applications", _list_applications)

    mod.list_applications(installed_only=True, limit=24)
    mod.list_applications(installed_only=True, limit=40)

    assert [kind for kind, _ in calls].count("list") == 1


def test_list_applications_coalesces_installed_and_marketplace_registry_reads(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)

    def _list_applications(**kwargs):
        calls.append(("list", kwargs))
        return [
            {
                "application": {
                    "application_id": "notes",
                    "visibility": "public",
                    "entrypoints": [{"presentation_ref": "scenario:notes"}],
                },
                "installed": False,
                "marketplace_release": {"version": "1.0.0"},
            }
        ]

    monkeypatch.setattr(mod.sdk_applications, "list_applications", _list_applications)

    installed = mod.list_applications(installed_only=True)
    marketplace = mod.list_applications(catalog_only=True, available_only=True)

    assert installed["count"] == 0
    assert marketplace["count"] == 1
    assert [kind for kind, _ in calls].count("list") == 1


def test_list_applications_treats_local_beta_as_installed(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_applications,
        "list_applications",
        lambda **_: [
            {
                "application": {
                    "application_id": "builder",
                    "display": {"title": "Builder"},
                    "entrypoints": [{"presentation_ref": "scenario:builder"}],
                },
                "local_beta_active": True,
                "effective_release": {"version": "0.9.5"},
            }
        ],
    )

    result = mod.list_applications(installed_only=True, launchable_only=True)

    assert result["items"][0]["installed"] is True
    assert result["items"][0]["installed_state"] == "installed"
    assert result["items"][0]["effective_channel"] == "Beta"
    assert result["items"][0]["scenario_id"] == "builder"
    assert result["items"][0]["launchable"] is True


def test_list_applications_returns_non_installed_catalog_rows(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_applications,
        "list_applications",
        lambda **_: [
            {
                "application": {
                    "application_id": "notes",
                    "display": {
                        "title": "Notes",
                        "summary": "Quick capture",
                        "categories": ["productivity"],
                    },
                    "entrypoints": [{"presentation_ref": "scenario:notes"}],
                },
                "installed": False,
                "marketplace_release": {"version": "0.8.1"},
                "effective_release": {"version": "0.8.1", "update_track": "stable"},
            }
        ],
    )

    result = mod.list_applications(catalog_only=True, available_only=True)

    assert result["ok"] is True
    assert result["items"][0]["installed"] is False
    assert result["items"][0]["launchable"] is False
    assert result["items"][0]["summary"] == "Quick capture"


def test_list_applications_keeps_missing_entrypoint_visible_but_not_launchable(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_applications,
        "list_applications",
        lambda **_: [
            {
                "application": {
                    "application_id": "headless",
                    "display": {"title": "Headless"},
                    "entrypoints": [],
                },
                "installed": True,
            }
        ],
    )

    result = mod.list_applications(installed_only=True)

    assert result["items"][0]["id"] == "headless"
    assert result["items"][0]["scenario_id"] is None
    assert result["items"][0]["launchable"] is False


def test_list_applications_returns_explicit_empty_projection(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(mod.sdk_applications, "list_applications", lambda **_: [])

    result = mod.list_applications()

    assert result == {
        "ok": True,
        "items": [],
        "count": 0,
        "total": 0,
        "empty": True,
        "truncated": False,
    }


def test_list_applications_returns_explicit_sdk_error_projection(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)

    def _fail(**_):
        raise RuntimeError("sdk unavailable")

    monkeypatch.setattr(mod.sdk_applications, "list_applications", _fail)

    result = mod.list_applications()

    assert result["ok"] is False
    assert result["error"] == "applications_list_failed"
    assert result["message"] == "sdk unavailable"
    assert result["items"] == []
    assert result["count"] == 0
    assert result["total"] == 0
    assert result["empty"] is True
    assert result["truncated"] is False


def test_unselected_detail_reads_skip_expensive_sources(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)

    def fail(*_args, **_kwargs):
        raise AssertionError("unselected detail must not query its source")

    monkeypatch.setattr(mod.sdk_applications, "list_applications", fail)
    monkeypatch.setattr(mod.sdk_control_plane, "list_device_objects", fail)
    monkeypatch.setattr(mod.sdk_control_plane, "list_browser_session_objects", fail)
    monkeypatch.setattr(mod.sdk_applications, "list_development_projects", fail)

    assert mod.list_applications(require_selection=True) == mod._empty_collection()
    assert mod.list_devices(require_selection=True) == mod._empty_collection()
    assert mod.list_developments(require_selection=True) == mod._empty_collection()
    assert calls == [("require", "workspace.read")] * 3


def test_list_applications_applies_query_and_bounded_limit(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_applications,
        "list_applications",
        lambda **_: [
            {
                "application": {
                    "application_id": f"notes-{index:03d}",
                    "display": {"title": f"Notes {index:03d}", "summary": "Capture"},
                }
            }
            for index in range(120)
        ],
    )

    result = mod.list_applications(query="notes", limit=20)

    assert result["count"] == 20
    assert result["total"] == 120
    assert result["truncated"] is True
    assert result["items"][0]["id"] == "notes-000"


def test_list_applications_projects_only_home_pinned_launchers_in_pinned_order(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod,
        "desktop_get_pinned_applications",
        lambda webspace_id=None: [
            "scenario:users_access",
            "scenario:applications",
        ],
    )
    monkeypatch.setattr(
        mod.sdk_applications,
        "list_applications",
        lambda **_: [
            {
                "application": {
                    "application_id": "applications",
                    "display": {"title": "Applications"},
                    "entrypoints": [{"presentation_ref": "scenario:applications"}],
                },
                "installed": True,
            },
            {
                "application": {
                    "application_id": "notes",
                    "display": {"title": "Notes"},
                    "entrypoints": [{"presentation_ref": "scenario:notes"}],
                },
                "installed": True,
            },
            {
                "application": {
                    "application_id": "users_access",
                    "display": {"title": "Users & Access"},
                    "entrypoints": [{"presentation_ref": "scenario:users_access"}],
                },
                "installed": True,
            },
        ],
    )

    result = mod.list_applications(pinned_only=True, launchable_only=True)

    assert [item["id"] for item in result["items"]] == [
        "users_access",
        "applications",
    ]
    assert [item["home_order"] for item in result["items"]] == [0, 1]
    assert all(item["pinned"] is True for item in result["items"])


def test_list_devices_is_subnet_wide_and_webspace_filter_is_explicit(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_device_objects",
        lambda: [
            {
                "id": "device:node",
                "kind": "device",
                "title": "Home node",
                "status": "online",
                "runtime": {"device_kind": "member", "connection_state": "heartbeat"},
            },
        ],
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_browser_session_objects",
        lambda: [
            {
                "id": "browser:current",
                "kind": "browser_session",
                "title": "Current browser",
                "status": "online",
                "runtime": {"connection_state": "open"},
                "relations": {"workspace": ["workspace:desktop"]},
            },
            {
                "id": "browser:other",
                "kind": "browser_session",
                "title": "Other browser",
                "status": "online",
                "runtime": {"connection_state": "open"},
                "relations": {"workspace": ["workspace:other"]},
            },
        ],
    )

    result = mod.list_devices(
        webspace_id="desktop",
        controller_endpoint_id="browser:current:surface:test:webspace:desktop",
        status="all",
    )
    scoped = mod.list_devices(
        webspace_id="desktop",
        webspace_only=True,
        controller_endpoint_id="browser:current:surface:test:webspace:desktop",
        status="all",
    )

    assert calls == [("require", "workspace.read"), ("require", "workspace.read")]
    assert [item["id"] for item in result["items"]] == [
        "browser:current",
        "device:node",
        "browser:other",
    ]
    assert [item["id"] for item in scoped["items"]] == [
        "browser:current",
        "device:node",
    ]
    assert result["items"][0]["current"] is True
    assert all("runtime" not in item for item in result["items"])


def test_list_devices_defaults_to_active_and_keeps_offline_filter_explicit(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_device_objects",
        lambda: [
            {
                "id": "device:online",
                "title": "Online",
                "status": "online",
                "audit": {"last_seen": 1_789_851_965.0},
            },
            {
                "id": "device:offline",
                "title": "Offline",
                "status": "offline",
                "audit": {"last_seen": 1_789_851_800.0},
            },
        ],
    )
    monkeypatch.setattr(mod.sdk_control_plane, "list_browser_session_objects", list)

    active = mod.list_devices()
    offline = mod.list_devices(status="offline")

    assert [item["id"] for item in active["items"]] == ["device:online"]
    assert [item["id"] for item in offline["items"]] == ["device:offline"]
    assert active["items"][0]["last_seen"].endswith("+00:00")


def test_list_devices_prefers_enriched_access_link_identity(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_device_objects",
        lambda: [
            {
                "id": "device:tv-01",
                "kind": "device",
                "title": "tv-01",
                "status": "online",
                "runtime": {"device_kind": "browser", "connection_state": "connected"},
            },
        ],
    )
    monkeypatch.setattr(mod.sdk_control_plane, "list_browser_session_objects", list)
    monkeypatch.setattr(
        mod.sdk_access_links,
        "list_browser_links",
        lambda: [
            {
                "id": "tv-01",
                "title": "AndroidTV",
                "effective_name": "AndroidTV",
                "endpoint_display_name": "Living room",
                "access_class": "device",
                "online": True,
                "connection_state": "connected",
                "last_webspace_id": "television",
                "last_seen_at": 1_789_851_965.0,
            },
        ],
    )

    result = mod.list_devices(status="active")

    assert calls == [("require", "workspace.read")]
    assert result["total"] == 1
    assert result["items"][0] == {
        "id": "browser:tv-01",
        "title": "AndroidTV",
        "summary": "device | Living room",
        "kind": "browser_session",
        "status": "online",
        "source": "access_links",
        "last_seen": "2026-09-19T21:06:05+00:00",
        "version": "",
        "location": "",
        "current": False,
        "device_type": "browser",
        "connection": "connected",
        "route_mode": "",
        "workspace_ids": ["television"],
        "owner": "",
        "access_class": "device",
        "device_display_name": "",
        "endpoint_display_name": "Living room",
    }


def test_system_overview_returns_bounded_section_without_raw_projection(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_self_object",
        lambda: {
            "id": "hub:home",
            "kind": "hub",
            "title": "Home",
            "status": "online",
        },
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_reliability_projection",
        lambda **_: {
            "subject": {
                "id": "hub:home",
                "kind": "hub",
                "title": "Home",
                "status": "online",
            },
            "objects": [],
            "incidents": [
                {"id": "incident:one", "title": "Review", "severity": "warning"}
            ],
        },
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_local_capacity_object",
        lambda: {"resources": {"active_skill_total": 3, "active_scenario_total": 2}},
    )

    result = mod.get_system_overview(webspace_id="desktop", section="incidents")

    assert result["subject"]["id"] == "hub:home"
    assert result["items"] == [
        {"id": "incident:one", "title": "Review", "severity": "warning"}
    ]
    assert result["metrics"]["capacity"]["value"] == 3
    assert "overview" not in result
    assert "reliability" not in result


def test_system_summary_does_not_claim_reliability_was_inspected(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_self_object",
        lambda: {"id": "hub:home", "kind": "hub", "title": "Home", "status": "online"},
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_local_capacity_object",
        lambda: {"resources": {"active_skill_total": 3, "active_scenario_total": 2}},
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_reliability_projection",
        lambda **_: (_ for _ in ()).throw(AssertionError("summary must stay fast")),
    )

    result = mod.get_system_overview(webspace_id="desktop", section="summary")

    assert result["metrics"]["services"]["value"] == 5
    assert result["metrics"]["incidents"]["value"] == "--"
    assert "inspect" in result["metrics"]["incidents"]["description"].casefold()


def test_system_all_returns_one_complete_shared_snapshot(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_self_object",
        lambda: {"id": "hub:home", "kind": "hub", "title": "Home", "status": "online"},
    )
    reliability_calls: list[str] = []
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_reliability_projection",
        lambda **_: reliability_calls.append("read") or {
            "subject": {
                "id": "hub:home",
                "kind": "hub",
                "title": "Home",
                "status": "online",
            },
            "objects": [
                {
                    "id": "runtime:one",
                    "kind": "runtime",
                    "title": "Runtime",
                    "status": "online",
                },
                {
                    "id": "connection:one",
                    "kind": "connection",
                    "title": "Route",
                    "status": "online",
                },
            ],
            "incidents": [
                {"id": "incident:one", "title": "Review", "severity": "warning"}
            ]
        },
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_local_capacity_object",
        lambda: {"resources": {"active_skill_total": 3, "active_scenario_total": 2}},
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_runtime_objects",
        lambda **_: [
            {
                "id": "runtime:one",
                "kind": "runtime",
                "title": "Runtime",
                "status": "online",
            }
        ],
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_connection_objects",
        lambda **_: [
            {
                "id": "connection:one",
                "kind": "connection",
                "title": "Route",
                "status": "online",
            }
        ],
    )
    monkeypatch.setattr(mod.sdk_control_plane, "list_quota_objects", lambda **_: [])

    result = mod.get_system_overview(webspace_id="desktop", section="all")

    assert [item["id"] for item in result["services"]] == ["runtime:one"]
    assert [item["id"] for item in result["connections"]] == ["connection:one"]
    assert result["incidents"][0]["id"] == "incident:one"
    assert result["metrics"]["services"]["value"] == 1
    assert result["metrics"]["incidents"]["value"] == 1
    assert reliability_calls == ["read"]


def test_system_connections_requests_only_the_required_sdk_section(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    observed: list[set[str]] = []
    monkeypatch.setattr(
        mod.sdk_system,
        "get_operational_snapshot",
        lambda **kwargs: observed.append(set(kwargs["sections"]))
        or {
            "subject": {"id": "hub:home", "kind": "hub", "title": "Home", "status": "online"},
            "capacity": {"resources": {}},
            "connections": [
                {"id": "connection:root", "kind": "connection", "title": "Root", "status": "online"}
            ],
        },
    )

    result = mod.get_system_overview(webspace_id="desktop", section="connections")

    assert observed == [{"summary", "connections"}]
    assert result["items"][0]["id"] == "connection:root"
    assert result["services"] == []


def test_list_developments_removes_local_paths_and_projects_scenario(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_applications,
        "list_development_projects",
        lambda **_: [
            {
                "id": "calibration",
                "title": "[Calibration] old",
                "version": "0.1.0",
                "primary_ref": "skill:calibration",
                "validation_status": "valid",
            },
            {
                "id": "notes",
                "title": "Notes",
                "description": "Capture notes",
                "version": "1.2.3",
                "updated_at": "2026-09-19T10:00:00Z",
                "primary_ref": "scenario:notes",
                "source_path": "D:/private/dev/notes",
                "validation_status": "valid",
            },
        ],
    )

    result = mod.list_developments()

    assert result["items"][0]["scenario_id"] == "notes"
    assert result["items"][0]["launchable"] is True
    assert "source_path" not in result["items"][0]
    assert result["items"][1]["id"] == "calibration"


def test_preferences_read_and_update_use_allowlisted_profile_fields(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    profile = {
        "user_id": "owner",
        "display_name": "Owner",
        "timezone": "UTC",
        "language": "en",
        "preferences": {"theme": "system"},
    }
    monkeypatch.setattr(mod.sdk_profile, "get_profile", lambda: profile)
    monkeypatch.setattr(
        mod.sdk_profile,
        "update_settings",
        lambda patch: calls.append(("profile", patch)),
    )
    monkeypatch.setattr(
        mod.sdk_profile,
        "update_preferences",
        lambda patch, device_override=False: calls.append(
            ("preferences", (patch, device_override))
        ),
    )

    read = mod.get_preferences()
    updated = mod.update_preferences(
        values={"profileName": "Dmitry", "theme": "dark", "unknown": "ignored"},
        device_override=True,
    )

    assert read["item"]["id"] == "current"
    assert ("profile", {"display_name": "Dmitry"}) in calls
    assert ("preferences", ({"theme": "dark"}, True)) in calls
    assert updated["updated_fields"] == ["display_name", "theme"]


def test_preferences_accept_current_desktop_start_destinations(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_profile,
        "get_profile",
        lambda: {
            "user_id": "owner",
            "preferences": {"start_destination": "apps"},
        },
    )
    monkeypatch.setattr(
        mod.sdk_profile,
        "update_preferences",
        lambda patch, device_override=False: calls.append(
            ("preferences", (patch, device_override))
        ),
    )

    assert mod.get_preferences()["startDestination"] == "home"
    accepted = mod.update_preferences(values={"startDestination": "system"})
    development = mod.update_preferences(values={"startDestination": "dev"})
    rejected = mod.update_preferences(values={"startDestination": "apps"})

    assert accepted["ok"] is True
    assert development["ok"] is True
    assert rejected["ok"] is False
    assert rejected["error"] == "invalid_start_destination"
    assert ("preferences", ({"start_destination": "system"}, False)) in calls
    assert ("preferences", ({"start_destination": "dev"}, False)) in calls


def test_preferences_hydrate_labels_from_current_runtime_context(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_profile,
        "get_profile",
        lambda: {"user_id": "owner", "preferences": {}},
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "get_workspace_object",
        lambda workspace_id: {"id": workspace_id, "title": "Home desktop"},
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_device_objects",
        lambda: [],
    )
    monkeypatch.setattr(
        mod.sdk_control_plane,
        "list_browser_session_objects",
        lambda: [
            {
                "id": "browser:browser-1:surface:page-1",
                "title": "Living room browser",
                "kind": "browser_session",
                "runtime": {"status": "online"},
                "relations": {"workspace": ["workspace:desktop"]},
            }
        ],
    )

    result = mod.get_preferences(
        webspace_id="desktop",
        controller_endpoint_id="browser:browser-1:surface:page-1",
    )

    assert result["workspaceLabel"] == "Home desktop"
    assert result["deviceLabel"] == "Living room browser"


def test_update_home_item_rejects_worker_local_application_mutations(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod,
        "application_get_pinned_panels",
        lambda **_: calls.append(("unexpected_widget_read", None)),
    )
    monkeypatch.setattr(
        mod,
        "application_set_pinned_panels",
        lambda *_, **__: calls.append(("unexpected_widget_write", None)),
    )

    result = mod.update_home_item(
        item_type="application",
        item_id="notes",
        action="move",
        to_index=0,
        webspace_id="home",
    )

    assert result["ok"] is False
    assert result["error"] == "authoritative_root_mcp_required"
    assert "applications.reorder_home" in result["message"]
    assert calls == [("require", "workspace.write")]


def test_update_home_item_rejects_application_unpin_without_install_mutation(
    monkeypatch,
) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    monkeypatch.setattr(
        mod.sdk_applications,
        "list_applications",
        lambda **_: calls.append(("unexpected_application_read", None)),
    )
    monkeypatch.setattr(
        mod,
        "application_set_pinned_panels",
        lambda *_, **__: calls.append(("unexpected_widget_write", None)),
    )

    result = mod.update_home_item(
        item_type="app",
        item_id="scenario:notes",
        action="unpin",
        webspace_id="home",
    )

    assert result == {
        "ok": False,
        "error": "authoritative_root_mcp_required",
        "message": (
            "Use applications.set_home_pin or applications.reorder_home so "
            "the Root-owned Home projection cannot be replaced by worker-local state."
        ),
    }
    assert "applications.set_home_pin" in result["message"]
    assert calls == [("require", "workspace.write")]


def test_home_widgets_read_and_update_pinned_panels(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, object]] = []
    _authorized(monkeypatch, mod, calls)
    panels = [
        {"id": "system-health", "type": "visual.metricTile", "title": "Health"},
        {"id": "activity", "type": "ui.list", "title": "Activity"},
    ]
    monkeypatch.setattr(
        mod,
        "application_get_pinned_panels",
        lambda webspace_id=None: list(panels),
    )

    def _set_panels(values, webspace_id=None, live=True):
        calls.append(("panels", list(values), webspace_id, live))

    monkeypatch.setattr(mod, "application_set_pinned_panels", _set_panels)

    read = mod.list_home_widgets(webspace_id="home")
    result = mod.update_home_item(
        item_type="widget",
        item_id="activity",
        action="move",
        to_index=0,
        webspace_id="home",
    )

    assert [item["id"] for item in read["items"]] == ["system-health", "activity"]
    assert [item["home_order"] for item in read["items"]] == [0, 1]
    assert result["ok"] is True
    assert calls[-1] == (
        "panels",
        [
            {"id": "activity", "type": "ui.list", "title": "Activity"},
            {"id": "system-health", "type": "visual.metricTile", "title": "Health"},
        ],
        "home",
        True,
    )
