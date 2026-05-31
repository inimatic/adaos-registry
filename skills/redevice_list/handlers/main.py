from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.io import stream_publish

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


_LOG = logging.getLogger("adaos.skill.redevice_list")
_RECEIVER = "redevice_list.devices"


def _root_base() -> str:
    raw = (
        os.environ.get("ADAOS_ROOT_API_BASE")
        or os.environ.get("PUBLIC_ROOT_BASE")
        or os.environ.get("ROOT_API_BASE")
        or "https://ru.api.inimatic.com"
    )
    return str(raw).strip().rstrip("/")


def _request_json(method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_root_base()}{path}"
    body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method.upper())
    req.add_header("accept", "application/json")
    if body is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=12) as res:
            raw = res.read().decode("utf-8")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"http_{exc.code}", "detail": detail}


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


def _compact_device(item: Mapping[str, Any]) -> dict[str, Any]:
    policy = item.get("endpoint_policy") if isinstance(item.get("endpoint_policy"), Mapping) else {}
    manifest = item.get("endpoint_manifest") if isinstance(item.get("endpoint_manifest"), Mapping) else {}
    diagnostics = item.get("diagnostic_report") if isinstance(item.get("diagnostic_report"), Mapping) else {}
    code = str(item.get("code") or "")
    endpoint_id = str(item.get("endpoint_id") or manifest.get("endpoint_id") or "")
    label = str(item.get("device_label") or manifest.get("display_name") or endpoint_id or code)
    state = str(item.get("state") or "-")
    trust = str(policy.get("trust_level") or manifest.get("trust_level") or "limited")
    zone = str(item.get("zone_id") or "-")
    seen = _age(item.get("approved_at") or item.get("issued_at"))
    return {
        "id": code or endpoint_id,
        "code": code,
        "title": label,
        "subtitle": f"state={state} zone={zone} trust={trust} seen={seen}",
        "state": state,
        "zone_id": zone,
        "trust_level": trust,
        "endpoint_id": endpoint_id,
        "hub_id": item.get("hub_id"),
        "diagnostic_report": diagnostics or None,
        "endpoint_manifest": manifest or None,
        "endpoint_policy": policy or None,
        "content": {
            "diagnostic_report": diagnostics or None,
            "endpoint_manifest": manifest or None,
            "endpoint_policy": policy or None,
        },
    }


def _load_devices() -> list[dict[str, Any]]:
    res = _request_json("GET", "/v1/redevice/devices")
    devices = res.get("devices") if isinstance(res, Mapping) else None
    if not isinstance(devices, list):
        return []
    return [_compact_device(item) for item in devices if isinstance(item, Mapping)]


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
    token = urllib.parse.quote(str(code or "").strip(), safe="")
    if not token:
        return {"ok": False, "error": "code_required"}
    res = _request_json("POST", f"/v1/redevice/devices/{token}/revoke", {})
    _publish_devices()
    return {"ok": bool(res.get("ok")), "result": res}


@tool
def retire_redevice(code: str) -> dict[str, Any]:
    token = urllib.parse.quote(str(code or "").strip(), safe="")
    if not token:
        return {"ok": False, "error": "code_required"}
    res = _request_json("POST", f"/v1/redevice/devices/{token}/retire", {})
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
