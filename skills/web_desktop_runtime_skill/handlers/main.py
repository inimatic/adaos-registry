from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any

from adaos.sdk import access as sdk_access
from adaos.sdk import applications as sdk_applications
from adaos.sdk import control_plane as sdk_control_plane
from adaos.sdk import system as sdk_system
from adaos.sdk.core.decorators import tool
from adaos.sdk.data import access_links as sdk_access_links
from adaos.sdk.data import profile as sdk_profile
from adaos.sdk.web import (
    application_get_pinned_panels,
    application_set_pinned_panels,
    desktop_get_pinned_applications,
)

_PREFERENCE_FIELDS: dict[str, str] = {
    "startDestination": "start_destination",
    "showPresence": "show_presence",
    "deviceLabel": "device_label",
    "theme": "theme",
    "density": "density",
    "textSize": "text_size",
    "reduceMotion": "reduce_motion",
    "workspaceLabel": "workspace_label",
    "notifyDevActivity": "notify_development_activity",
    "notifySecurity": "notify_security",
    "notifyDeviceStatus": "notify_device_status",
    "quietHours": "quiet_hours",
    "notifyAppUpdates": "notify_application_updates",
    "autoUpdate": "auto_update",
    "followPrerelease": "follow_prerelease",
    "updateWindow": "update_window",
    "meteredDownloads": "metered_downloads",
    "shareTelemetry": "share_telemetry",
    "activityRetention": "activity_retention_days",
}

_PREFERENCE_DEFAULTS: dict[str, Any] = {
    "startDestination": "home",
    "showPresence": True,
    "theme": "system",
    "density": "comfortable",
    "textSize": "default",
    "reduceMotion": False,
    "notifyDevActivity": False,
    "notifySecurity": True,
    "notifyDeviceStatus": True,
    "quietHours": "off",
    "notifyAppUpdates": True,
    "autoUpdate": True,
    "followPrerelease": False,
    "updateWindow": "any",
    "meteredDownloads": False,
    "shareTelemetry": False,
    "activityRetention": "90",
}

_START_DESTINATIONS = {
    "home",
    "devices",
    "chat",
    "activity",
    "settings",
    "system",
    "dev",
}

_APPLICATION_CACHE_TTL_SECONDS = 15.0
_APPLICATION_CACHE_LOCK = Lock()
_APPLICATION_CACHE: tuple[float, tuple[dict[str, Any], ...]] | None = None
_PROFILE_FIELDS: dict[str, str] = {
    "profileName": "display_name",
    "timeZone": "timezone",
    "language": "language",
}


def _application_id(value: Any) -> str:
    token = str(value or "").strip()
    if token.startswith("app-"):
        return token[4:]
    if token.startswith("scenario:"):
        return token.split(":", 1)[1]
    return token


def _platform_application_id(value: Any) -> str:
    token = _application_id(value)
    if not token:
        return ""
    return token if token.startswith("scenario:") else f"scenario:{token}"


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_string(*values: Any) -> str:
    for value in values:
        text = _string(value)
        if text:
            return text
    return ""


def _bool_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "installed",
            "active",
        }
    return bool(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _bounded_limit(value: Any, *, default: int = 40, maximum: int = 40) -> int:
    try:
        requested = int(value or default)
    except (TypeError, ValueError):
        requested = default
    return max(1, min(requested, maximum))


def _empty_collection() -> dict[str, Any]:
    return {
        "ok": True,
        "items": [],
        "item": {},
        "count": 0,
        "total": 0,
        "empty": True,
        "truncated": False,
    }


def _timestamp_value(value: Any) -> float:
    token = _string(value)
    if not token:
        return 0.0
    try:
        return float(token)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _datetime_value(value: Any) -> str:
    token = _string(value)
    if not token:
        return ""
    timestamp = _timestamp_value(token)
    if not timestamp:
        return token
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _controller_device_token(value: Any) -> str:
    token = _string(value)
    if not token.startswith("browser:"):
        return ""
    return token.removeprefix("browser:").split(":surface:", 1)[0]


def _device_is_active(item: dict[str, Any]) -> bool:
    active_states = {"connected", "heartbeat", "online", "open", "ready"}
    return bool(
        item.get("current")
        or _string(item.get("status")).casefold() in active_states
        or _string(item.get("connection")).casefold() in active_states
    )


def _browser_identity(value: Any) -> str:
    token = _string(value)
    for prefix in ("device:", "browser:"):
        if token.startswith(prefix):
            return token.removeprefix(prefix)
    return token


def _browser_link_item(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    browser_id = _browser_identity(item.get("id"))
    status = "online" if _bool_value(item.get("online")) else "offline"
    endpoint_name = _first_string(
        item.get("endpoint_display_name"),
        item.get("endpoint_title"),
        item.get("display_name"),
        item.get("draft_name"),
    )
    device_name = _first_string(item.get("device_display_name"))
    access_class = _first_string(item.get("access_class"), "device").casefold()
    title = _first_string(
        item.get("title"),
        item.get("effective_name"),
        endpoint_name if access_class == "client" else device_name,
        device_name,
        endpoint_name,
        item.get("hostname"),
        browser_id,
        "Browser",
    )
    webspace_id = _first_string(item.get("last_webspace_id"))
    details = [access_class]
    if device_name and device_name.casefold() != title.casefold():
        details.append(device_name)
    if endpoint_name and endpoint_name.casefold() != title.casefold():
        details.append(endpoint_name)
    return {
        "id": f"browser:{browser_id}",
        "title": title,
        "summary": " | ".join(details),
        "kind": "browser_session",
        "status": status,
        "source": "access_links",
        "last_seen": _datetime_value(item.get("last_seen_at")),
        "version": _first_string(
            item.get("client_build_version"), item.get("client_version")
        ),
        "location": "",
        "current": False,
        "device_type": "browser",
        "connection": _first_string(item.get("connection_state"), status),
        "route_mode": _first_string(item.get("runtime_source")),
        "workspace_ids": [webspace_id] if webspace_id else [],
        "owner": "",
        "access_class": access_class,
        "device_display_name": device_name,
        "endpoint_display_name": endpoint_name,
    }


def _canonical_item(value: Any, *, source: str) -> dict[str, Any]:
    item = _mapping(value)
    audit = _mapping(item.get("audit"))
    runtime = _mapping(item.get("runtime"))
    actual = _mapping(item.get("actual_state"))
    resources = _mapping(item.get("resources"))
    versioning = _mapping(item.get("versioning"))
    relations = _mapping(item.get("relations"))
    governance = _mapping(item.get("governance"))
    kind = _first_string(item.get("kind"), source)
    status = _first_string(
        item.get("status"), runtime.get("status"), actual.get("status"), "unknown"
    )
    workspace_ids = [
        _string(workspace).split(":", 1)[-1]
        for workspace in list(relations.get("workspace") or [])
        if _string(workspace)
    ]
    return {
        "id": _first_string(
            item.get("id"), item.get("device_id"), item.get("session_id")
        ),
        "title": _first_string(
            item.get("title"), item.get("name"), item.get("label"), item.get("id")
        ),
        "summary": _first_string(item.get("summary"), item.get("description"), kind),
        "kind": kind,
        "status": status,
        "source": source,
        "last_seen": _datetime_value(
            _first_string(
                audit.get("last_seen"),
                runtime.get("last_seen"),
                actual.get("last_seen"),
            )
        ),
        "version": _first_string(
            versioning.get("current"),
            versioning.get("version"),
            runtime.get("runtime_version"),
            runtime.get("version"),
        ),
        "location": _first_string(
            runtime.get("location"), actual.get("location"), resources.get("location")
        ),
        "current": _bool_value(runtime.get("current", actual.get("current"))),
        "device_type": (
            "browser"
            if kind == "browser_session"
            else _first_string(runtime.get("device_kind"), kind)
        ),
        "connection": _first_string(runtime.get("connection_state"), status),
        "route_mode": _first_string(runtime.get("route_mode")),
        "workspace_ids": workspace_ids,
        "owner": _first_string(governance.get("owner_id")),
    }


def _preference_record(
    *,
    webspace_id: str | None = None,
    controller_endpoint_id: str | None = None,
) -> dict[str, Any]:
    profile = _mapping(sdk_profile.get_profile())
    preferences = _mapping(profile.get("preferences"))
    record: dict[str, Any] = {
        "id": "current",
        "profileName": _first_string(
            profile.get("display_name"), profile.get("user_id")
        ),
        "timeZone": _first_string(
            profile.get("timezone"), preferences.get("timezone"), "UTC"
        ),
        "language": _first_string(
            profile.get("language"), preferences.get("language"), "en"
        ),
    }
    for field, key in _PREFERENCE_FIELDS.items():
        if key in preferences or field in _PREFERENCE_DEFAULTS:
            record[field] = deepcopy(
                preferences.get(key, _PREFERENCE_DEFAULTS.get(field))
            )
    selected_webspace = _string(webspace_id)
    if not _string(record.get("workspaceLabel")) and selected_webspace:
        try:
            workspace = _mapping(
                sdk_control_plane.get_workspace_object(selected_webspace)
            )
        except Exception:
            workspace = {}
        record["workspaceLabel"] = _first_string(
            workspace.get("title"),
            workspace.get("display_name"),
            workspace.get("name"),
            selected_webspace,
        )
    if not _string(record.get("deviceLabel")):
        try:
            devices = list_devices(
                webspace_id=selected_webspace or None,
                controller_endpoint_id=controller_endpoint_id,
                status="all",
                limit=200,
            ).get("items", [])
        except Exception:
            devices = []
        current = next(
            (item for item in devices if _bool_value(_mapping(item).get("current"))),
            None,
        )
        record["deviceLabel"] = _first_string(
            _mapping(current).get("title"), "Current browser"
        )
    if record.get("startDestination") not in _START_DESTINATIONS:
        record["startDestination"] = "home"
    return record


def _categories(application: dict[str, Any]) -> list[str]:
    display = application.get("display")
    display = display if isinstance(display, dict) else {}
    raw = display.get("categories", application.get("categories"))
    if raw is None:
        raw = application.get("category")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    return [_string(item) for item in values if _string(item)]


def _publisher(application: dict[str, Any]) -> str:
    publisher = application.get("publisher")
    if isinstance(publisher, dict):
        return _first_string(
            publisher.get("display_name"),
            publisher.get("title"),
            publisher.get("name"),
            publisher.get("publisher_ref"),
            publisher.get("id"),
        )
    return _first_string(publisher, application.get("publisher_ref"))


def _entrypoint_scenario_id(entrypoint: Any) -> str | None:
    if isinstance(entrypoint, str):
        token = entrypoint.strip()
        if token.startswith("scenario:") and len(token) > len("scenario:"):
            return token.split(":", 1)[1]
        return None
    if not isinstance(entrypoint, dict):
        return None
    for key in ("presentation_ref", "ref", "target", "presentation", "scenario", "id"):
        value = entrypoint.get(key)
        if isinstance(value, str):
            scenario_id = _entrypoint_scenario_id(value)
            if scenario_id:
                return scenario_id
    return None


def _scenario_id(application: dict[str, Any]) -> str | None:
    raw_entrypoints = application.get("entrypoints")
    if raw_entrypoints is None:
        raw_entrypoints = application.get("entry_points")
    if raw_entrypoints is None:
        raw_entrypoints = application.get("entrypoint")
    if isinstance(raw_entrypoints, dict):
        candidates = list(raw_entrypoints.values())
    elif isinstance(raw_entrypoints, list):
        candidates = raw_entrypoints
    else:
        candidates = [raw_entrypoints]
    for entrypoint in candidates:
        scenario_id = _entrypoint_scenario_id(entrypoint)
        if scenario_id:
            return scenario_id
    return None


def _project_application(model: dict[str, Any]) -> dict[str, Any]:
    application = model.get("application")
    application = application if isinstance(application, dict) else model
    display = application.get("display")
    display = display if isinstance(display, dict) else {}
    effective = model.get("effective_release")
    effective = effective if isinstance(effective, dict) else {}
    effective_release = effective.get("release")
    effective_release = effective_release if isinstance(effective_release, dict) else {}
    installed_release = model.get("installed_release")
    installed_release = installed_release if isinstance(installed_release, dict) else {}
    marketplace_release = model.get("marketplace_release")
    marketplace_release = (
        marketplace_release if isinstance(marketplace_release, dict) else {}
    )
    categories = _categories(application)
    local_beta_active = _bool_value(model.get("local_beta_active"))
    installed = (
        _bool_value(model.get("installed", application.get("installed")))
        or local_beta_active
    )
    scenario_id = _scenario_id(application)
    effective_channel = _first_string(
        effective.get("update_track"),
        model.get("effective_channel"),
        model.get("channel"),
    )
    if local_beta_active and not effective_channel:
        effective_channel = "Beta"
    has_update = _bool_value(model.get("update_available", model.get("has_update")))
    update_state = _first_string(
        model.get("update_state"),
        (model.get("operation") or {}).get("status")
        if isinstance(model.get("operation"), dict)
        else None,
        "update_available" if has_update else "current",
    )
    application_id = _first_string(
        application.get("id"),
        application.get("application_id"),
        application.get("ref"),
        application.get("slug"),
    )
    return {
        "id": application_id,
        "title": _first_string(
            display.get("title"),
            application.get("title"),
            application.get("name"),
            application_id,
        ),
        "summary": _first_string(
            display.get("summary"),
            application.get("summary"),
            application.get("description"),
        ),
        "icon": _first_string(
            model.get("icon"), application.get("icon"), "apps-outline"
        ),
        "publisher": _publisher(application),
        "categories": categories,
        "category": _first_string(
            application.get("category"), categories[0] if categories else ""
        ),
        "installed": installed,
        "installed_state": "installed" if installed else "not_installed",
        "local_beta_active": local_beta_active,
        "effective_version": _first_string(
            effective.get("version"),
            effective_release.get("version"),
            installed_release.get("version"),
            marketplace_release.get("version"),
            model.get("effective_version"),
            application.get("version"),
        ),
        "effective_channel": effective_channel,
        "channel": effective_channel,
        "update_state": update_state,
        "has_update": has_update,
        "scenario_id": scenario_id,
        "launchable": bool(installed and scenario_id),
    }


def _application_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("items", payload.get("applications", []))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _installed_applications(payload: dict[str, Any]) -> list[str]:
    values = payload.get("applications")
    if values is None:
        values = payload.get("apps")
    return [str(item) for item in list(values or []) if str(item or "").strip()]


def _installed_widgets(payload: dict[str, Any]) -> list[str]:
    values = payload.get("widgets")
    if values is None:
        values = payload.get("pinnedWidgets")
    return [str(item) for item in list(values or []) if str(item or "").strip()]


def _pinned_application_ids(webspace_id: str | None = None) -> list[str]:
    return [
        _application_id(item)
        for item in desktop_get_pinned_applications(webspace_id=webspace_id)
    ]


def _widget_id(value: Any) -> str:
    if isinstance(value, dict):
        return _first_string(
            value.get("id"),
            value.get("widget_id"),
            value.get("panel_id"),
            value.get("ref"),
        )
    return _string(value)


def _widget_record(value: Any, *, index: int) -> dict[str, Any]:
    if isinstance(value, dict):
        record = deepcopy(value)
    else:
        record = {"id": _string(value)}
    widget_id = _widget_id(record)
    record["id"] = widget_id
    record["title"] = _first_string(record.get("title"), record.get("label"), widget_id)
    record["type"] = _first_string(record.get("type"), record.get("kind"), "widget")
    record["home_order"] = index
    record["pinned"] = True
    return record


def _move_value(values: list[Any], item_id: str, to_index: Any) -> list[Any]:
    if not item_id:
        return values
    current_index = next(
        (index for index, item in enumerate(values) if _application_id(item) == item_id),
        -1,
    )
    if current_index < 0:
        return values
    item = values.pop(current_index)
    try:
        target = int(to_index)
    except (TypeError, ValueError):
        target = len(values)
    target = max(0, min(target, len(values)))
    values.insert(target, item)
    return values


def _move_widget(values: list[Any], item_id: str, to_index: Any) -> list[Any]:
    if not item_id:
        return values
    current_index = next(
        (index for index, item in enumerate(values) if _widget_id(item) == item_id),
        -1,
    )
    if current_index < 0:
        return values
    item = values.pop(current_index)
    try:
        target = int(to_index)
    except (TypeError, ValueError):
        target = len(values)
    target = max(0, min(target, len(values)))
    values.insert(target, item)
    return values


def _read_application_models(
    *,
    catalog_only: bool,
    available_only: bool,
    developed_only: bool,
) -> list[dict[str, Any]]:
    global _APPLICATION_CACHE

    with _APPLICATION_CACHE_LOCK:
        now = monotonic()
        cached = _APPLICATION_CACHE
        if (
            cached is not None
            and now - cached[0] <= _APPLICATION_CACHE_TTL_SECONDS
        ):
            records = deepcopy(list(cached[1]))
        else:
            records = sdk_applications.list_applications(
                installed_only=False,
                catalog_only=False,
                available_only=False,
                developed_only=False,
                include_development=False,
            )
            records = _application_records(records)
            _APPLICATION_CACHE = (monotonic(), tuple(records))
            records = deepcopy(records)

    def catalog_available(model: dict[str, Any]) -> bool:
        application = _mapping(model.get("application"))
        channels = _mapping(model.get("channels"))
        effective = _mapping(model.get("effective_release"))
        return bool(
            model.get("marketplace_release")
            or channels.get("stable")
            or (
                _string(application.get("visibility")).casefold() == "public"
                and _string(effective.get("update_track")).casefold() == "stable"
            )
        )

    if available_only:
        records = [
            model
            for model in records
            if _bool_value(model.get("installed"))
            or _bool_value(model.get("local_beta_active"))
            or catalog_available(model)
        ]
    if catalog_only:
        records = [model for model in records if catalog_available(model)]
    if developed_only:
        records = [
            model
            for model in records
            if _bool_value(_mapping(model.get("local_development")).get("exists"))
        ]
    return records


def _invalidate_application_cache() -> None:
    global _APPLICATION_CACHE

    with _APPLICATION_CACHE_LOCK:
        _APPLICATION_CACHE = None


@tool(
    "list_applications",
    summary="List desktop applications from the authoritative Applications SDK.",
    stability="experimental",
    examples=["list_applications({'installed_only': True, 'launchable_only': True})"],
)
def list_applications(
    installed_only: bool = False,
    pinned_only: bool = False,
    catalog_only: bool = False,
    available_only: bool = False,
    developed_only: bool = False,
    launchable_only: bool = False,
    application_id: str | None = None,
    require_selection: bool = False,
    query: str | None = None,
    webspace_id: str | None = None,
    limit: int = 40,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    installed_filter = _bool_value(installed_only)
    pinned_filter = _bool_value(pinned_only)
    catalog_filter = _bool_value(catalog_only)
    available_filter = _bool_value(available_only)
    developed_filter = _bool_value(developed_only)
    launchable_filter = _bool_value(launchable_only)
    selected_id = _string(application_id)
    if _bool_value(require_selection) and not selected_id:
        return _empty_collection()
    try:
        records = _read_application_models(
            catalog_only=catalog_filter,
            available_only=available_filter,
            developed_only=developed_filter,
        )
        pinned_ids = _pinned_application_ids(webspace_id) if pinned_filter else []
    except Exception as exc:  # pragma: no cover - exercised through public SDK mock
        return {
            "ok": False,
            "error": "applications_list_failed",
            "message": str(exc)
            or "Applications SDK failed while listing applications.",
            "items": [],
            "count": 0,
            "total": 0,
            "empty": True,
            "truncated": False,
        }

    pinned_order = {item_id: index for index, item_id in enumerate(pinned_ids)}
    items = [_project_application(item) for item in records]
    for item in items:
        item["pinned"] = item["id"] in pinned_order
        item["home_order"] = pinned_order.get(item["id"], 0)
    if selected_id:
        items = [item for item in items if item["id"] == selected_id]
    if pinned_filter:
        items = [item for item in items if item["pinned"]]
    if installed_filter:
        items = [item for item in items if item["installed"]]
    if catalog_filter:
        items = [item for item in items if not item["installed"]]
    if launchable_filter:
        items = [item for item in items if item["launchable"]]
    query_token = _string(query).casefold()
    if query_token:
        items = [
            item
            for item in items
            if query_token
            in " ".join(
                [
                    item["title"],
                    item["summary"],
                    item["publisher"],
                    *item["categories"],
                ]
            ).casefold()
        ]
    if pinned_filter:
        items.sort(key=lambda item: (item["home_order"], item["title"].casefold()))
    else:
        items.sort(key=lambda item: (item["title"].casefold(), item["id"]))
    total = len(items)
    bounded_limit = _bounded_limit(limit)
    items = items[:bounded_limit]

    result: dict[str, Any] = {
        "ok": True,
        "items": items,
        "count": len(items),
        "total": total,
        "empty": not items,
        "truncated": total > len(items),
    }
    if selected_id:
        result["item"] = items[0] if items else {}
        if items:
            result.update(items[0])
    return result


@tool(
    "list_home_widgets",
    summary="List desktop widgets pinned to the Home surface.",
    stability="experimental",
    examples=["list_home_widgets({'webspace_id': 'desktop'})"],
)
def list_home_widgets(
    webspace_id: str | None = None,
    limit: int = 40,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    try:
        records = application_get_pinned_panels(webspace_id=webspace_id)
    except Exception as exc:  # pragma: no cover - exercised through public SDK mock
        return {
            "ok": False,
            "error": "home_widgets_list_failed",
            "message": str(exc) or "Web Desktop SDK failed while listing widgets.",
            "items": [],
            "count": 0,
            "total": 0,
            "empty": True,
            "truncated": False,
        }
    items = [_widget_record(item, index=index) for index, item in enumerate(records)]
    total = len(items)
    bounded_limit = _bounded_limit(limit)
    items = items[:bounded_limit]
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "total": total,
        "empty": not items,
        "truncated": total > len(items),
    }


@tool(
    "list_devices",
    summary="List real node devices and browser sessions from the control-plane SDK.",
    stability="experimental",
    examples=["list_devices({'webspace_id': 'desktop'})"],
)
def list_devices(
    webspace_id: str | None = None,
    webspace_only: bool = False,
    controller_endpoint_id: str | None = None,
    device_id: str | None = None,
    require_selection: bool = False,
    query: str | None = None,
    status: str | None = "active",
    limit: int = 40,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    selected_id = _string(device_id)
    if _bool_value(require_selection) and not selected_id:
        return _empty_collection()
    try:
        canonical_devices = [
            _canonical_item(item, source="device")
            for item in sdk_control_plane.list_device_objects()
        ]
        canonical_browsers = [
            _canonical_item(item, source="browser_session")
            for item in sdk_control_plane.list_browser_session_objects()
        ]
        browser_items = {
            _browser_identity(item.get("id")): item
            for item in [*canonical_devices, *canonical_browsers]
            if item.get("device_type") == "browser" and _browser_identity(item.get("id"))
        }
        try:
            for link in sdk_access_links.list_browser_links():
                rich = _browser_link_item(link)
                identity = _browser_identity(rich.get("id"))
                if identity:
                    browser_items[identity] = rich
        except Exception:
            pass
        devices = [
            item for item in canonical_devices if item.get("device_type") != "browser"
        ] + list(browser_items.values())
    except Exception as exc:  # pragma: no cover - exercised through public SDK mock
        return {
            "ok": False,
            "error": "devices_list_failed",
            "message": str(exc) or "Control-plane SDK failed while listing devices.",
            "items": [],
            "count": 0,
            "total": 0,
            "empty": True,
            "truncated": False,
        }
    selected_webspace = _string(webspace_id)
    controller_device_token = _controller_device_token(controller_endpoint_id)
    items = []
    for item in devices:
        is_browser = item["device_type"] == "browser"
        belongs_to_webspace = (
            not selected_webspace or selected_webspace in item["workspace_ids"]
        )
        if is_browser and _bool_value(webspace_only) and not belongs_to_webspace:
            continue
        item["current"] = bool(
            item["current"]
            or (
                controller_device_token
                and controller_device_token in _string(item["id"])
            )
        )
        if item["current"] and item["title"] in item["id"]:
            item["title"] = (
                "Current browser tab"
                if "::page_" in item["id"]
                else "Current browser"
            )
        items.append(item)
    if selected_id:
        items = [item for item in items if item["id"] == selected_id]
    query_token = _string(query).casefold()
    if query_token:
        items = [
            item
            for item in items
            if query_token
            in " ".join(
                [
                    item["title"],
                    item["summary"],
                    item["kind"],
                    item["status"],
                    item["source"],
                ]
            ).casefold()
        ]
    status_token = _string(status).casefold()
    if status_token == "active":
        items = [item for item in items if _device_is_active(item)]
    elif status_token == "offline":
        items = [item for item in items if not _device_is_active(item)]
    items.sort(
        key=lambda item: (
            not item["current"],
            item["status"] != "online",
            -_timestamp_value(item["last_seen"]),
            item["title"].casefold(),
            item["id"],
        )
    )
    total = len(items)
    items = items[: _bounded_limit(limit)]
    result: dict[str, Any] = {
        "ok": True,
        "webspace_id": _string(webspace_id),
        "items": items,
        "count": len(items),
        "total": total,
        "empty": not items,
        "truncated": total > len(items),
    }
    if selected_id:
        result["item"] = items[0] if items else {}
        if items:
            result.update(items[0])
    return result


@tool(
    "get_system_overview",
    summary="Read the real local system and reliability projections.",
    stability="experimental",
    examples=["get_system_overview({'webspace_id': 'desktop'})"],
)
def get_system_overview(
    webspace_id: str | None = None,
    section: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    selected_section = _string(section).casefold()
    sdk_sections: set[str] = {"summary"}
    if selected_section in {"", "all"}:
        sdk_sections = {"all"}
    elif selected_section in {"services", "connections", "quotas", "incidents", "update"}:
        sdk_sections.add(selected_section)
    try:
        snapshot = sdk_system.get_operational_snapshot(
            sections=sdk_sections,
            webspace_id=webspace_id,
            limit=40,
        )
    except Exception as exc:  # pragma: no cover - exercised through public SDK mock
        return {
            "ok": False,
            "error": "system_overview_failed",
            "message": str(exc)
            or "Control-plane SDK failed while reading system status.",
            "subject": {},
            "services": [],
            "connections": [],
            "quotas": [],
            "incidents": [],
            "update": {},
        }
    subject = _canonical_item(snapshot.get("subject"), source="node")
    capacity = _mapping(snapshot.get("capacity"))
    services = [
        _canonical_item(item, source="control_plane")
        for item in list(snapshot.get("services") or [])
        if isinstance(item, dict)
    ]
    connections = [
        _canonical_item(item, source="control_plane")
        for item in list(snapshot.get("connections") or [])
        if isinstance(item, dict)
    ]
    quotas = [
        _canonical_item(item, source="control_plane")
        for item in list(snapshot.get("quotas") or [])
        if isinstance(item, dict)
    ]
    incidents = [item for item in list(snapshot.get("incidents") or []) if isinstance(item, dict)]
    update = _mapping(snapshot.get("update"))
    for collection in (services, connections, quotas):
        collection.sort(
            key=lambda item: (item["kind"], item["title"].casefold(), item["id"])
        )
    capacity_resources = _mapping(capacity.get("resources"))
    active_skill_total = int(capacity_resources.get("active_skill_total") or 0)
    active_scenario_total = int(capacity_resources.get("active_scenario_total") or 0)
    services_loaded = selected_section in {"", "all", "services"}
    if services_loaded:
        service_value: int | str = len(services)
        service_description = (
            f"{sum(item['status'] == 'online' for item in services)} online"
        )
    else:
        service_value = active_skill_total + active_scenario_total
        service_description = (
            f"{active_skill_total} skills, {active_scenario_total} scenarios"
        )
    incidents_loaded = selected_section in {"", "all", "incidents"}
    incident_value: int | str = len(incidents) if incidents_loaded else "--"
    incident_description = (
        "No observed incidents"
        if incidents_loaded and not incidents
        else "Review system attention"
        if incidents_loaded
        else "Open System to inspect reliability incidents"
    )
    metrics = {
        "node": {
            "value": subject["status"],
            "label": subject["title"],
            "description": subject["summary"],
            "color": "success" if subject["status"] == "online" else "warning",
        },
        "services": {
            "value": service_value,
            "label": "Active runtime components"
            if not services_loaded
            else "Observed services",
            "description": service_description,
            "color": "success" if not services_loaded or not incidents else "warning",
        },
        "capacity": {
            "value": active_skill_total,
            "label": "Active skills",
            "description": f"{active_scenario_total} active scenarios",
            "color": "primary",
        },
        "incidents": {
            "value": incident_value,
            "label": "Observed incidents",
            "description": incident_description,
            "color": "success"
            if incidents_loaded and not incidents
            else "warning"
            if incidents
            else "primary",
        },
        "connections": {
            "value": len(connections) if selected_section in {"", "all", "connections"} else "--",
            "label": "Observed connections",
            "description": "Control-plane routes and transports",
            "color": "success"
            if connections and all(item["status"] == "online" for item in connections)
            else "warning"
            if connections
            else "primary",
        },
        "update": {
            "value": _string(update.get("state")) or "--",
            "label": "Core update",
            "description": _string(update.get("message")) or "No active transition",
            "color": "warning"
            if _string(update.get("state")).casefold() not in {"", "idle", "ready", "succeeded"}
            else "success",
        },
    }
    selected_items: list[dict[str, Any]] = []
    if selected_section == "services":
        selected_items = services[:40]
    elif selected_section == "connections":
        selected_items = connections[:40]
    elif selected_section == "quotas":
        selected_items = quotas[:40]
    elif selected_section == "incidents":
        selected_items = incidents[:40]
    result = {
        "ok": True,
        "webspace_id": _string(webspace_id),
        "subject": subject,
        "services": services[:40]
        if selected_section in {"", "all", "services"}
        else [],
        "connections": connections[:40]
        if selected_section in {"", "all", "connections"}
        else [],
        "quotas": quotas[:40]
        if selected_section in {"", "all", "quotas"}
        else [],
        "incidents": incidents[:40]
        if selected_section in {"", "all", "incidents"}
        else [],
        "update": update if selected_section in {"", "all", "update"} else {},
        "metrics": metrics,
        "items": selected_items,
        "count": len(selected_items),
        "total": len(services)
        if selected_section == "services"
        else len(connections)
        if selected_section == "connections"
        else len(quotas)
        if selected_section == "quotas"
        else len(incidents)
        if selected_section == "incidents"
        else 0,
        "empty": not selected_items if selected_section else False,
        "truncated": (
            len(services) > len(selected_items)
            if selected_section == "services"
            else len(connections) > len(selected_items)
            if selected_section == "connections"
            else len(quotas) > len(selected_items)
            if selected_section == "quotas"
            else len(incidents) > len(selected_items)
            if selected_section == "incidents"
            else False
        ),
    }
    if selected_section == "summary":
        result.update(metrics.get("node", {}))
        result["services"] = []
        result["connections"] = []
        result["quotas"] = []
        result["incidents"] = []
        result["update"] = {}
    return result


@tool(
    "list_developments",
    summary="List real development projects from the public developer SDK.",
    stability="experimental",
    examples=["list_developments({'query': 'desktop'})"],
)
def list_developments(
    development_id: str | None = None,
    require_selection: bool = False,
    query: str | None = None,
    profile: str | None = None,
    limit: int = 40,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    selected_id = _string(development_id)
    if _bool_value(require_selection) and not selected_id:
        return _empty_collection()
    bounded_limit = _bounded_limit(limit)
    try:
        records = sdk_applications.list_development_projects(
            profile=_string(profile) or None,
            query=_string(query) or None,
            limit=500,
        )
    except Exception as exc:  # pragma: no cover - exercised through public SDK mock
        return {
            "ok": False,
            "error": "developments_list_failed",
            "message": str(exc) or "Developer SDK failed while listing projects.",
            "items": [],
            "count": 0,
            "total": 0,
            "empty": True,
            "truncated": False,
        }
    items: list[dict[str, Any]] = []
    for value in records:
        record = _mapping(value)
        project_id = _first_string(
            record.get("id"), record.get("project_id"), record.get("name")
        )
        primary_ref = _first_string(record.get("primary_ref"))
        scenario_id = (
            primary_ref.split(":", 1)[1]
            if primary_ref.startswith("scenario:")
            else None
        )
        items.append(
            {
                "id": project_id,
                "title": _first_string(
                    record.get("title"), record.get("name"), project_id
                ),
                "summary": _first_string(
                    record.get("description"), record.get("summary")
                ),
                "status": _first_string(
                    record.get("status"), record.get("phase"), "development"
                ),
                "version": _first_string(
                    record.get("version"), record.get("current_version")
                ),
                "updated_at": _first_string(
                    record.get("updated_at"), record.get("modified_at")
                ),
                "stage": _first_string(
                    record.get("stage"),
                    _mapping(record.get("publication")).get("stage"),
                ),
                "visibility": _first_string(
                    record.get("visibility"),
                    _mapping(record.get("publication")).get("visibility"),
                ),
                "primary_ref": primary_ref,
                "scenario_id": scenario_id,
                "launchable": bool(scenario_id),
                "source_kind": _first_string(record.get("source_kind")),
                "validation_status": _first_string(record.get("validation_status")),
            }
        )
    items.sort(
        key=lambda item: (
            -_timestamp_value(item["updated_at"]),
            item["title"].casefold(),
            item["id"],
        )
    )
    if selected_id:
        items = [item for item in items if item["id"] == selected_id]
    total = len(items)
    truncated = total > bounded_limit
    items = items[:bounded_limit]
    result: dict[str, Any] = {
        "ok": True,
        "items": items,
        "count": len(items),
        "total": total,
        "empty": not items,
        "truncated": truncated,
    }
    if selected_id:
        result["item"] = items[0] if items else {}
        if items:
            result.update(items[0])
    return result


@tool(
    "get_preferences",
    summary="Read the current user and device preferences for Web Desktop settings.",
    stability="experimental",
    examples=["get_preferences({})"],
)
def get_preferences(
    webspace_id: str | None = None,
    controller_endpoint_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    try:
        item = _preference_record(
            webspace_id=webspace_id,
            controller_endpoint_id=controller_endpoint_id,
        )
    except Exception as exc:  # pragma: no cover - exercised through public SDK mock
        return {
            "ok": False,
            "error": "preferences_read_failed",
            "message": str(exc) or "Profile SDK failed while reading preferences.",
            "items": [],
            "item": {},
        }
    return {"ok": True, "items": [item], "item": item, **item}


@tool(
    "update_preferences",
    summary="Persist allowlisted Web Desktop profile and preference fields.",
    stability="experimental",
    examples=[
        "update_preferences({'values': {'theme': 'dark'}, 'device_override': True})"
    ],
)
def update_preferences(
    values: dict[str, Any],
    device_override: bool = False,
    webspace_id: str | None = None,
    controller_endpoint_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    submitted = _mapping(values)
    profile_patch = {
        target: submitted[field]
        for field, target in _PROFILE_FIELDS.items()
        if field in submitted
    }
    preference_patch = {
        target: submitted[field]
        for field, target in _PREFERENCE_FIELDS.items()
        if field in submitted
    }
    if "startDestination" in submitted:
        start_destination = _string(submitted.get("startDestination"))
        if start_destination not in _START_DESTINATIONS:
            return {
                "ok": False,
                "error": "invalid_start_destination",
                "message": "Start destination is not a current Desktop section.",
                "items": [],
                "item": {},
            }
    try:
        if profile_patch:
            sdk_profile.update_settings(profile_patch)
        if preference_patch:
            sdk_profile.update_preferences(
                preference_patch,
                device_override=_bool_value(device_override),
            )
        item = _preference_record(
            webspace_id=webspace_id,
            controller_endpoint_id=controller_endpoint_id,
        )
    except Exception as exc:  # pragma: no cover - exercised through public SDK mock
        return {
            "ok": False,
            "error": "preferences_update_failed",
            "message": str(exc) or "Profile SDK failed while updating preferences.",
            "items": [],
            "item": {},
        }
    return {
        "ok": True,
        "updated_fields": sorted([*profile_patch, *preference_patch]),
        "items": [item],
        "item": item,
        **item,
    }


@tool(
    "prepare_connection",
    summary="Prepare an authorized browser, Telegram, or node connection flow.",
    stability="experimental",
)
async def prepare_connection(
    mode: str = "browser",
    webspace_id: str | None = None,
    refresh: bool = True,
    force_new: bool = False,
    renew: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        from .connect import prepare_connection as implementation
    except ImportError:
        import importlib.util
        from pathlib import Path

        connect_path = Path(__file__).with_name("connect.py")
        spec = importlib.util.spec_from_file_location(f"{__name__}_connect", connect_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("connection handler could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        implementation = module.prepare_connection
    return await implementation(
        mode=mode,
        webspace_id=webspace_id,
        refresh=refresh,
        force_new=force_new,
        renew=renew,
        **kwargs,
    )


@tool(
    "update_home_item",
    summary="Pin, unpin or reorder a Home widget.",
    stability="experimental",
    examples=[
        "update_home_item({'item_type': 'application', 'item_id': 'notes', 'action': 'unpin'})"
    ],
)
def update_home_item(
    item_type: str,
    item_id: str,
    action: str,
    to_index: int | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    normalized_type = _string(item_type).casefold()
    normalized_action = _string(action).casefold()
    if normalized_type in {"application", "app"}:
        return {
            "ok": False,
            "error": "authoritative_root_mcp_required",
            "message": (
                "Use applications.set_home_pin or applications.reorder_home so "
                "the Root-owned Home projection cannot be replaced by worker-local state."
            ),
        }
    if normalized_type in {"widget", "panel"}:
        panels = list(application_get_pinned_panels(webspace_id=webspace_id) or [])
        widget_id = _string(item_id)
        if normalized_action in {"pin", "add"}:
            if widget_id and not any(_widget_id(item) == widget_id for item in panels):
                panels.append({"id": widget_id, "type": "widget"})
        elif normalized_action in {"unpin", "remove"}:
            panels = [item for item in panels if _widget_id(item) != widget_id]
        elif normalized_action in {"move", "reorder"}:
            panels = _move_widget(panels, widget_id, to_index)
        else:
            return {
                "ok": False,
                "error": "unsupported_home_action",
                "message": "Choose pin, unpin or move.",
            }
        application_set_pinned_panels(panels, webspace_id=webspace_id, live=True)
        return {
            "ok": True,
            "item_type": "widget",
            "item_id": widget_id,
            "action": normalized_action,
            "widgets": panels,
        }
    return {
        "ok": False,
        "error": "unsupported_home_item_type",
        "message": "Home customization supports application launchers and widgets.",
    }
