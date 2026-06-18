from __future__ import annotations

import time
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import device_access as sdk_device_access
from adaos.sdk.data import devices as sdk_devices
from adaos.sdk.data import skill_memory_get, skill_memory_set
from adaos.sdk.io import stream_publish
from adaos.services.redevice_versions import endpoint_version_info

try:
    from adaos.sdk import redevice as sdk_redevice
except Exception:  # pragma: no cover - older core without ReDevice SDK
    sdk_redevice = None

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


_RECEIVER = "redevice_settings.state"
_SELECTED_BY_WS_KEY = "selected_by_webspace"
_ASSIGNMENTS_KEY = "endpoint_assignments"
_LAST_COMMAND_KEY = "last_command"
_ASSIGNMENT_PRESETS = ["assistant", "slideshow", "voice_endpoint", "media_center", "webcam", "idle"]
_MAX_TABLE_ITEMS = 32
_MIN_PUBLISH_INTERVAL_S = 1.0
_LAST_PUBLISH_AT: dict[str, float] = {}
_LAST_PUBLISH_FINGERPRINT: dict[str, str] = {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _compact_value(value: Any, *, depth: int = 0, max_fields: int = 16, max_text: int = 180) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_fields:
                result["_truncated_fields"] = len(value) - max_fields
                break
            result[str(key)] = _compact_value(item, depth=depth + 1, max_fields=max_fields, max_text=max_text)
        return result
    if isinstance(value, list):
        if depth >= 2:
            return f"{len(value)} items"
        return [_compact_value(item, depth=depth + 1, max_fields=max_fields, max_text=max_text) for item in value[:8]]
    if isinstance(value, tuple):
        return _compact_value(list(value), depth=depth, max_fields=max_fields, max_text=max_text)
    if isinstance(value, str):
        return value if len(value) <= max_text else value[: max_text - 1] + "..."
    return value


def _compact_result(value: Any) -> dict[str, Any]:
    result = _mapping(value)
    if not result:
        return {}
    allowed = {
        "ok",
        "error",
        "message",
        "state",
        "status",
        "code",
        "device_ref",
        "endpoint_id",
        "command_id",
        "online_state",
        "updated_at",
    }
    compact = {key: _compact_value(result.get(key), max_fields=8, max_text=120) for key in allowed if key in result}
    if not compact:
        compact = _compact_value(result, max_fields=8, max_text=120)
    return compact if isinstance(compact, dict) else {"value": compact}


def _diagnostics_summary(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(diagnostics)
    return {
        "endpoint_manifest": _compact_value(_mapping(payload.get("endpoint_manifest")), max_fields=14, max_text=160),
        "endpoint_policy": _compact_value(_mapping(payload.get("endpoint_policy")), max_fields=14, max_text=160),
        "diagnostic_report": _compact_value(_mapping(payload.get("diagnostic_report")), max_fields=14, max_text=160),
        "endpoint_health": _compact_value(_mapping(payload.get("endpoint_health")), max_fields=14, max_text=160),
        "service_state": _compact_value(_mapping(payload.get("service_state")), max_fields=14, max_text=160),
        "policy_source": _text(payload.get("policy_source")),
    }


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _age(value: Any) -> str:
    try:
        ts = float(value or 0)
    except Exception:
        return "-"
    if ts <= 0:
        return "-"
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {sec % 3600 // 60}m"


def _explicit_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes", "online"}:
            return True
        if token in {"false", "0", "no", "offline"}:
            return False
    return None


def _last_seen_age_s(value: Any) -> float | None:
    try:
        ts = float(value or 0)
    except Exception:
        return None
    if ts <= 0:
        return None
    return max(0.0, time.time() - ts)


def _recently_seen(value: Any, *, max_age_s: float = 180.0) -> bool:
    age = _last_seen_age_s(value)
    return age is not None and age <= max_age_s


def _connection_state(value: Any, *, online: bool | None, last_seen_at: Any) -> str:
    token = _text(value).lower()
    if token and token != "unknown":
        if token == "offline" and online is True:
            return "online"
        return token
    if online is True:
        return "online"
    if online is False:
        return "offline"
    if _recently_seen(last_seen_at):
        return "online"
    if _last_seen_age_s(last_seen_at) is not None:
        return "stale"
    return "unknown"


def _memory_dict(key: str) -> dict[str, Any]:
    try:
        value = skill_memory_get(key, {})
    except Exception:
        value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def _set_memory_dict(key: str, value: Mapping[str, Any]) -> None:
    try:
        skill_memory_set(key, dict(value))
    except Exception:
        pass


def _webspace_id(value: str | None = None) -> str:
    return _text(value) or default_webspace_id()


def _selected_by_ws() -> dict[str, str]:
    return {str(k): str(v) for k, v in _memory_dict(_SELECTED_BY_WS_KEY).items() if str(k) and str(v)}


def _assignments() -> dict[str, str]:
    return {str(k): str(v) for k, v in _memory_dict(_ASSIGNMENTS_KEY).items() if str(k)}


def _set_selected(webspace_id: str, device_ref: str) -> None:
    state = _selected_by_ws()
    state[_webspace_id(webspace_id)] = _text(device_ref)
    _set_memory_dict(_SELECTED_BY_WS_KEY, state)


def _device_ref_from_item(item: Mapping[str, Any]) -> str:
    ref = _text(item.get("ref"))
    if ref:
        return ref
    identity = _mapping(item.get("identity"))
    endpoint_id = _text(identity.get("endpoint_id") or identity.get("link_id") or item.get("endpoint_id") or item.get("code"))
    return f"redevice:{endpoint_id}" if endpoint_id else ""


def _normalize_device(item: Mapping[str, Any], *, selected_ref: str | None = None) -> dict[str, Any]:
    raw = _mapping(item.get("raw"))
    identity = _mapping(item.get("identity"))
    policy = _mapping(item.get("policy"))
    observation = _mapping(item.get("observation"))
    runtime = _mapping(item.get("runtime"))
    diagnostics = _mapping(item.get("diagnostics"))
    if raw:
        raw_policy = _mapping(raw.get("endpoint_policy"))
        raw_manifest = _mapping(raw.get("endpoint_manifest"))
        if not policy:
            policy = {
                "trust_level": _text(item.get("trust_level") or raw_policy.get("trust_level") or raw_manifest.get("trust_level")),
                "display_name": _text(item.get("display_name") or raw.get("display_name") or raw.get("device_label") or raw_manifest.get("display_name")),
                "aliases": list(raw.get("aliases") or item.get("aliases") or []),
                "hub_id": _text(raw.get("hub_id") or raw_policy.get("hub_id") or raw_manifest.get("hub_id")),
                "zone_id": _text(raw.get("zone_id") or raw_manifest.get("zone_id")),
            }
        if not observation:
            last_seen_at = raw.get("last_seen_at") or item.get("last_seen_at")
            explicit_online = _explicit_bool(item.get("online"))
            online = explicit_online if explicit_online is not None else _recently_seen(last_seen_at)
            observation = {
                "online": online,
                "connection_state": _connection_state(
                    item.get("online_state") or item.get("connection_state"),
                    online=online,
                    last_seen_at=last_seen_at,
                ),
                "last_seen_at": last_seen_at,
            }
        if not runtime:
            runtime = {
                "active_app": item.get("active_app") or raw.get("active_app"),
                "active_surface": item.get("active_surface") or raw.get("active_surface"),
            }
        if not diagnostics:
            diagnostics = {
                "endpoint_manifest": raw_manifest,
                "endpoint_policy": raw_policy,
                "diagnostic_report": _mapping(raw.get("diagnostic_report")),
                "endpoint_health": _mapping(raw.get("endpoint_health")),
                "service_state": _mapping(raw.get("service_state")),
            }
    ref = _device_ref_from_item(item)
    endpoint_id = _text(identity.get("endpoint_id") or identity.get("link_id") or item.get("endpoint_id"))
    pair_code = _text(identity.get("pair_code") or item.get("code"))
    effective_name = _text(policy.get("effective_name") or policy.get("display_name") or item.get("display_name") or item.get("title")) or endpoint_id or pair_code or "ReDevice"
    last_seen_at = observation.get("last_seen_at")
    explicit_online = _explicit_bool(observation.get("online"))
    inferred_online = explicit_online if explicit_online is not None else _recently_seen(last_seen_at)
    connection_state = _connection_state(
        observation.get("connection_state") or runtime.get("snapshot_state"),
        online=inferred_online,
        last_seen_at=last_seen_at,
    )
    active_app = _mapping(runtime.get("active_app"))
    active_surface = _mapping(runtime.get("active_surface"))
    assignment = _assignments().get(ref) or _text(active_app.get("app_id")) or "idle"
    endpoint_health = _mapping(diagnostics.get("endpoint_health"))
    diagnostic_report = _mapping(diagnostics.get("diagnostic_report"))
    service_state = _mapping(diagnostics.get("service_state"))
    capabilities = _mapping(diagnostic_report.get("capabilities"))
    battery = _mapping(endpoint_health.get("battery") or diagnostic_report.get("battery"))
    if not battery and ("battery_level" in diagnostic_report or "charging" in diagnostic_report):
        battery = {
            "level": diagnostic_report.get("battery_level"),
            "charging": diagnostic_report.get("charging"),
            "state": "charging" if diagnostic_report.get("charging") else "battery",
        }
    network = _mapping(endpoint_health.get("network") or endpoint_health.get("connectivity") or diagnostic_report.get("network"))
    if not network:
        network = {
            "state": "online" if diagnostic_report.get("network_online") else "offline",
            "wifi": _mapping(capabilities.get("network.wifi")),
        }
    audio = _mapping(diagnostic_report.get("audio") or service_state.get("audio_output_endpoint"))
    audio_input = _mapping(audio.get("input") or capabilities.get("audio.input") or service_state.get("audio_input_endpoint"))
    audio_output = _mapping(audio.get("output") or capabilities.get("audio.output") or service_state.get("audio_output_endpoint"))
    if audio_input or audio_output:
        audio = {
            **audio,
            "input": audio_input,
            "output": audio_output,
            "state": _text(audio.get("state") or audio_output.get("quality") or audio_input.get("quality")),
            "quality": _text(audio.get("quality") or audio_output.get("quality") or audio_input.get("quality")),
        }
    display = _mapping(diagnostic_report.get("screen") or diagnostic_report.get("display") or service_state.get("display_endpoint") or capabilities.get("screen"))
    bluetooth = _mapping(diagnostic_report.get("bluetooth") or service_state.get("bluetooth_endpoint") or capabilities.get("network.bluetooth"))
    location = _mapping(diagnostic_report.get("location") or service_state.get("location_endpoint"))
    selected = bool(ref and ref == _text(selected_ref))
    endpoint_policy = _mapping(diagnostics.get("endpoint_policy"))
    manifest = _mapping(diagnostics.get("endpoint_manifest"))
    version_info = endpoint_version_info(item)
    software_version = _text(version_info.get("software_version")) or "-"
    served_version = _text(version_info.get("served_version")) or "unknown"
    version_status = _text(version_info.get("version_status")) or "unknown"
    lifecycle_state = _text(item.get("state") or raw.get("state")) or "unknown"
    return {
        "id": ref or pair_code or endpoint_id,
        "ref": ref,
        "code": pair_code,
        "endpoint_id": endpoint_id,
        "lifecycle_state": lifecycle_state,
        "title": effective_name,
        "selected": selected,
        "selected_label": "selected" if selected else "",
        "online": bool(inferred_online),
        "online_state": connection_state,
        "last_seen": _age(observation.get("last_seen_at")),
        "trust_level": _text(policy.get("trust_level")) or _text(endpoint_policy.get("trust_level")) or "limited",
        "assignment": assignment,
        "active_app": _text(active_app.get("label") or active_app.get("app_id")) or "-",
        "active_surface": _text(active_surface.get("surface_ref") or active_surface.get("surface_id")) or "-",
        "software_version": software_version,
        "served_version": served_version,
        "version_status": version_status,
        "version_info": version_info,
        "aliases": ", ".join(str(item or "").strip() for item in list(policy.get("aliases") or policy.get("labels") or []) if str(item or "").strip()),
        "battery": battery or {},
        "network": network or {},
        "audio": audio or {},
        "display": display or {},
        "bluetooth": bluetooth or {},
        "location": location or {},
        "subnet": {
            "zone_id": _text(manifest.get("zone_id") or policy.get("zone_id") or endpoint_policy.get("zone_id")),
            "assistant_name": _text(manifest.get("assistant_name") or manifest.get("subnet_name") or policy.get("assistant_name")),
            "hub_id": _text(manifest.get("hub_id") or endpoint_policy.get("hub_id") or policy.get("hub_id")),
            "node_name": _text(manifest.get("node_name") or manifest.get("hub_name") or policy.get("node_name")),
            "policy_id": _text(endpoint_policy.get("policy_id") or endpoint_policy.get("id")),
        },
        "diagnostics": _diagnostics_summary(diagnostics),
        "commandable": lifecycle_state in {"approved", "consumed", "unknown"} and bool(pair_code),
    }


def _is_commandable(item: Mapping[str, Any]) -> bool:
    state = _text(item.get("lifecycle_state")).lower()
    return bool(item.get("commandable")) and state not in {"revoked", "retired", "expired"}


def _table_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Keep fleet rows lightweight; selected details carry diagnostics."""
    allowed = {
        "id",
        "ref",
        "code",
        "endpoint_id",
        "lifecycle_state",
        "title",
        "selected",
        "selected_label",
        "online",
        "online_state",
        "last_seen",
        "trust_level",
        "assignment",
        "active_app",
        "active_surface",
        "software_version",
        "served_version",
        "version_status",
        "aliases",
        "commandable",
    }
    return {key: item.get(key) for key in allowed if key in item}


def _load_devices(selected_ref: str | None = None) -> list[dict[str, Any]]:
    raw_items: list[Mapping[str, Any]] = []
    list_endpoint_devices = getattr(sdk_device_access, "list_endpoint_devices", None)
    if callable(list_endpoint_devices):
        try:
            raw_items = [item for item in list_endpoint_devices("redevice", sync_registry=True) if isinstance(item, Mapping)]
        except Exception:
            raw_items = []
    if not raw_items and sdk_redevice is not None:
        try:
            compact = getattr(sdk_redevice, "compact_endpoint", None)
            endpoints = [item for item in sdk_redevice.list_endpoints(sync_registry=True) if isinstance(item, Mapping)]
            if callable(compact):
                raw_items = [compact(item) for item in endpoints]
            else:
                raw_items = endpoints
        except Exception:
            raw_items = []
    if not raw_items:
        try:
            raw_items = [item for item in sdk_devices.list_devices(kind="redevice") if isinstance(item, Mapping)]
        except Exception:
            raw_items = []
    normalized = [_normalize_device(item, selected_ref=selected_ref) for item in raw_items if isinstance(item, Mapping)]
    return [item for item in normalized if _is_commandable(item)]


def _first_selected(items: list[dict[str, Any]], webspace_id: str | None = None) -> str:
    ws = _webspace_id(webspace_id)
    selected = _selected_by_ws().get(ws, "")
    refs_by_item = [(item, _text(item.get("ref"))) for item in items if _text(item.get("ref"))]
    selected_item = next((item for item, ref in refs_by_item if ref == selected), None)
    if selected_item and bool(selected_item.get("online")):
        return selected
    for item in items:
        ref = _text(item.get("ref"))
        if ref and bool(item.get("online")) and _is_commandable(item):
            _set_selected(ws, ref)
            return ref
    if selected_item and _is_commandable(selected_item):
        return selected
    for item in items:
        ref = _text(item.get("ref"))
        if ref and _is_commandable(item):
            _set_selected(ws, ref)
            return ref
    return ""


def _status_cards(selected: Mapping[str, Any] | None, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_map = dict(selected or {})
    online_count = sum(1 for item in items if item.get("online"))
    return [
        {
            "id": "fleet",
            "title": "Endpoints",
            "value": len(items),
            "subtitle": f"{online_count} online",
        },
        {
            "id": "selected",
            "title": "Selected",
            "value": _text(selected_map.get("title")) or "-",
            "subtitle": _text(selected_map.get("online_state")) or "not selected",
        },
        {
            "id": "assignment",
            "title": "Assignment",
            "value": _text(selected_map.get("assignment")) or "-",
            "subtitle": _text(selected_map.get("active_app")) or "-",
        },
        {
            "id": "version",
            "title": "Agent",
            "value": _text(selected_map.get("software_version")) or "-",
            "subtitle": f"served {_text(selected_map.get('served_version')) or 'unknown'} | {_text(selected_map.get('version_status')) or 'unknown'}",
        },
        {
            "id": "trust",
            "title": "Trust",
            "value": _text(selected_map.get("trust_level")) or "-",
            "subtitle": f"last seen {_text(selected_map.get('last_seen')) or '-'}",
        },
    ]


def _summary(selected: Mapping[str, Any] | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    selected_map = dict(selected or {})
    online_count = sum(1 for item in items if item.get("online"))
    assignment = _text(selected_map.get("assignment")) or "idle"
    last_seen = _text(selected_map.get("last_seen")) or "-"
    code = _text(selected_map.get("code")) or "-"
    trust_level = _text(selected_map.get("trust_level")) or "-"
    version = _text(selected_map.get("software_version")) or "-"
    served_version = _text(selected_map.get("served_version")) or "unknown"
    return {
        "fleet": {
            "value": len(items),
            "label": "ReDevice endpoints",
            "subtitle": f"{online_count} online",
            "description": "Endpoint Registry snapshot",
            "color": "success" if online_count else "warning",
        },
        "selected": {
            "value": _text(selected_map.get("title")) or "No endpoint selected",
            "label": _text(selected_map.get("online_state")) or "not selected",
            "subtitle": f"seen {last_seen} | code {code}",
            "description": f"{assignment} | trust {trust_level} | agent {version}/{served_version}",
            "color": "success" if selected_map.get("online") else "warning" if selected_map else "danger",
        },
        "assignment": {
            "value": assignment.replace("_", " ").title(),
            "label": _text(selected_map.get("active_app")) or "-",
            "subtitle": _text(selected_map.get("active_surface")) or "-",
            "description": "Current endpoint role and active surface",
            "color": "primary" if assignment and assignment != "idle" else "",
        },
    }


def _inspection_groups(selected: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    item = dict(selected or {})
    network = _mapping(item.get("network"))
    bluetooth = _mapping(item.get("bluetooth"))
    audio = _mapping(item.get("audio"))
    display = _mapping(item.get("display"))
    battery = _mapping(item.get("battery"))
    diagnostics = _mapping(item.get("diagnostics"))
    manifest = _mapping(diagnostics.get("endpoint_manifest"))
    policy = _mapping(diagnostics.get("endpoint_policy"))
    software_version = _text(item.get("software_version")) or "-"
    served_version = _text(item.get("served_version")) or "unknown"
    version_status = _text(item.get("version_status")) or "unknown"
    return [
        {
            "id": "connectivity",
            "title": "Connectivity",
            "description": _text(network.get("ssid") or network.get("state")) or _text(item.get("online_state")) or "unknown",
            "subtitle": "Wi-Fi and subnet reachability",
            "icon": "wifi-outline",
        },
        {
            "id": "bluetooth",
            "title": "Bluetooth",
            "description": _text(bluetooth.get("state")) or "unknown",
            "subtitle": "Output pairing and reconnect hints",
            "icon": "bluetooth-outline",
        },
        {
            "id": "io",
            "title": "Audio and display",
            "description": f"audio {_text(audio.get('state') or audio.get('quality')) or 'unknown'} | screen {_text(display.get('readability') or display.get('state')) or 'unknown'}",
            "subtitle": "Speaker, microphone, screen and active surface",
            "icon": "tablet-landscape-outline",
        },
        {
            "id": "power",
            "title": "Power and sensors",
            "description": _text(battery.get("level") or battery.get("state")) or "unknown",
            "subtitle": "Battery, location and degraded role hints",
            "icon": "battery-half-outline",
        },
        {
            "id": "contracts",
            "title": "Contracts",
            "description": f"agent {software_version}/{served_version} | {version_status}",
            "subtitle": "Manifest, policy and diagnostics payload",
            "icon": "document-text-outline",
        },
    ]


def _section_rows(selected: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    item = dict(selected or {})
    network = _mapping(item.get("network"))
    bluetooth = _mapping(item.get("bluetooth"))
    audio = _mapping(item.get("audio"))
    display = _mapping(item.get("display"))
    battery = _mapping(item.get("battery"))
    location = _mapping(item.get("location"))
    subnet = _mapping(item.get("subnet"))
    diagnostics = _mapping(item.get("diagnostics"))
    endpoint_health = _mapping(diagnostics.get("endpoint_health"))
    manifest = _mapping(diagnostics.get("endpoint_manifest"))
    policy = _mapping(diagnostics.get("endpoint_policy"))
    version_info = _mapping(item.get("version_info"))
    return {
        "overview": [
            {"id": "name", "title": "Name", "description": _text(item.get("title")) or "-"},
            {"id": "ref", "title": "Device ref", "description": _text(item.get("ref")) or "-", "subtitle": _text(item.get("endpoint_id")) or "-"},
            {"id": "subnet", "title": "Subnet", "description": _text(subnet.get("assistant_name")) or _text(subnet.get("zone_id")) or "-", "subtitle": f"node {_text(subnet.get('node_name') or subnet.get('hub_id')) or '-'}"},
            {"id": "assignment", "title": "Current assignment", "description": _text(item.get("assignment")) or "idle"},
            {"id": "active", "title": "Active app", "description": _text(item.get("active_app")) or "-", "subtitle": _text(item.get("active_surface")) or "-"},
            {"id": "agent_version", "title": "Agent version", "description": _text(item.get("software_version")) or "-", "subtitle": f"served {_text(item.get('served_version')) or 'unknown'} | {_text(item.get('version_status')) or 'unknown'}"},
        ],
        "network": [
            {"id": "wifi", "title": "Wi-Fi", "description": _text(network.get("ssid") or network.get("state")) or "read-only", "subtitle": "Agent can assist but does not manage physical network."},
            {"id": "connected", "title": "Subnet", "description": _text(item.get("online_state")) or "-", "subtitle": f"last seen {_text(item.get('last_seen')) or '-'}"},
            {"id": "hub", "title": "Hub", "description": _text(subnet.get("node_name") or subnet.get("hub_id")) or "-", "subtitle": _text(subnet.get("policy_id")) or "policy unknown"},
        ],
        "bluetooth": [
            {"id": "available", "title": "Bluetooth", "description": _text(bluetooth.get("state") or bluetooth.get("quality")) or "unknown", "subtitle": "A2DP auto-connect is best-effort unless privileged."},
            {"id": "preferred", "title": "Preferred speaker", "description": _text(bluetooth.get("preferred_output") or bluetooth.get("preferred_device") or "-")},
            {"id": "headset", "title": "Headset", "description": _text(bluetooth.get("headset") or bluetooth.get("input_device") or "-"), "subtitle": "Headset microphone routing requires OS support and policy."},
            {"id": "diagnostic", "title": "Diagnostics", "description": "open Bluetooth settings or reconnect output", "subtitle": "Remote pairing is guided; legacy Android may require local confirmation."},
        ],
        "audio": [
            {"id": "input", "title": "Audio input", "description": _text(_mapping(audio.get("input")).get("quality") or _mapping(audio.get("input")).get("state")) or "unknown", "subtitle": "Used by endpoint_audio_service VAD/PTT."},
            {"id": "output", "title": "Audio output", "description": _text(_mapping(audio.get("output")).get("quality") or audio.get("state") or audio.get("quality")) or "unknown", "subtitle": "Speaker or Bluetooth output."},
            {"id": "volume", "title": "Volume", "description": _text(audio.get("volume") or "-"), "subtitle": "Volume commands are policy-bound and may be OS-limited."},
        ],
        "display": [
            {"id": "screen", "title": "Display", "description": _text(display.get("readability") or display.get("state")) or "unknown"},
            {"id": "surface", "title": "Surface", "description": _text(item.get("active_surface")) or "-"},
        ],
        "battery": [
            {"id": "level", "title": "Battery", "description": _text(battery.get("level") or battery.get("state") or endpoint_health.get("battery") or "-")},
            {"id": "power", "title": "Power role", "description": _text(battery.get("power_role") or "-"), "subtitle": "Weak batteries should prefer fixed-power roles."},
        ],
        "location": [
            {"id": "permission", "title": "Location", "description": _text(location.get("state") or "not enabled")},
            {"id": "policy", "title": "Policy", "description": "disabled by default", "subtitle": "Location streams require explicit endpoint policy."},
        ],
        "apps": [
            {"id": "settings", "title": "Settings", "description": "service skill", "subtitle": "This modal owns device settings UX."},
            {"id": "slideshow", "title": "Slideshow", "description": "scenario dependency", "subtitle": "Use selected endpoint from slideshow modal."},
            {"id": "voice", "title": "ReDevice Voice", "description": "scenario dependency", "subtitle": "PTT and VAD debug surfaces."},
        ],
        "about": [
            {"id": "version", "title": "Version", "description": _text(item.get("version_status")) or "unknown", "subtitle": f"used {_text(item.get('software_version')) or '-'} | served {_text(item.get('served_version')) or 'unknown'}", "details": version_info},
            {"id": "manifest", "title": "Manifest", "description": _text(manifest.get("schema_version") or "-"), "details": manifest},
            {"id": "policy", "title": "Policy", "description": _text(policy.get("policy_id") or policy.get("id") or "-"), "details": policy},
            {"id": "diagnostics", "title": "Diagnostics", "description": _text(diagnostics.get("policy_source") or "-"), "details": diagnostics},
        ],
        "right_summary": [
            {"id": "name", "title": "Name", "description": _text(item.get("title")) or "-"},
            {"id": "state", "title": "State", "description": _text(item.get("online_state")) or "-", "subtitle": f"seen {_text(item.get('last_seen')) or '-'}"},
            {"id": "role", "title": "Role", "description": _text(item.get("assignment")) or "idle", "subtitle": _text(item.get("active_app")) or "-"},
            {"id": "version", "title": "Agent", "description": _text(item.get("software_version")) or "-", "subtitle": f"served {_text(item.get('served_version')) or 'unknown'}"},
            {"id": "trust", "title": "Trust", "description": _text(item.get("trust_level")) or "-", "subtitle": f"code {_text(item.get('code')) or '-'}"},
        ],
    }


def _build_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    ws = _webspace_id(webspace_id)
    first_items = _load_devices()
    selected_ref = _first_selected(first_items, ws)
    items = _load_devices(selected_ref)
    selected = next((item for item in items if item.get("selected")), None)
    table_items = [_table_item(item) for item in items[:_MAX_TABLE_ITEMS]]
    last_command = _memory_dict(_LAST_COMMAND_KEY)
    return {
        "ok": True,
        "selected_ref": selected_ref,
        "selected": selected or {},
        "items": table_items,
        "items_truncated": max(0, len(items) - len(table_items)),
        "count": len(items),
        "summary": _summary(selected, items),
        "status": _status_cards(selected, items),
        "inspection": _inspection_groups(selected),
        "sections": _section_rows(selected),
        "assignment_presets": [{"id": item, "label": item.replace("_", " ").title()} for item in _ASSIGNMENT_PRESETS],
        "last_command": last_command,
        "scenario": {
            "id": "redevice_user_face",
            "required_skills": ["redevice_settings", "slideshow_skill", "redevice_voice"],
            "sdk_boundary": "sdk.data.devices + sdk.data.device_access",
        },
        "updated_at": _now_iso(),
    }


def _fingerprint_snapshot(snapshot: Mapping[str, Any]) -> str:
    payload = dict(snapshot)
    payload.pop("updated_at", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _publish(webspace_id: str | None = None, *, force: bool = False) -> dict[str, Any]:
    snapshot = _build_snapshot(webspace_id)
    ws = _webspace_id(webspace_id)
    now = time.monotonic()
    fingerprint = _fingerprint_snapshot(snapshot)
    last_at = float(_LAST_PUBLISH_AT.get(ws) or 0.0)
    last_fingerprint = _LAST_PUBLISH_FINGERPRINT.get(ws)
    if force or fingerprint != last_fingerprint or now - last_at >= _MIN_PUBLISH_INTERVAL_S:
        stream_publish(_RECEIVER, snapshot, _meta={"webspace_id": ws})
        _LAST_PUBLISH_AT[ws] = now
        _LAST_PUBLISH_FINGERPRINT[ws] = fingerprint
    return snapshot


def _ack(
    snapshot: Mapping[str, Any],
    *,
    status: str,
    ok: bool = True,
    result: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    selected = _mapping(snapshot.get("selected"))
    payload = {
        "ok": bool(ok),
        "status": status,
        "receiver": _RECEIVER,
        "selected_ref": _text(snapshot.get("selected_ref") or selected.get("ref")),
        "selected_code": _text(selected.get("code")),
        "selected_title": _text(selected.get("title")),
        "count": int(snapshot.get("count") or 0),
        "updated_at": _text(snapshot.get("updated_at")) or _now_iso(),
    }
    if result is not None:
        payload["result"] = _compact_result(result)
    payload.update({key: _compact_value(value, max_fields=8, max_text=120) for key, value in extra.items()})
    return payload


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    return payload if isinstance(payload, Mapping) else {}


def _matches_receiver(payload: Mapping[str, Any]) -> bool:
    receiver = _text(payload.get("receiver"))
    return receiver in {_RECEIVER, "redevice_settings.*"}


@tool
def refresh_redevice_settings_state(webspace_id: str | None = None) -> dict[str, Any]:
    return _ack(_publish(webspace_id), status="refreshed")


@tool
def select_redevice_settings_endpoint(device_ref: str | None = None, code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    token = _text(device_ref)
    if not token and _text(code):
        for item in _load_devices():
            if _text(item.get("code")) == _text(code):
                token = _text(item.get("ref"))
                break
    if not token:
        return {"ok": False, "error": "device_ref_required"}
    _set_selected(_webspace_id(webspace_id), token)
    return _ack(_publish(webspace_id), status="selected")


@tool
def rename_redevice_settings_endpoint(
    device_ref: str | None = None,
    code: str | None = None,
    display_name: str | None = None,
    aliases: str | list[str] | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    resolved_ref = _text(device_ref) or _text(selected.get("ref"))
    resolved_code = _text(code) or _text(selected.get("code"))
    raw_aliases = aliases if isinstance(aliases, list) else str(aliases or "").split(",")
    alias_list: list[str] = []
    seen: set[str] = set()
    for item in raw_aliases:
        alias = _text(item)
        folded = alias.casefold()
        if alias and folded not in seen:
            seen.add(folded)
            alias_list.append(alias)
    result = sdk_device_access.update_endpoint_profile(
        device_ref=resolved_ref,
        code=resolved_code,
        display_name=display_name,
        aliases=alias_list,
    )
    snapshot = _publish(webspace_id)
    return _ack(snapshot, status="renamed", ok=bool(result.get("ok")), result=result)


@tool
def set_redevice_assignment(
    assignment: str,
    device_ref: str | None = None,
    code: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    ref = _text(device_ref) or _text(selected.get("ref"))
    if not ref and _text(code):
        for item in _load_devices():
            if _text(item.get("code")) == _text(code):
                ref = _text(item.get("ref"))
                break
    if not ref:
        return {"ok": False, "error": "device_ref_required"}
    normalized = _text(assignment).lower() or "idle"
    if normalized not in _ASSIGNMENT_PRESETS:
        normalized = "idle"
    state = _assignments()
    state[ref] = normalized
    _set_memory_dict(_ASSIGNMENTS_KEY, state)
    return _ack(_publish(webspace_id), status="assignment_updated", assignment=normalized)


@tool
def send_redevice_settings_command(
    action: str,
    device_ref: str | None = None,
    code: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    snapshot_before = _build_snapshot(webspace_id)
    items = list(snapshot_before.get("items") or [])
    requested_ref = _text(device_ref)
    requested_code = _text(code)
    selected = _mapping(snapshot_before.get("selected"))
    if requested_ref or requested_code:
        selected = next(
            (
                _mapping(item)
                for item in items
                if (requested_ref and _text(_mapping(item).get("ref")) == requested_ref)
                or (requested_code and _text(_mapping(item).get("code")) == requested_code)
            ),
            selected,
        )
    ref = requested_ref or _text(selected.get("ref"))
    pair_code = requested_code or _text(selected.get("code"))
    command_type_by_action = {
        "open_wifi": "settings.open_wifi",
        "open_bluetooth": "settings.open_bluetooth",
        "bluetooth_reconnect": "bluetooth.reconnect_output",
        "speaker_test": "audio.test_output",
        "volume_up": "audio.volume_up",
        "volume_down": "audio.volume_down",
        "keep_awake": "display.keep_awake",
        "run_diagnostics": "diagnostics.run",
        "logout": "endpoint.logout",
    }
    token = _text(action).lower()
    command_type = command_type_by_action.get(token)
    if not command_type:
        return {"ok": False, "error": "unknown_action", "action": token}
    if not ref and not pair_code:
        result = {
            "ok": False,
            "error": "endpoint_required",
            "action": token,
            "message": "Select a ReDevice endpoint before sending settings commands.",
        }
        _set_memory_dict(_LAST_COMMAND_KEY, {"action": token, "result": result, "updated_at": _now_iso()})
        snapshot = _publish(webspace_id)
        return _ack(snapshot, status="command_rejected", ok=False, result=result, action=token)
    if selected and not bool(selected.get("online")):
        result = {
            "ok": False,
            "error": "endpoint_offline",
            "device_ref": ref,
            "code": pair_code,
            "online_state": _text(selected.get("online_state")) or "offline",
        }
        _set_memory_dict(_LAST_COMMAND_KEY, {"action": token, "result": result, "updated_at": _now_iso()})
        snapshot = _publish(webspace_id)
        return _ack(snapshot, status="command_rejected", ok=False, result=result, action=token)
    command = {
        "command_id": f"cmd:settings:{int(time.time() * 1000)}",
        "type": command_type,
        "payload": {
            "surface_id": f"settings:{token}",
            "surface_ref": "redevice.settings",
            "title": "ReDevice settings",
            "body": f"Requested action: {token}. This action is policy-bound and may require native support on the endpoint.",
            "active_app": {
                "app_id": "redevice_settings",
                "skill_id": "redevice_settings",
                "label": "ReDevice Settings",
                "fullscreen": False,
            },
        },
    }
    result = sdk_device_access.send_endpoint_command(device_ref=ref, code=pair_code, command=command)
    _set_memory_dict(_LAST_COMMAND_KEY, {"action": token, "command": command, "result": result, "updated_at": _now_iso()})
    snapshot = _publish(webspace_id)
    return _ack(snapshot, status="command_sent", ok=bool(result.get("ok")), result=result, action=token)


@tool
def revoke_redevice_settings_endpoint(device_ref: str | None = None, code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    result = sdk_device_access.revoke_endpoint(device_ref=device_ref or _text(selected.get("ref")), code=code or _text(selected.get("code")))
    snapshot = _publish(webspace_id)
    return _ack(snapshot, status="revoked", ok=bool(result.get("ok")), result=result)


@tool
def retire_redevice_settings_endpoint(device_ref: str | None = None, code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    result = sdk_device_access.retire_endpoint(device_ref=device_ref or _text(selected.get("ref")), code=code or _text(selected.get("code")))
    snapshot = _publish(webspace_id)
    return _ack(snapshot, status="retired", ok=bool(result.get("ok")), result=result)


def dispose(reason: str | None = None, **_: Any) -> dict[str, Any]:
    _set_memory_dict(_LAST_COMMAND_KEY, {"action": "dispose", "reason": _text(reason) or "dispose", "updated_at": _now_iso()})
    return {"ok": True, "reason": _text(reason) or "dispose", "updated_at": _now_iso()}


def on_quarantine(
    ttl_s: float | None = None,
    reason: str | None = None,
    metrics: Mapping[str, Any] | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    incident = {
        "schema": "adaos.redevice_settings.quarantine.v1",
        "reason": _text(reason) or "unknown",
        "ttl_s": ttl_s,
        "webspace_id": _text(webspace_id) or None,
        "metrics": _compact_value(dict(metrics or {}), max_fields=12, max_text=120),
        "updated_at": _now_iso(),
    }
    _set_memory_dict(_LAST_COMMAND_KEY, {"action": "quarantine", "result": incident, "updated_at": _now_iso()})
    return {"ok": True, "incident": incident}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    if not _matches_receiver(payload):
        return
    _publish(_text(payload.get("webspace_id") or payload.get("workspace_id")) or None)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_receiver(payload):
        _publish(_text(payload.get("webspace_id") or payload.get("workspace_id")) or None)
