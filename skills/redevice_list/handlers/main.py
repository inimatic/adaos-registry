from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.io import stream_publish
from adaos.sdk.redevice import ReDeviceBridge
from adaos.services.redevice_versions import endpoint_version_info

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


_LOG = logging.getLogger("adaos.skill.redevice_list")
_RECEIVER = "redevice_list.devices"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _age(value: Any) -> str:
    try:
        ts = float(value or 0)
    except Exception:
        return "-"
    if ts <= 0:
        return "-"
    sec = max(0, int(datetime.now(tz=timezone.utc).timestamp() - ts))
    if sec < 60:
        return f"{sec}s"
    return f"{sec // 60}m {sec % 60}s"


def _last_seen_status(value: Any) -> tuple[str, str]:
    try:
        ts = float(value or 0)
    except Exception:
        return "unknown", "-"
    if ts <= 0:
        return "unknown", "-"
    sec = max(0, int(datetime.now(tz=timezone.utc).timestamp() - ts))
    if sec < 60:
        status = "online"
    elif sec < 5 * 60:
        status = "stale"
    else:
        status = "offline"
    return status, _age(ts)


def _compact_device(item: Mapping[str, Any]) -> dict[str, Any]:
    policy = item.get("endpoint_policy") if isinstance(item.get("endpoint_policy"), Mapping) else {}
    manifest = item.get("endpoint_manifest") if isinstance(item.get("endpoint_manifest"), Mapping) else {}
    diagnostics = item.get("diagnostic_report") if isinstance(item.get("diagnostic_report"), Mapping) else {}
    active_app = item.get("active_app") if isinstance(item.get("active_app"), Mapping) else {}
    active_surface = item.get("active_surface") if isinstance(item.get("active_surface"), Mapping) else {}
    version_info = endpoint_version_info(item)
    software_version = _text(version_info.get("software_version")) or "-"
    served_version = _text(version_info.get("served_version")) or "unknown"
    version_status = _text(version_info.get("version_status")) or "unknown"
    code = str(item.get("code") or "")
    endpoint_id = str(item.get("endpoint_id") or manifest.get("endpoint_id") or "")
    label = str(item.get("display_name") or item.get("device_label") or manifest.get("display_name") or endpoint_id or code)
    state = str(item.get("state") or "-")
    trust = str(policy.get("trust_level") or manifest.get("trust_level") or "limited")
    zone = str(item.get("zone_id") or "-")
    online_state, last_seen = _last_seen_status(item.get("last_seen_at"))
    admitted = _age(item.get("approved_at") or item.get("issued_at"))
    return {
        "id": code or endpoint_id,
        "code": code,
        "title": label,
        "subtitle": (
            f"state={state} online={online_state} last_seen={last_seen} "
            f"zone={zone} trust={trust} version={software_version}/{served_version}"
        ),
        "state": state,
        "online_state": online_state,
        "last_seen": last_seen,
        "admitted_age": admitted,
        "zone_id": zone,
        "trust_level": trust,
        "endpoint_id": endpoint_id,
        "hub_id": item.get("hub_id"),
        "software_version": software_version,
        "served_version": served_version,
        "version_status": version_status,
        "version_source": _text(version_info.get("software_version_source")) or "-",
        "served_version_source": _text(version_info.get("served_version_source")) or "-",
        "version_summary": f"used={software_version} served={served_version} status={version_status}",
        "diagnostic_report": diagnostics or None,
        "endpoint_manifest": manifest or None,
        "endpoint_policy": policy or None,
        "active_app_label": str(active_app.get("label") or active_app.get("app_id") or "-"),
        "active_surface_ref": str(active_surface.get("surface_ref") or active_surface.get("surface_id") or "-"),
        "aliases": ", ".join(str(item or "").strip() for item in list(item.get("aliases") or []) if str(item or "").strip()),
        "content": {
            "diagnostic_report": diagnostics or None,
            "endpoint_manifest": manifest or None,
            "endpoint_policy": policy or None,
            "version_info": version_info,
            "active_app": active_app or None,
            "active_surface": active_surface or None,
            "service_state": item.get("service_state") if isinstance(item.get("service_state"), Mapping) else None,
            "aliases": list(item.get("aliases") or []),
        },
    }


def _load_devices() -> list[dict[str, Any]]:
    return [_compact_device(item) for item in ReDeviceBridge(timeout=12).list_endpoints(sync_registry=True)]


def _publish_devices(webspace_id: str | None = None) -> list[dict[str, Any]]:
    items = _load_devices()
    stream_publish(
        _RECEIVER,
        {"items": items, "count": len(items), "updated_at": datetime.now(tz=timezone.utc).isoformat()},
        _meta={"webspace_id": str(webspace_id or default_webspace_id())},
    )
    return items


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    return payload if isinstance(payload, Mapping) else {}


def _matches_receiver(payload: Mapping[str, Any]) -> bool:
    receiver = str(payload.get("receiver") or "").strip()
    return receiver in {_RECEIVER, "redevice_list.*"}


@tool
def refresh_redevices(webspace_id: str | None = None) -> dict[str, Any]:
    items = _publish_devices(webspace_id)
    return {"ok": True, "count": len(items)}


@tool
def revoke_redevice(code: str) -> dict[str, Any]:
    token = str(code or "").strip()
    if not token:
        return {"ok": False, "error": "code_required"}
    res = ReDeviceBridge(timeout=12).revoke(token)
    _publish_devices()
    return {"ok": bool(res.get("ok")), "result": res}


@tool
def retire_redevice(code: str) -> dict[str, Any]:
    token = str(code or "").strip()
    if not token:
        return {"ok": False, "error": "code_required"}
    res = ReDeviceBridge(timeout=12).retire(token)
    _publish_devices()
    return {"ok": bool(res.get("ok")), "result": res}


@tool
def rename_redevice(code: str, display_name: str | None = None, aliases: str | list[str] | None = None) -> dict[str, Any]:
    token = str(code or "").strip()
    if not token:
        return {"ok": False, "error": "code_required"}
    raw_aliases = aliases if isinstance(aliases, list) else str(aliases or "").split(",")
    alias_list = []
    seen = set()
    for item in raw_aliases:
        alias = str(item or "").strip()
        folded = alias.casefold()
        if not alias or folded in seen:
            continue
        seen.add(folded)
        alias_list.append(alias)
    res = ReDeviceBridge(timeout=12).update_profile(token, display_name=display_name, aliases=alias_list)
    _publish_devices()
    return {"ok": bool(res.get("ok")), "result": res}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    if not _matches_receiver(payload):
        return
    webspace_id = str(payload.get("webspace_id") or payload.get("workspace_id") or default_webspace_id())
    try:
        _publish_devices(webspace_id)
    except Exception:
        _LOG.exception("failed to publish ReDevice list snapshot")


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_receiver(payload):
        on_webio_stream_snapshot_requested(evt)
