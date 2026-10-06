from __future__ import annotations

from copy import deepcopy
import asyncio
import logging
import re
from datetime import datetime, timezone
from threading import Lock, RLock
from time import monotonic
from typing import Any

from adaos.sdk import access as sdk_access
from adaos.sdk import applications as sdk_applications
from adaos.sdk import control_plane as sdk_control_plane
from adaos.sdk import system as sdk_system
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import access_links as sdk_access_links
from adaos.sdk.data import device_access as sdk_device_access
from adaos.sdk.data import profile as sdk_profile
from adaos.sdk.data.blob import put_upload
from adaos.sdk.web import (
    application_get_pinned_panels,
    application_set_pinned_panels,
    desktop_get_pinned_applications,
)

from .operational import project as _project_operational_dashboard
from .operational import number as _operational_number


_MAX_DEVICE_INVENTORY_PAGE_SIZE = 500


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(event: Any) -> None:
    from .system_stream import snapshot_requested
    from .system_bootstrap_stream import snapshot_requested as bootstrap_snapshot_requested

    snapshot_requested(event)
    bootstrap_snapshot_requested(event)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(event: Any) -> None:
    from .system_stream import subscription_changed
    from .system_bootstrap_stream import subscription_changed as bootstrap_subscription_changed

    subscription_changed(event)
    bootstrap_subscription_changed(event)


@subscribe("root.subscription.changed")
@subscribe("economic.subscription.changed")
@subscribe("subscription.usage.changed")
def on_root_subscription_changed(event: Any) -> None:
    """Refresh the active System read-model after Root usage changes."""
    from .system_bootstrap_stream import root_subscription_changed

    root_subscription_changed(event)


_SYSTEM_LIFECYCLE_LOCK = RLock()


def _drain_system_publishers(*, reason: str) -> dict[str, Any]:
    with _SYSTEM_LIFECYCLE_LOCK:
        return _drain_system_publishers_locked(reason=reason)


def _drain_system_publishers_locked(*, reason: str) -> dict[str, Any]:
    """Stop every thread owned by this runtime before replacement."""
    from .system_bootstrap_stream import drain_refresh, stop_refresh
    from .system_stream import drain_workers, stop_workers

    stop_workers()
    stop_refresh()
    hardware = drain_workers()
    subscription = drain_refresh()
    return {
        "ok": not hardware.get("alive") and not subscription.get("worker_alive"),
        "reason": str(reason or "runtime_draining"),
        "hardware": hardware,
        "subscription": subscription,
    }


@tool("web_desktop_runtime_drain")
def web_desktop_runtime_drain(reason: str = "drain", **_: Any) -> dict[str, Any]:
    return _drain_system_publishers(reason=reason)


@tool("web_desktop_runtime_dispose")
def web_desktop_runtime_dispose(reason: str = "dispose", **_: Any) -> dict[str, Any]:
    return _drain_system_publishers(reason=reason)


@tool("web_desktop_runtime_rehydrate")
def web_desktop_runtime_rehydrate(reason: str = "rehydrate", **_: Any) -> dict[str, Any]:
    """Re-enable demand publishers after a verified runtime cutover."""

    from .system_bootstrap_stream import rehydrate_refresh
    from .system_stream import rehydrate_workers

    with _SYSTEM_LIFECYCLE_LOCK:
        drained = _drain_system_publishers(reason=reason)
        if not drained["ok"]:
            return {**drained, "reason": "workers_still_draining"}
        hardware = rehydrate_workers()
        subscription = rehydrate_refresh()
    return {
        "ok": bool(hardware.get("ok")) and bool(subscription.get("ok")),
        "reason": str(reason or "runtime_rehydrated"),
        "hardware": hardware,
        "subscription": subscription,
    }


@tool("rename_subnet", summary="Persist the current subnet display name.", stability="experimental")
def rename_subnet(display_name: str) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 120:
        raise ValueError("display_name_invalid")
    return sdk_system.rename_local_subnet(display_name.strip())


@tool("get_current_builder_application", summary="Read the current Builder selection.", stability="experimental")
def get_current_builder_application() -> dict[str, Any]:
    from .builder import get_current_builder_application as read_current
    return read_current()


@tool("get_subscription_usage", summary="Read rolling subscription token usage.", stability="experimental")
def get_subscription_usage(webspace_id: str | None = None, refresh: bool = False) -> dict[str, Any]:
    from .usage import get_subscription_usage as read_usage

    return read_usage(webspace_id=webspace_id, refresh=refresh)

@tool("request_core_update", summary="Request the governed Core release.", stability="experimental")
def request_core_update(target_node_id: str) -> dict[str, Any]:
    # Core ingress consumes target_node_id and routes before this local call.
    from .updates import request_core_update as request_update
    return request_update()


@tool("rename_selected_node", summary="Rename the ingress-selected member node.", stability="experimental")
def rename_selected_node(target_node_id: str, display_name: str) -> dict[str, Any]:
    # Core ingress consumes target_node_id and routes before this local call.
    if not str(target_node_id or "").strip():
        raise ValueError("target_node_id_required")
    from .nodes import rename_selected_node as rename_node
    return rename_node(display_name)


@tool("set_core_autoupdate", summary="Set governed Core autoupdate.", stability="experimental")
def set_core_autoupdate(target_node_id: str, enabled: bool) -> dict[str, Any]:
    from .updates import set_core_autoupdate as set_autoupdate
    return set_autoupdate(enabled)


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
    "showProjects": "show_projects",
    "autoUpdate": "auto_update",
    "followPrerelease": "follow_prerelease",
    "updateWindow": "update_window",
    "meteredDownloads": "metered_downloads",
    "shareTelemetry": "share_telemetry",
    "participateDevelopment": "participate_application_development",
    "sendDiagnostics": "send_diagnostics_to_developer",
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
    "showProjects": False,
    "autoUpdate": True,
    "followPrerelease": False,
    "updateWindow": "any",
    "meteredDownloads": False,
    "shareTelemetry": False,
    "participateDevelopment": False,
    "sendDiagnostics": False,
    "activityRetention": "90",
}

_START_DESTINATIONS = {
    "home",
    "chat",
}

_APPLICATION_CACHE_TTL_SECONDS = 15.0
_APPLICATION_CACHE_LOCK = Lock()
_APPLICATION_CACHE: tuple[float, tuple[dict[str, Any], ...]] | None = None
_PROFILE_FIELDS: dict[str, str] = {
    "profileName": "display_name",
    "email": "email",
    "timeZone": "timezone",
    "language": "language",
    "avatarRef": "avatar_ref",
}
_PROFILE_AVATAR_FIELD_ID = "avatar_ref"
_PROFILE_AVATAR_MAX_BYTES = 2 * 1024 * 1024
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_FILENAME_RE = re.compile(r"^[^/\\:\x00-\x1f\x7f]{1,200}$")


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


def _device_slug(value: Any) -> str:
    return "-".join(
        "".join(
            character if character.isalnum() else " "
            for character in _string(value).casefold()
        ).split()
    )


def _browser_zone(item: dict[str, Any]) -> str:
    explicit = _string(item.get("browser_zone")).upper()
    if explicit:
        return explicit
    origin = _string(item.get("browser_origin")).casefold()
    if any(token in origin for token in ("127.0.0.1", "localhost", "[::1]")):
        return "LO"
    return "RU"


def _browser_representation_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    identity = _browser_identity(item.get("id"))
    parent = identity.split("::", 1)[0]
    webspace = _first_string(*(item.get("workspace_ids") or []), "desktop")
    return (
        parent,
        _browser_zone(item),
        webspace.casefold(),
        _string(item.get("browser_origin")).casefold(),
    )


def _collapse_browser_representations(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose one addressable endpoint per zone/webspace representation."""

    pages = [item for item in items if "::" in _browser_identity(item.get("id"))]
    if not pages:
        return items
    parents_with_pages = {
        _browser_identity(item.get("id")).split("::", 1)[0] for item in pages
    }
    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in pages:
        key = _browser_representation_key(item)
        previous = selected.get(key)
        if previous is None or (
            bool(item.get("current")),
            _timestamp_value(item.get("last_seen")),
        ) > (
            bool(previous.get("current")),
            _timestamp_value(previous.get("last_seen")),
        ):
            selected[key] = item
    standalone = [
        item
        for item in items
        if "::" not in _browser_identity(item.get("id"))
        and _browser_identity(item.get("id")) not in parents_with_pages
    ]
    return [*standalone, *selected.values()]


def _device_tree_items(
    items: list[dict[str, Any]],
    *,
    subnet: dict[str, Any] | None = None,
    subject: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    node_endpoints = [
        dict(item) for item in items if item.get("device_type") != "browser"
    ]
    browser_endpoints = _collapse_browser_representations(
        [dict(item) for item in items if item.get("device_type") == "browser"]
    )
    subnet_info = _mapping(subnet)
    subnet_id = _string(subnet_info.get("subnet_id"))
    subnet_name = _string(subnet_info.get("display_name"))
    subject_info = _mapping(subject)
    subject_id = _string(subject_info.get("id"))
    subject_node_token = subject_id.split(":", 1)[-1].casefold()

    physical_roots: dict[str, dict[str, Any]] = {}
    endpoint_parent: dict[str, str] = {}
    root_by_title: dict[str, str] = {}

    for endpoint in node_endpoints:
        endpoint_id = _string(endpoint.get("id"))
        physical_name = _first_string(
            endpoint.get("hostname"),
            *(endpoint.get("node_names") or []),
            endpoint.get("title"),
            endpoint_id,
            "Device",
        )
        root_id = f"physical-device:{_device_slug(physical_name) or _device_slug(endpoint_id) or 'local'}"
        physical_roots.setdefault(
            root_id,
            {
                "id": root_id,
                "title": physical_name,
                "summary": "Physical OS device",
                "kind": "physical_device",
                "status": endpoint.get("status") or "unknown",
                "source": "device_inventory",
                "last_seen": endpoint.get("last_seen") or "",
                "version": "",
                "location": endpoint.get("location") or "",
                "current": bool(endpoint.get("current")),
                "device_type": "physical_device",
                "connection": endpoint.get("connection") or "",
                "route_mode": endpoint.get("route_mode") or "",
                "workspace_ids": [],
                "owner": endpoint.get("owner") or "",
                "parent_device_id": "",
                "can_accept_endpoints": True,
                "can_move_endpoint": False,
                "can_rename": False,
                "icon": "desktop-outline",
            },
        )
        for key in (
            endpoint_id,
            _string(endpoint.get("device_ref")),
            _string(endpoint.get("title")),
            physical_name,
        ):
            if key:
                endpoint_parent[key.casefold()] = root_id
        root_by_title[physical_name.casefold()] = root_id
        endpoint["parent_device_id"] = root_id
        endpoint["can_accept_endpoints"] = False
        endpoint["can_move_endpoint"] = False
        endpoint["icon"] = "server-outline"
        endpoint_kind = (
            "Hub"
            if _string(endpoint.get("route_mode")).casefold() == "hub"
            or "hub" in endpoint_id.casefold()
            or bool(
                subject_node_token
                and subject_node_token
                == _first_string(endpoint.get("node_id"), endpoint_id)
                .split(":", 1)[-1]
                .casefold()
            )
            else "Node"
        )
        endpoint_ref = subnet_id if endpoint_kind == "Hub" and subnet_id else (
            _string(endpoint.get("node_id")) or endpoint_id.split(":")[-1]
        )
        endpoint_name = subnet_name if endpoint_kind == "Hub" and subnet_name else _string(endpoint.get("title"))
        endpoint["title"] = f"RU:{endpoint_kind}:{endpoint_ref}"
        if endpoint_name and endpoint_name.casefold() != endpoint_ref.casefold():
            endpoint["title"] += f" ({endpoint_name})"

    online_physical_roots = [
        root_id
        for root_id, root in physical_roots.items()
        if _string(root.get("status")).casefold() == "online"
    ]
    # Older browser-link records predate explicit OS/parent metadata.  When
    # exactly one OS device is online, it is the only safe automatic parent;
    # offline phones and retired nodes must not force an artificial
    # "Browser device" root.  New records keep using their explicit parent.
    fallback_root_id = (
        next(iter(physical_roots), "")
        if len(physical_roots) == 1
        else online_physical_roots[0]
        if len(online_physical_roots) == 1
        else ""
    )
    for endpoint in browser_endpoints:
        parent_ref = _string(endpoint.get("parent_device_ref"))
        device_name = _string(endpoint.get("device_display_name"))
        parent = (
            endpoint_parent.get(parent_ref.casefold(), "")
            or endpoint_parent.get(device_name.casefold(), "")
            or root_by_title.get(device_name.casefold(), "")
            or fallback_root_id
        )
        if not parent:
            physical_name = device_name or _first_string(
                endpoint.get("os_name"), "Browser device"
            )
            parent = f"physical-device:{_device_slug(physical_name) or 'browser'}"
            physical_roots.setdefault(
                parent,
                {
                    "id": parent,
                    "title": physical_name,
                    "summary": "Physical OS device",
                    "kind": "physical_device",
                    "status": endpoint.get("status") or "unknown",
                    "source": "browser_identity",
                    "last_seen": endpoint.get("last_seen") or "",
                    "version": "",
                    "location": "",
                    "current": bool(endpoint.get("current")),
                    "device_type": "physical_device",
                    "connection": endpoint.get("connection") or "",
                    "route_mode": "",
                    "workspace_ids": [],
                    "owner": "",
                    "parent_device_id": "",
                    "can_accept_endpoints": True,
                    "can_move_endpoint": False,
                    "can_rename": False,
                    "icon": "desktop-outline",
                },
            )
        webspace = _first_string(*(endpoint.get("workspace_ids") or []), "desktop")
        browser_name = _first_string(
            endpoint.get("endpoint_display_name"),
            endpoint.get("title"),
            "Browser",
        )
        endpoint["title"] = f"{_browser_zone(endpoint)}:Browser:{webspace} ({browser_name})"
        endpoint["parent_device_id"] = parent
        endpoint["can_move_endpoint"] = True
        endpoint["can_accept_endpoints"] = False
        endpoint["icon"] = "globe-outline"
    return [*physical_roots.values(), *node_endpoints, *browser_endpoints]


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
    is_page_endpoint = "::page_" in browser_id
    parent_policy: dict[str, Any] = {}
    if is_page_endpoint:
        parent_id = browser_id.split("::", 1)[0]
        try:
            parent_policy = _mapping(sdk_access_links.get_browser_link(parent_id))
        except Exception:
            parent_policy = {}
    endpoint_name = _first_string(
        parent_policy.get("endpoint_display_name") if is_page_endpoint else None,
        parent_policy.get("endpoint_title") if is_page_endpoint else None,
        parent_policy.get("display_name") if is_page_endpoint else None,
        item.get("endpoint_display_name"),
        item.get("endpoint_title"),
        item.get("display_name"),
        item.get("draft_name"),
    )
    device_name = _first_string(
        item.get("device_display_name"),
        parent_policy.get("device_display_name"),
    )
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
    browser_origin = _first_string(item.get("browser_origin"))
    browser_zone = _first_string(item.get("browser_zone"))
    if is_page_endpoint:
        page_id = browser_id.rsplit("::", 1)[-1]
        representation = " · ".join(
            value for value in (webspace_id, browser_zone, browser_origin) if value
        )
        title = f"{endpoint_name or 'Browser tab'}"
        if representation:
            title += f" · {representation}"
        title += f" · {page_id[-8:]}"
    details = [access_class]
    if webspace_id:
        details.append(f"webspace: {webspace_id}")
    if device_name and device_name.casefold() != title.casefold():
        details.append(device_name)
    if endpoint_name and endpoint_name.casefold() != title.casefold():
        details.append(endpoint_name)
    if browser_zone:
        details.append(f"zone: {browser_zone}")
    if browser_origin:
        details.append(f"origin: {browser_origin}")
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
        "browser_origin": browser_origin,
        "browser_zone": browser_zone,
        "os_name": _first_string(item.get("os_name")),
        "parent_device_ref": _first_string(item.get("parent_device_ref")),
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
        "hostname": _first_string(actual.get("hostname")),
        "node_names": [
            _string(name)
            for name in list(actual.get("node_names") or [])
            if _string(name)
        ],
        "node_id": _first_string(actual.get("device_ref"), item.get("node_id")),
        "device_ref": _first_string(actual.get("device_ref")),
        "device_display_name": _first_string(actual.get("device_display_name")),
        "endpoint_display_name": _first_string(actual.get("endpoint_display_name")),
        "os_name": _first_string(actual.get("os_name")),
        "parent_device_ref": _first_string(actual.get("parent_device_ref")),
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
        "email": _first_string(profile.get("email")),
        "avatarRef": _first_string(profile.get("avatar_ref")),
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
        controller_token = _controller_device_token(controller_endpoint_id)
        try:
            link = (
                sdk_access_links.get_browser_link(controller_token)
                if controller_token else {}
            )
            current = _browser_link_item(link) if link else {}
        except Exception:
            current = {}
        record["deviceLabel"] = _first_string(
            _mapping(current).get("endpoint_display_name"),
            _mapping(current).get("title"),
            "Current browser",
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
    application_kind = _first_string(application.get("kind"), "application")
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
        "application_kind": application_kind,
        "owner_application_id": _first_string(application.get("owner_application_id")),
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
                # Cache the complete authoritative set. Consumer preferences
                # are applied after the snapshot so explicit Home membership
                # remains stronger than discovery visibility.
                include_projects=True,
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
    include_projects: bool | None = None,
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
    if include_projects is None:
        try:
            show_projects = _bool_value(
                _preference_record(webspace_id=webspace_id).get("showProjects")
            )
        except Exception:
            show_projects = False
    else:
        show_projects = _bool_value(include_projects)
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
    if not show_projects and not pinned_filter:
        items = [item for item in items if item["application_kind"] != "project"]
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


@tool("get_runtime_controls", summary="Read operator controls and Rasa service state.", stability="experimental")
async def get_runtime_controls(**_: Any) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    snapshot = sdk_system.get_runtime_controls()
    controls = _mapping(snapshot.get("controls"))
    rasa = _mapping(snapshot.get("rasa"))
    return {"ok": snapshot.get("ok", True), "item": {
        **controls,
        **{"rasa_" + key: rasa.get(source) for key, source in {
            "configured": "configured", "installed": "installed",
            "running": "running", "health": "health",
            "env_mode": "environment", "version_profile": "version_profile",
            "availability": "availability",
        }.items()},
        "diet_profile": rasa.get("diet_profile"),
    }}


@tool("set_runtime_control", summary="Apply an owner-governed runtime control.", stability="experimental")
async def set_runtime_control(control: str, value: Any = None, **_: Any) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    invocation = sdk_access.invocation()
    request_id = invocation.get("request_id") if isinstance(invocation, dict) else None
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 160:
        raise ValueError("invocation_request_id_missing")
    if control not in {"rasa_install", "rasa_enabled", "log_level",
                       "core_auto_update", "application_auto_update_default"}:
        return {"ok": False, "error": "unsupported_runtime_control"}
    return sdk_system.set_runtime_control(request_id=request_id, control=control, value=value)


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
    status_token = _string(status).casefold()
    try:
        canonical_devices = [
            _canonical_item(item, source="device")
            for item in sdk_control_plane.list_device_objects(
                status=status_token if status_token in {"active", "offline"} else None,
                include_detached=status_token == "offline",
                limit=min(_MAX_DEVICE_INVENTORY_PAGE_SIZE, max(40, _bounded_limit(limit) * 4)),
            )
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
        try:
            topology = _mapping(
                sdk_system.get_operational_snapshot(
                    sections=["summary"],
                    webspace_id=webspace_id,
                    limit=1,
                )
            )
        except Exception:
            topology = {}
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
        item_identity = _browser_identity(item["id"]) if is_browser else ""
        controller_matches = bool(
            controller_device_token
            and (
                item_identity == controller_device_token
                or (
                    "::" in controller_device_token
                    and item_identity == controller_device_token
                )
            )
        )
        item["current"] = bool(item["current"] or controller_matches)
        if item["current"] and item["title"] in item["id"]:
            item["title"] = (
                f"{(item['workspace_ids'][0] if item['workspace_ids'] else 'Current')} · browser tab"
                if "::page_" in item["id"]
                else "Current browser"
            )
        elif is_browser and "::page_" in item["id"] and item["title"] in item["id"]:
            item["title"] = f"{(item['workspace_ids'][0] if item['workspace_ids'] else 'Browser')} · tab"
        # Find addresses the selected page endpoint.  It is not restricted to
        # the Management page's own webspace: another open representation is a
        # valid target and Core already knows its last observed webspace.
        item["can_locate"] = (
            is_browser
            and "::" in _browser_identity(item.get("id"))
            and _device_is_active(item)
        )
        item["can_open_system"] = not is_browser
        item["can_rename"] = True
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
    if not selected_id:
        items = _device_tree_items(
            items,
            subnet=_mapping(topology.get("subnet")),
            subject=_mapping(topology.get("subject")),
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
    "rename_device",
    summary="Rename one managed device or browser endpoint through the Management-owned local contract.",
    stability="experimental",
)
def rename_device(
    device_ref: str,
    name: str,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    token = _string(device_ref)
    display_name = _string(name)
    if not token or token.startswith("physical-device:"):
        return {"ok": False, "error": "managed_endpoint_required"}
    if not display_name or len(display_name) > 120:
        return {"ok": False, "error": "browser_name_invalid"}
    # Page ids identify representations. The user-facing browser name belongs
    # to their durable parent endpoint and is inherited by every representation.
    target_ref = token.split("::", 1)[0] if token.startswith("browser:") else token
    return sdk_device_access.rename_device(target_ref, display_name)


@tool(
    "assign_device_endpoint",
    summary="Assign a browser endpoint to a physical device in the device inventory.",
    stability="experimental",
)
def assign_device_endpoint(
    endpoint_ref: str,
    parent_device_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    return sdk_device_access.assign_browser_parent_device(
        _string(endpoint_ref),
        _string(parent_device_ref) or None,
    )


@tool(
    "identify_device",
    summary="Ask a selected browser endpoint to identify itself to the user.",
    stability="experimental",
)
def identify_device(
    device_ref: str,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Expose the Core device signal through the Project-owned runtime contract."""

    sdk_access.require("workspace.read")
    token = _string(device_ref)
    if not token:
        raise ValueError("device_ref_required")
    return sdk_device_access.identify_device(
        token,
        webspace_id=_string(webspace_id) or None,
    )


def _activity_content(projection: dict[str, Any]) -> str:
    items = [item for item in list(projection.get("items") or []) if isinstance(item, dict)]
    if projection.get("available") is not True:
        return "Recent system activity is unavailable."
    if not items:
        return "No recent user-impacting system activity."
    lines = []
    for item in items[:20]:
        occurred = _string(item.get("recorded_at")) or "Time unavailable"
        summary = _string(item.get("summary")) or "System activity"
        status = _string(item.get("status"))
        lines.append(f"{occurred} — {summary}" + (f" ({status})" if status else ""))
    return "\n".join(lines)


def _technical_content(projection: dict[str, Any]) -> str:
    if projection.get("available") is not True:
        return "Technical details are unavailable."
    identifiers = _mapping(projection.get("identifiers"))
    runtime = _mapping(projection.get("runtime"))
    capacity = _mapping(runtime.get("capacity"))
    connectivity = _mapping(projection.get("connectivity"))
    update = _mapping(projection.get("update"))
    capacity_bits = []
    for key, label in (
        ("active_skill_total", "active skills"),
        ("active_scenario_total", "active scenarios"),
        ("skill_total", "installed skills"),
        ("scenario_total", "installed scenarios"),
    ):
        if capacity.get(key) is not None:
            capacity_bits.append(f"{capacity[key]} {label}")
    ready, observed = connectivity.get("ready"), connectivity.get("observed")
    connection_summary = _string(connectivity.get("status")) or "unknown"
    if type(ready) is int and type(observed) is int:
        connection_summary += f" · {ready} of {observed} ready"
    update_summary = _string(update.get("state") or update.get("phase")) or "unavailable"
    if _string(update.get("message")):
        update_summary += f" · {_string(update.get('message'))}"
    return "\n".join(
        [
            f"Node: {_string(identifiers.get('node_id')) or 'unavailable'}",
            f"Subnet: {_string(identifiers.get('subnet_id')) or 'unavailable'}",
            f"Webspace: {_string(identifiers.get('webspace_id')) or 'unavailable'}",
            f"Runtime: {_string(runtime.get('status')) or 'unknown'}"
            + (f" · version {_string(runtime.get('version'))}" if _string(runtime.get("version")) else ""),
            "Capacity: " + (", ".join(capacity_bits) if capacity_bits else "unavailable"),
            f"Connectivity: {connection_summary}",
            f"Core update: {update_summary}",
        ]
    )


@tool(
    "get_system_overview",
    summary="Read the real local system and reliability projections.",
    stability="experimental",
    examples=["get_system_overview({'webspace_id': 'desktop'})"],
)
def get_system_overview(
    webspace_id: str | None = None,
    section: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    selected_section = _string(section).casefold()
    sdk_sections: set[str] = {"summary"}
    if selected_section in {"", "all"}:
        sdk_sections = {"summary", "services", "connections", "quotas", "incidents",
                        "update", "applications", "resources", "members"}
    elif selected_section in {"services", "connections", "quotas", "incidents", "update"}:
        sdk_sections.add(selected_section)
    elif selected_section == "metrics":
        sdk_sections.add("resources")
    elif selected_section in {"applications", "members", "skills", "development", "activity", "technical"}:
        sdk_sections.add(selected_section)
    elif selected_section == "subnet":
        sdk_sections.update({"members", "development"})
    elif selected_section in {"dashboard", "system_bootstrap"}:
        sdk_sections.update({"members", "applications", "development"})
    elif selected_section == "node_dashboard":
        sdk_sections.update({"applications", "skills", "update", "resources", "incidents"})
    elif selected_section in {"current_node", "update"}:
        sdk_sections.add("applications")
        if selected_section == "update":
            sdk_sections.add("update")
    try:
        snapshot = sdk_system.get_operational_snapshot(
            sections=sdk_sections,
            webspace_id=webspace_id,
            limit=40,
        )
        if not isinstance(snapshot, dict) or snapshot.get("ok") is False:
            raise ValueError("system_snapshot_unavailable")
    except Exception:  # Transport details are not a browser-facing diagnostic.
        return {
            "ok": False,
            "error": "system_overview_failed",
            "message": "System status is unavailable. Retry when the node is connected.",
            "metrics": {}, "items": [], "count": 0, "total": 0,
            "empty": True, "truncated": False,
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
    resources = _mapping(snapshot.get("resources"))
    applications = [
        _canonical_item(item, source="application_registry")
        | {
            "version": _string(item.get("version")),
            "channel": _string(item.get("channel")),
            "update_state": _string(item.get("update_state")),
            "has_update": bool(item.get("has_update")),
        }
        for item in list(snapshot.get("applications") or [])
        if isinstance(item, dict)
    ]
    members = [
        _canonical_item(item, source="subnet_membership")
        | {
            "connection": _string(item.get("connection") or item.get("status")),
            "active": _string(item.get("status") or item.get("connection")).casefold()
            in {"online", "connected", "heartbeat", "ready"},
            "is_hub": bool(item.get("is_hub") or item.get("role") == "hub"),
            "development": bool(item.get("development")),
            "runtime_channel": _string(item.get("runtime_channel")),
            "runtime_version": _string(item.get("runtime_version") or item.get("version")),
        }
        for item in list(snapshot.get("members") or [])
        if isinstance(item, dict)
    ]
    skills = [
        _canonical_item(item, source="skill_registry")
        for item in list(snapshot.get("skills") or [])
        if isinstance(item, dict)
    ]
    for collection in (services, connections, quotas):
        collection.sort(
            key=lambda item: (item["kind"], item["title"].casefold(), item["id"])
        )
    capacity_resources = _mapping(capacity.get("resources"))
    active_skill_total = _operational_number(capacity_resources.get("active_skill_total"))
    active_scenario_total = _operational_number(capacity_resources.get("active_scenario_total"))
    capacity_known = active_skill_total is not None and active_scenario_total is not None
    services_loaded = selected_section in {"", "all", "services"} and isinstance(snapshot.get("services"), list)
    if services_loaded:
        service_value: int | str = len(services)
        service_description = (
            f"{sum(item['status'] == 'online' for item in services)} online"
        )
    else:
        service_value = active_skill_total + active_scenario_total if capacity_known else "Unavailable"
        service_description = (
            f"{active_skill_total} skills, {active_scenario_total} scenarios"
            if capacity_known else "Runtime component counts unavailable"
        )
    incidents_loaded = selected_section in {"", "all", "incidents", "node_dashboard"} and isinstance(snapshot.get("incidents"), list)
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
            "color": "success" if services_loaded and incidents_loaded and not incidents else "warning",
        },
        "capacity": {
            "value": active_skill_total if active_skill_total is not None else "Unavailable",
            "label": "Active skills",
            "description": f"{active_scenario_total} active scenarios" if active_scenario_total is not None else "Scenario count unavailable",
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
            "value": len(connections) if selected_section in {"", "all", "connections"} and isinstance(snapshot.get("connections"), list) else "--",
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
            "description": _string(update.get("message")) or "Update status unavailable",
            "color": "warning"
            if _string(update.get("state")).casefold() not in {"idle", "ready", "succeeded"}
            else "success",
        },
    }
    resource_freshness = _string(resources.get("freshness")) or "unavailable"
    resource_available = resources.get("available") is True
    for metric_id, source_key in (("cpu", "cpu"), ("ram", "memory"), ("disk", "disk")):
        sample = _mapping(resources.get(source_key))
        percent = _operational_number(sample.get("percent"))
        if percent is not None and percent > 100:
            percent = None
        metrics[metric_id] = {
            "value": f"{float(percent):.1f}%" if resource_available and percent is not None else "--",
            "label": metric_id.upper(),
            "freshness": resource_freshness,
            "color": "success"
            if resource_available and percent is not None and float(percent) < 80
            else "warning",
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
        "subnet": _mapping(snapshot.get("subnet")),
        "service_summary": _mapping(snapshot.get("service_summary")),
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
        if selected_section in {"", "all", "incidents", "node_dashboard"}
        else [],
        "update": update if selected_section in {"", "all", "update", "node_dashboard"} else {},
        "applications": applications if selected_section in {"", "all", "applications", "dashboard", "system_bootstrap", "node_dashboard"} else [],
        "members": members if selected_section in {"", "all", "members", "dashboard", "system_bootstrap"} else [],
        "member_summary": _mapping(snapshot.get("member_summary")),
        "application_updates": _mapping(snapshot.get("application_updates")),
        "skills": skills if selected_section in {"skills", "node_dashboard"} else [],
        "skill_summary": _mapping(snapshot.get("skill_summary"))
        if selected_section in {"skills", "node_dashboard"}
        else {},
        "update_controls": _mapping(snapshot.get("update_controls"))
        if selected_section in {"update", "node_dashboard"}
        else {},
        "resources": resources if selected_section in {"", "all", "metrics", "node_dashboard"} else {},
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
    # These projections are section-driven. Technical data is requested only
    # explicitly, never as part of the first-paint summary.
    if selected_section in {"development", "dashboard", "system_bootstrap"}:
        result["development_delivery"] = _mapping(
            snapshot.get("development_delivery")
        ) or {"available": False, "freshness": "unavailable"}
    if selected_section == "activity":
        raw_activity = snapshot.get("activity")
        result["activity"] = (
            [dict(item) for item in raw_activity if isinstance(item, dict)]
            if isinstance(raw_activity, list)
            else _mapping(raw_activity)
            or {"available": False, "freshness": "unavailable", "items": []}
        )
    if selected_section == "technical":
        result["technical"] = _mapping(snapshot.get("technical")) or {
            "available": False,
            "freshness": "unavailable",
        }
    if selected_section in {"activity", "technical"}:
        projection = result[selected_section]
        result["item"] = {
            **(projection if isinstance(projection, dict) else {}),
            "content": (
                _activity_content(projection if isinstance(projection, dict) else {"items": projection})
                if selected_section == "activity"
                else _technical_content(projection)
            ),
        }
    if selected_section == "skills":
        summary = result["skill_summary"]
        total = summary.get("total")
        known = summary.get("available") is True and type(total) is int and total >= 0
        item = {
            "id": subject.get("id", ""), "status": subject.get("status", "unknown"),
            "apps_count": total if known else None,
            "available": known, "freshness": summary.get("freshness", "unavailable"),
            "observed_at": snapshot.get("observed_at"), "source": summary.get("source"),
        }
        result.update(item=item, items=[item] if item["id"] else [],
                      count=1 if item["id"] else 0, total=1 if item["id"] else 0,
                      empty=not bool(item["id"]))
    if selected_section == "summary":
        result.update(metrics.get("node", {}))
        result["services"] = []
        result["connections"] = []
        result["quotas"] = []
        result["incidents"] = []
        result["update"] = {}
    if selected_section in {"dashboard", "system_bootstrap"}:
        result["subnet_panel"] = _project_operational_dashboard(snapshot, "subnet")
        result["applications_tile"] = _project_operational_dashboard(snapshot, "applications")
    if selected_section == "system_bootstrap":
        # One admitted read-model for System first paint.  The SDK reads are
        # deliberately sequential: parallel tool fan-out amplifies SQLite and
        # Python event-loop contention on a hub and was slower in production
        # traces.  Keep the flat preference fields for ui.form compatibility,
        # while retaining named submodels for diagnostics and future masks.
        try:
            preference_item = _preference_record(webspace_id=webspace_id)
        except Exception as exc:
            preference_item = {
                "ok": False,
                "status": "unavailable",
                "reason": type(exc).__name__,
            }
        try:
            from .usage import get_subscription_usage as read_usage

            usage = read_usage(webspace_id=webspace_id, refresh=False)
        except Exception as exc:
            usage = {
                "ok": False,
                "status": "unavailable",
                "reason": type(exc).__name__,
                "usage_arc": {},
            }
        result["preferences"] = preference_item
        result["subscription_usage"] = usage
        result.update(preference_item)
        if isinstance(usage, dict):
            for key in ("usage_arc", "usage_status", "updated_at"):
                if key in usage:
                    result[key] = usage[key]
            result["subscription_status"] = usage.get("status")
    if selected_section == "node_dashboard":
        skill_summary = _mapping(snapshot.get("skill_summary"))
        skill_total = skill_summary.get("total")
        skill_known = skill_summary.get("available") is True and type(skill_total) is int and skill_total >= 0
        result["node_details"] = {
            "id": subject.get("id", ""),
            "status": subject.get("status", "unknown"),
            "apps_count": skill_total if skill_known else "Unavailable",
            "observed_at": snapshot.get("observed_at"),
        }
        result["update_tile"] = _project_operational_dashboard(snapshot, "update")
        # Keep the named submodel for diagnostics, but also expose the stable
        # flat dashboard ABI consumed by the ArcChart.  The node dashboard is
        # a composite read-model; projecting the literal ``node_dashboard``
        # section below cannot manufacture the metric fields on its own.
        hardware = _project_operational_dashboard(snapshot, "metrics")
        result["hardware"] = hardware
        for key in (
            "hardware_metrics",
            "center",
            "subtitle",
            "freshness",
            "value",
            "label",
            "description",
        ):
            if key in hardware:
                result[key] = hardware[key]
        result["attention_tile"] = _project_operational_dashboard(snapshot, "incidents")
    result.update(_project_operational_dashboard(snapshot, selected_section))
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
            # The UI already declares a bounded result window.  Scanning 500
            # Builder projects for a six-row Home card dominated first paint.
            limit=bounded_limit,
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
    endpoint_name = ""
    if _bool_value(device_override) and "deviceLabel" in submitted:
        endpoint_name = _string(submitted.get("deviceLabel"))
        preference_patch.pop("device_label", None)
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
        if endpoint_name:
            endpoint_ref = _string(controller_endpoint_id)
            if not endpoint_ref.startswith("browser:"):
                raise ValueError("current_browser_endpoint_unavailable")
            rename_result = rename_device(endpoint_ref, endpoint_name)
            if not _bool_value(rename_result.get("ok")):
                raise ValueError(
                    _string(rename_result.get("error")) or "browser_endpoint_rename_failed"
                )
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
        "updated_fields": sorted(
            [*profile_patch, *preference_patch, *(("browser_endpoint_name",) if endpoint_name else ())]
        ),
        "items": [item],
        "item": item,
        **item,
    }


@tool(
    "upload_profile_avatar",
    summary="Persist one authenticated profile avatar in the Desktop-owned blob store.",
    stability="experimental",
)
def upload_profile_avatar(
    filename: str,
    field_id: str,
    media_type: str,
    size_bytes: int,
    digest: str,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    if (
        field_id != _PROFILE_AVATAR_FIELD_ID
        or not _SAFE_FILENAME_RE.fullmatch(str(filename or ""))
        or type(size_bytes) is not int
        or not 1 <= size_bytes <= _PROFILE_AVATAR_MAX_BYTES
        or not _SHA256_REF_RE.fullmatch(str(digest or ""))
        or not str(media_type or "").lower().startswith("image/")
        or len(str(media_type or "")) > 128
    ):
        raise ValueError("invalid_profile_avatar")
    receipt = put_upload("profile-avatars")
    if (
        receipt.get("digest") != digest
        or receipt.get("size_bytes") != size_bytes
        or receipt.get("owner_ref") != "skill:web_desktop_runtime_skill"
        or receipt.get("media_type") != media_type
    ):
        raise ValueError("invalid_profile_avatar_receipt")
    return receipt


@tool(
    "read_profile_avatar",
    summary="Authorize an authenticated reader to retrieve a Desktop-owned profile avatar.",
    stability="experimental",
)
def read_profile_avatar(
    ref: str,
    field_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    sdk_access.require("workspace.read")
    token = str(ref or "").strip()
    if not _SHA256_REF_RE.fullmatch(token):
        raise ValueError("invalid_profile_avatar_ref")
    return {"ok": True, "ref": token}


@tool(
    "unlink_node",
    summary="Detach a paired member node from the current subnet.",
    stability="experimental",
    examples=["unlink_node({'device_ref': 'member:edge-node-1'})"],
)
def unlink_node(
    device_ref: str | None = None,
    node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    sdk_access.require("workspace.write")
    token = _string(device_ref or node_id)
    if token.startswith("device:member:"):
        token = f"member:{token.removeprefix('device:member:')}"
    elif token and not token.startswith(("member:", "browser:", "redevice:")):
        token = f"member:{token}"
    if not token or token == "member:hub":
        return {
            "ok": False,
            "error": "node_ref_required",
            "message": "Select a paired member node. The current hub cannot unlink itself.",
        }
    result = sdk_device_access.detach_device(token)
    if not isinstance(result, dict):
        return {
            "ok": False,
            "error": "node_unlink_failed",
            "message": "The device inventory returned an invalid detach result.",
        }
    return {
        **result,
        "device_ref": token,
        "message": (
            "Node unlinked. It can be paired again later."
            if result.get("ok")
            else _string(result.get("message")) or "The node could not be unlinked."
        ),
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
